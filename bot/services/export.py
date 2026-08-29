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

# Удалённая запись (этап 7) из базы не пропадает и обязана попасть в выгрузку:
# это единственное место, где её вообще видно. Но выглядеть как живая она не
# должна — иначе выгрузка врёт ровно про то, ради чего мягкое удаление затеяно
STATUS_TITLES = {"open": "открыта", "done": "закрыта", "archived": "удалена"}

# Чекбоксы Markdown: удалённой нужен свой, иначе она неотличима от открытой
STATUS_BOXES = {"done": "[x]", "archived": "[—]"}

COLUMNS = (
    "id",
    "тип",
    "статус",
    "заголовок",
    "описание",
    "срок",
    "весь день",
    "автор",
    "поручено",
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
                entry.assignee.display_name if entry.assignee else "",
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


# --- iCalendar -----------------------------------------------------------------
#
# Односторонняя выгрузка в календарь — то, что осталось от отменённой интеграции
# с Google (28.08.2026). Отменена была именно интеграция: OAuth, `token.json`,
# таблица `gcal_map` и живая синхронизация. Файл `.ics` ничего этого не требует
# — его открывает любой календарь, включая гугловский, — и потому запрету не
# противоречит. Синхронизации здесь нет и не будет: выгрузил и забыл.
#
# Пишем формат руками, без библиотеки. RFC 5545 велик, но нужная его часть —
# десяток строк, а новая зависимость ради них тянется в `requirements.lock`
# и в образ.

# Домен для UID. Своего у бота нет, а UID обязан быть глобально уникальным,
# иначе повторная выгрузка заведёт в календаре дубли вместо обновления записей
ICS_DOMAIN = "family-planner.local"

# RFC 5545 ограничивает строку 75 **октетами**, не символами, и требует
# переносить продолжение с пробела. Считать длину в символах нельзя: кириллица
# в UTF-8 занимает по два байта, и строка из 70 букв «я» — это 140 октетов.
# Берём 73, чтобы у строки продолжения хватило места на ведущий пробел
ICS_FOLD_AT = 73


def _ics_escape(value: str) -> str:
    """Экранирование по RFC 5545. Порядок важен: обратный слэш первым.

    Иначе экранирующий слэш, добавленный к запятой, сам будет экранирован
    следующим проходом — и в календаре появится `\\\\,` вместо `,`.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Длинную строку — на куски по `ICS_FOLD_AT` октетов, продолжения с пробела.

    Границу считаем в октетах, а режем по символам: рвать UTF-8 посередине
    нельзя — получится битый файл. Поэтому набираем посимвольно, пока
    помещается, а не делим строку на равные куски: при кириллице «равные куски»
    по символам дают вдвое больше октетов, чем разрешено.
    """
    if len(line.encode("utf-8")) <= ICS_FOLD_AT:
        return [line]

    folded: list[str] = []
    chunk, size = "", 0
    # У продолжений первый октет съедает ведущий пробел
    for char in line:
        width = len(char.encode("utf-8"))
        limit = ICS_FOLD_AT - (1 if folded else 0)
        if size + width > limit:
            folded.append(chunk)
            chunk, size = char, width
        else:
            chunk += char
            size += width
    folded.append(chunk)
    return [folded[0], *(f" {part}" for part in folded[1:])]


def to_ics(entries: list[Entry], tz: str, title: str, now: datetime) -> bytes:
    """События и записи со сроком → календарь. Пусто — тоже валидный файл.

    Берём **только записи с `due_at`**: у заметки без срока в календаре места
    нет, а пустая дата сделала бы файл невалидным. Удалённые (`archived`) не
    выгружаем вовсе — в отличие от CSV и Markdown: те задуманы как полный
    слепок базы, а календарь — как рабочий инструмент, и воскрешать в нём
    выброшенное незачем.

    `all_day` едет как `VALUE=DATE` — так календари и рисуют его полосой на
    весь день, а не встречей в полночь. Остальное переводится в UTC с суффиксом
    `Z`: писать локальное время пришлось бы вместе с блоком `VTIMEZONE`,
    а он в разы больше всего остального файла.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{ICS_DOMAIN}//Family Planner//RU",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(title)}",
    ]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    for entry in entries:
        if entry.due_at is None or entry.status == "archived":
            continue
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{entry.id}@{ICS_DOMAIN}")
        lines.append(f"DTSTAMP:{stamp}")
        if entry.all_day:
            local_day = tu.to_local(entry.due_at, tz).date()
            lines.append(f"DTSTART;VALUE=DATE:{local_day.strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART:{entry.due_at.strftime('%Y%m%dT%H%M%SZ')}")
        lines.append(f"SUMMARY:{_ics_escape(_flat(entry.title))}")

        # ATTENDEE намеренно не используем: он требует адреса участника и
        # превращает событие в приглашение, которое календарь попытается
        # разослать. Поручение — просто строка описания
        described = [_flat(entry.body)] if entry.body else []
        if entry.assignee:
            described.append(f"Поручено: {entry.assignee.display_name}")
        if described:
            lines.append(f"DESCRIPTION:{_ics_escape(' — '.join(described))}")
        # Закрытую помечаем статусом, а не выбрасываем: план на прошедшую неделю
        # без сделанного читается как «ничего и не было»
        lines.append(
            "STATUS:COMPLETED" if entry.status == "done" else "STATUS:CONFIRMED"
        )
        lines.append(f"CATEGORIES:{_ics_escape(KIND_TITLES.get(entry.kind, entry.kind))}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    # CRLF — требование RFC, и не формальное: часть календарей на голом \n
    # молча показывает пустой файл
    folded = [part for line in lines for part in _fold(line)]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")


def _md_line(entry: Entry, tz: str) -> str:
    box = STATUS_BOXES.get(entry.status, "[ ]")
    parts = [f"- {box}"]
    when = _moment(entry.due_at, tz, all_day=entry.all_day)
    if when:
        parts.append(f"**{when}**")
    parts.append(_flat(entry.title))
    body = _flat(entry.body)
    if body:
        parts.append(f"— {body}")
    if entry.author:
        who = entry.author.display_name
        if entry.assignee:
            # Стрелкой, а не двумя скобками: «(Аня) (Миша)» не читается, а
            # «(Аня → Миша)» сразу говорит, кто завёл и кому поручено
            who += f" → {entry.assignee.display_name}"
        parts.append(f"({who})")
    return " ".join(parts)
