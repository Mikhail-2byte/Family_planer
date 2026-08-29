"""Точка входа: настройка логирования.

Модуль `bot/__main__.py` не упоминался в тестах ни разу — и это вышло боком при
первом же боевом перезапуске 29.08.2026. Логирование в нём настраивается **на
импорте**, поэтому недоступный лог-файл убивал процесс до `main()`: старый
`cmd.exe` ещё держал `bot.log` открытым по редиректу, новый получил
`PermissionError` и не стартовал вовсе.

Полный запуск здесь не проверить — это long polling и сеть. Зато проверяется то,
что тогда сломалось: сбор обработчиков логов.
"""

import logging

import pytest

from bot import __main__ as entry


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "bot.log"
    monkeypatch.setattr(entry, "LOG_PATH", path)
    return path


def test_writes_a_file_handler_in_utf8(log_path):
    handlers = entry._log_handlers()
    try:
        files = [h for h in handlers if isinstance(h, logging.FileHandler)]
        assert len(files) == 1
        # Кодировка задана явно, иначе Python возьмёт локаль Windows — ровно то,
        # из-за чего боевой лог был нечитаемой смесью cp866 и utf-8
        assert files[0].encoding == "utf-8"
    finally:
        _close(handlers)


def test_console_handler_is_always_there(log_path):
    handlers = entry._log_handlers()
    try:
        assert any(type(h) is logging.StreamHandler for h in handlers)
    finally:
        _close(handlers)


def test_unopenable_log_does_not_stop_the_bot(log_path, capsys):
    """Тест, ради которого файл и заведён.

    Полный диск, антивирус, чужой открытый хендл — причин хватает. Бот обязан
    подняться без файла: лог вещь побочная, как `parse_log`, и терять из-за
    него семейного бота нельзя.
    """
    log_path.mkdir()  # каталог на месте файла: открыть его нельзя

    handlers = entry._log_handlers()
    try:
        assert handlers, "консольный обработчик обязан остаться"
        assert not [h for h in handlers if isinstance(h, logging.FileHandler)]
        # И человек должен узнать, почему файла нет
        assert "недоступен" in capsys.readouterr().err
    finally:
        _close(handlers)


def test_directory_is_created_when_missing(tmp_path, monkeypatch):
    """`data/` заводит и `db.session`, но полагаться на порядок импортов нельзя."""
    path = tmp_path / "новый" / "bot.log"
    monkeypatch.setattr(entry, "LOG_PATH", path)

    handlers = entry._log_handlers()
    try:
        assert path.parent.is_dir()
    finally:
        _close(handlers)


def _close(handlers: list[logging.Handler]) -> None:
    """Хендлеры держат файл открытым — на Windows его иначе не удалить."""
    for handler in handlers:
        handler.close()
