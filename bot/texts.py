"""Все русские строки бота в одном месте — правятся чаще всего остального.

Про род глаголов: Telegram не сообщает пол пользователя, поэтому авторство
подписывается нейтрально («Аня, вчера в 21:14»), а не «добавила Аня» —
угадывать род по имени значит регулярно ошибаться.
"""

from datetime import datetime

from aiogram.utils.text_decorations import html_decoration as fmt

from bot.db.models import Entry
from bot.services import timeutil as tu

GREETING = (
    "Привет! Я семейный планировщик.\n\n"
    "Пишите сюда что угодно — задачи, заметки, покупки, события. "
    "Я запомню, кто и когда записал, и напомню вовремя.\n\n"
    "Ничего настраивать не нужно, семья уже создана.\n"
    "Начните с /new — записать что-нибудь, или /today — план на сегодня."
)

PONG = "Живой. Семья: {title}, участников: {members}."

PRIVATE_CHAT = (
    "Я работаю в общем семейном чате, а не в личке. "
    "Добавьте меня в группу «Семья»."
)

KIND_ICONS = {"task": "✅", "note": "📝", "event": "📅", "shopping": "🛒"}
KIND_NAMES = {
    "task": "Задача",
    "note": "Заметка",
    "event": "Событие",
    "shopping": "Покупка",
}

EMPTY_TODAY = "На сегодня ничего не запланировано."
EMPTY_WEEK = "На этой неделе ничего не запланировано."
EMPTY_TASKS = "Открытых задач нет."
EMPTY_NOTES = "Заметок пока нет."
EMPTY_SEARCH = "По запросу «{query}» ничего не нашлось."
FIND_USAGE = "Как искать: <code>/find молоко</code>"

HEADER_TODAY = "📅 <b>Сегодня, {date}</b>"
HEADER_WEEK = "📅 <b>Неделя {start} — {end}</b>"
HEADER_TASKS = "✅ <b>Задачи</b> ({shown} из {total})"
HEADER_NOTES = "📝 <b>Заметки</b> ({shown} из {total})"
HEADER_SEARCH = "🔎 <b>Найдено по «{query}»:</b> {count}"
HEADER_OVERDUE = "⚠️ <b>Просрочено</b>"

DONE_CONFIRMED = "Готово: {title}"
DONE_ALREADY = "Эта запись уже закрыта."
SAVED = "Записал."

FAMILY_HEADER = "👨‍👩‍👦 <b>{title}</b>\nТаймзона: {tz} · дайджест в {digest}"
FAMILY_MEMBER = "• {name} — записей: {count}"


def _escape(value: str) -> str:
    return fmt.quote(value)


def _author_suffix(entry: Entry, tz: str, now: datetime | None = None) -> str:
    name = _escape(entry.author.display_name) if entry.author else "кто-то"
    return fmt.italic(f"{name}, {tu.fmt_when(entry.created_at, tz, now)}")


def _when_part(entry: Entry, tz: str, now: datetime | None, show_date: bool) -> str:
    if entry.due_at is None:
        return ""
    if show_date:
        return tu.fmt_due(entry.due_at, tz, all_day=entry.all_day, now=now)
    if entry.all_day:
        return "весь день"
    return f"{tu.to_local(entry.due_at, tz):%H:%M}"


def entry_line(
    entry: Entry, tz: str, now: datetime | None = None, *, show_date: bool = True
) -> str:
    """Одна строка списка. Единственное место, где запись превращается в текст."""
    icon = "✔️" if entry.status == "done" else KIND_ICONS.get(entry.kind, "•")
    title = _escape(entry.title)
    if entry.status == "done":
        title = fmt.strikethrough(title)

    when = _when_part(entry, tz, now, show_date)
    head = f"{icon} {fmt.bold(when)} {title}" if when else f"{icon} {title}"
    return f"{head} — {_author_suffix(entry, tz, now)}"


def entry_card(entry: Entry, tz: str, now: datetime | None = None) -> str:
    """Развёрнутая карточка одной записи — после сохранения."""
    icon = KIND_ICONS.get(entry.kind, "•")
    name = KIND_NAMES.get(entry.kind, entry.kind)
    lines = [f"{icon} <b>{name}:</b> {_escape(entry.title)}"]
    if entry.body:
        lines.append(_escape(entry.body))
    when = _when_part(entry, tz, now, show_date=True)
    if when:
        lines.append(f"📅 {when}")
    lines.append(_author_suffix(entry, tz, now))
    return "\n".join(lines)


def day_header(day, tz: str, now: datetime | None = None) -> str:
    today = tu.local_today(tz, now)
    label = tu.day_label(day, today)
    stamp = f"{day.day} {tu.MONTHS_SHORT[day.month - 1]}"
    weekday = tu.WEEKDAYS_SHORT[day.weekday()]
    return fmt.bold(f"{label}, {stamp}" if label else f"{weekday}, {stamp}")
