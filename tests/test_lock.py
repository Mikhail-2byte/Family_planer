"""Замок одного экземпляра (01.09.2026).

Повод — два long polling на одном токене: ни один не упал и не пожаловался в
чат, семьдесят шесть `Conflict` в логе, потерянные сообщения и бот, выглядящий
живым. Подробности и обоснования — в докстринге `bot/services/lock.py`.

Ключевой здесь — `test_a_killed_process_leaves_no_stale_lock`: ровно ради него
выбран замок средствами ОС, а не PID-файл.
"""

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from bot.services import lock


@pytest.fixture
def lockfile(tmp_path):
    """Свой файл замка на тест: боевой лежит рядом с базой."""
    return tmp_path / "bot.lock"


def test_the_first_one_takes_it(lockfile):
    guard = lock.SingleInstance(lockfile)
    try:
        assert guard.acquire(timeout=0) is True
    finally:
        guard.release()


def test_the_second_one_is_refused(lockfile):
    first = lock.SingleInstance(lockfile)
    second = lock.SingleInstance(lockfile)
    try:
        assert first.acquire(timeout=0) is True
        assert second.acquire(timeout=0) is False, (
            "второй экземпляр обязан уйти, а не встать рядом"
        )
    finally:
        first.release()
        second.release()


def test_released_lock_is_free_again(lockfile):
    """Штатный перезапуск: старый отпустил — новый взял."""
    first = lock.SingleInstance(lockfile)
    first.acquire(timeout=0)
    first.release()

    second = lock.SingleInstance(lockfile)
    try:
        assert second.acquire(timeout=0) is True
    finally:
        second.release()


def test_release_without_acquire_is_harmless(lockfile):
    """Зовётся из `finally`, куда можно попасть и не взяв замок."""
    lock.SingleInstance(lockfile).release()


def test_double_release_is_harmless(lockfile):
    guard = lock.SingleInstance(lockfile)
    guard.acquire(timeout=0)
    guard.release()
    guard.release()

    other = lock.SingleInstance(lockfile)
    try:
        assert other.acquire(timeout=0) is True
    finally:
        other.release()


def test_missing_data_dir_is_created(tmp_path):
    """Первый запуск на чистой машине: каталога `data/` ещё нет."""
    target = tmp_path / "нет-такого" / "bot.lock"
    guard = lock.SingleInstance(target)
    try:
        assert guard.acquire(timeout=0) is True
        assert target.exists()
    finally:
        guard.release()


def test_waiting_is_bounded(lockfile):
    """Ожидание ограничено сверху — иначе бот повис бы вместо запуска."""
    holder = lock.SingleInstance(lockfile)
    holder.acquire(timeout=0)
    waiter = lock.SingleInstance(lockfile)
    try:
        started = time.monotonic()
        assert waiter.acquire(timeout=0.2) is False
        assert time.monotonic() - started < 3.0
    finally:
        holder.release()
        waiter.release()


def test_it_waits_for_a_short_overlap(lockfile, monkeypatch):
    """Штатный перезапуск — короткое наложение, и сдаваться на нём нельзя.

    Старый экземпляр ещё держит `getUpdates`, пока новый уже стартует. Замок,
    отказывающий мгновенно, оставил бы бота лежать до следующего тика сторожа,
    то есть на пять минут.
    """
    monkeypatch.setattr(lock, "POLL_SECONDS", 0.05)
    holder = lock.SingleInstance(lockfile)
    holder.acquire(timeout=0)

    # Отпустим замок из таймера — как это сделал бы уходящий экземпляр
    threading.Timer(0.2, holder.release).start()

    waiter = lock.SingleInstance(lockfile)
    try:
        assert waiter.acquire(timeout=5.0) is True, "дождаться было можно"
    finally:
        waiter.release()


def test_a_killed_process_leaves_no_stale_lock(lockfile):
    """Главный тест: замок снимается при убийстве процесса без `finally`.

    Так бот обычно и умирает — `Stop-Process -Force`, выключение питания. Ровно
    поэтому здесь блокировка средствами ОС, а не PID-файл: тот пережил бы смерть
    процесса и запретил бы запуск навсегда, до ручной уборки.
    """
    # Путь едет аргументом, а не подстановкой в текст: в нём бывают обратные
    # слэши, и экранировать их в исходнике — способ отладить не то, что нужно.
    # Рабочий каталог — корень проекта, иначе `import bot` не найдётся
    code = textwrap.dedent("""
        import sys, time
        from pathlib import Path
        from bot.services import lock
        guard = lock.SingleInstance(Path(sys.argv[1]))
        assert guard.acquire(timeout=0)
        # Маркер латиницей: stdout дочернего живёт в кодировке консоли Windows,
        # и кириллица приехала бы сюда мусором
        print("locked", flush=True)
        time.sleep(60)
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(lockfile)],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parent.parent,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked", "дочерний не взял замок"

        blocked = lock.SingleInstance(lockfile)
        assert blocked.acquire(timeout=0) is False, "пока жив — замок занят"
        blocked.release()

        child.kill()  # без graceful shutdown, как `Stop-Process -Force`
        child.wait(timeout=10)

        freed = lock.SingleInstance(lockfile)
        try:
            assert freed.acquire(timeout=5.0) is True, (
                "замок остался протухшим — ровно то, чего PID-файл не умеет"
            )
        finally:
            freed.release()
    finally:
        if child.poll() is None:
            child.kill()
