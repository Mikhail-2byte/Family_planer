"""Выгрузка записей в Markdown и CSV (шаг 6.3).

Модуль чистый, как `parsing.py`: ни БД, ни aiogram. Из проекта берёт только
`timeutil` — перевести срок из наивного UTC в локальное время семьи.

**Почему здесь свой рендер, а не `texts.entry_line`.** Та — единственное место,
где запись превращается в **сообщение чата**, и отдаёт HTML (`<b>`, `<s>`) с
экранированием под `parse_mode="HTML"`. В файле теги — мусор, а `&amp;` вместо
`&` — уже порча данных: выгрузка задумана как то, что читается без бота.
Сторож против соблазна переиспользовать — `test_markdown_carries_no_html`.

По той же причине подписи колонок и названия разделов лежат здесь, а не в
`texts.py`: это содержимое файла, а не строка, которую бот говорит в чат.
"""

import csv
import io
from datetime import date, datetime

from bot.db.models import Entry
from bot.services import timeutil as tu

KIND_TITLES = {
    "task": "Задачи",
    "event": "События",
    "note": "Заметки",
    "shopping": "Покупки",
}

KIND_ORDER = ("task", "event", "note", "shopping")

STATUS_TITLES = {"open": "открыта", "done": "закрыта"}

COLUMNS = (
    "id",
    "тип",
    "статус",
    "заголовок",
    "описание",
    "срок",
    "весь день",
    "автор",
    "создано",
    "закрыто",
    "кто закрыл",
)


def _moment(value: datetime | None, tz: str, *, all_day: bool = False) -> str:
    """Момент из БД → строка для файла. ISO: так и человеку видно, и Excel сортирует."""
    if value is None:
        return ""
    local = tu.to_local(value, tz)
    return local.strftime("%Y-%m-%d") if all_day else local.strftime("%Y-%m-%d %H:%M")


def _flat(value: str | None) -> str:
    """Схлопнуть переводы строк: одна запись — одна строка файла.

    В CSV они пережили бы кавычки, а вот список Markdown многострочный заголовок
    разрывает. Правило одно на оба формата, чтобы файлы не расходились.
    """
    return " ".join((value or "").split())


def to_csv(entries: list[Entry], tz: str) -> bytes:
    """CSV под русский Excel: разделитель `;`, UTF-8 **с BOM**.

    Без BOM Excel читает кириллицу как кракозябры, а с запятой вместо точки с
    запятой валит всю строку в одну ячейку. Цена компромисса: это не канонический
    CSV — pandas и Google Sheets попросят указать разделитель вручную.

    Кавычки, `;` и переводы строк внутри заголовка экранирует сам `csv.writer`.
    Руками этого не делать.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for entry in entries:
        writer.writerow(
            [
                entry.id,
                KIND_TITLES.get(entry.kind, entry.kind),
                STATUS_TITLES.get(entry.status, entry.status),
                _flat(entry.title),
                _flat(entry.body),
                _moment(entry.due_at, tz, all_day=entry.all_day),
                "да" if entry.all_day else "",
                entry.author.display_name if entry.author else "",
                _moment(entry.created_at, tz),
                _moment(entry.done_at, tz),
                entry.closer.display_name if entry.closer else "",
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def to_markdown(entries: list[Entry], tz: str, title: str, today: date) -> bytes:
    """Тот же материал разделами по типам — чтобы читалось глазами.

    Порядок записей внутри раздела сохраняется как пришёл из `repo.all_entries`:
    со сроком по времени, затем без.
    """
    lines = [f"# {title} — выгрузка на {today.isoformat()}", ""]

    by_kind: dict[str, list[Entry]] = {}
    for entry in entries:
        by_kind.setdefault(entry.kind, []).append(entry)

    # Незнакомый `kind` в хвосте: выгрузка обязана вынести всё, что есть в базе,
    # даже если тип завели позже этого кода
    unknown = [kind for kind in by_kind if kind not in KIND_ORDER]
    for kind in (*KIND_ORDER, *sorted(unknown)):
        found = by_kind.get(kind)
        if not found:
            continue
        lines.append(f"## {KIND_TITLES.get(kind, kind)}")
        lines.extend(_md_line(entry, tz) for entry in found)
        lines.append("")

    return "\n".join(lines).encode("utf-8")


def _md_line(entry: Entry, tz: str) -> str:
    box = "[x]" if entry.status == "done" else "[ ]"
    parts = [f"- {box}"]
    when = _moment(entry.due_at, tz, all_day=entry.all_day)
    if when:
        parts.append(f"**{when}**")
    parts.append(_flat(entry.title))
    body = _flat(entry.body)
    if body:
        parts.append(f"— {body}")
    if entry.author:
        parts.append(f"({entry.author.display_name})")
    return " ".join(parts)
