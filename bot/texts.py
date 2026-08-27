"""Все русские строки бота в одном месте — правятся чаще всего остального.

Про род глаголов: Telegram не сообщает пол пользователя, поэтому авторство
подписывается нейтрально («Аня, вчера в 21:14»), а не «добавила Аня» —
угадывать род по имени значит регулярно ошибаться.
"""

from datetime import date, datetime, timedelta

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

# Меню команд в поле ввода. Кнопок на клавиатуре пять, а команд десять —
# без этого списка про `/week`, `/find`, `/family` и `/cancel` узнать неоткуда
COMMANDS = [
    ("new", "Записать что-нибудь"),
    ("today", "План на сегодня"),
    ("week", "План на неделю"),
    ("tasks", "Открытые задачи"),
    ("notes", "Заметки"),
    ("find", "Поиск: /find молоко"),
    ("remind", "Напомнить: /remind завтра в 19:00 позвонить маме"),
    ("family", "Кто в семье и сколько записал"),
    ("cancel", "Прервать мастер /new"),
    ("ping", "Проверить, жив ли бот"),
]

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

SOON_SHOPPING = "Покупки ещё не готовы — они появятся на этапе 4."
WIZARD_DROPPED = "Запись через /new прервана — начните заново, если она нужна."

EMPTY_TODAY = "На сегодня ничего не запланировано."
EMPTY_WEEK = "На этой неделе ничего не запланировано."
EMPTY_TASKS = "Открытых задач нет."
EMPTY_NOTES = "Заметок пока нет."
EMPTY_SEARCH = "По запросу «{query}» ничего не нашлось."
FIND_USAGE = "Как искать: <code>/find молоко</code>"

HEADER_WEEK = "📅 <b>Неделя {start} — {end}</b>"
HEADER_TASKS = "✅ <b>Задачи</b> ({shown} из {total})"
HEADER_NOTES = "📝 <b>Заметки</b> ({shown} из {total})"
HEADER_SEARCH = "🔎 <b>Найдено по «{query}»:</b> {count}"
HEADER_OVERDUE = "⚠️ <b>Просрочено</b>"
SEARCH_TRUNCATED = "Показаны первые {limit} — уточните запрос."

MORE_ITEMS = "…и ещё {count}"
# Сообщение уходит одним куском, а у Telegram лимит 4096 символов. Любой
# список, который человек может наращивать бесконечно, обязан иметь потолок:
# просрочка копится годами, пропущенные — за время простоя ПК.
MAX_SUMMARY_ITEMS = 10
MAX_DAY_ITEMS = 15
# У недели потолок свой и общий на все семь дней: семь дней по MAX_DAY_ITEMS
# перерастают 4096 символов, и /week отваливался бы целиком. 30 — из бюджета:
# минус заголовок недели, до семи заголовков дней и хвост остаётся ~3900
# символов на строки, то есть ~130 на строку с учётом HTML-тегов
MAX_WEEK_ITEMS = 30

DONE_CONFIRMED = "Готово: {title}"
DONE_ALREADY = "Эта запись уже закрыта."
SAVED = "Записал."

FAMILY_HEADER = "👨‍👩‍👦 <b>{title}</b>\nТаймзона: {tz} · дайджест в {digest}"
FAMILY_MEMBER = "• {name} — записей: {count}"


def _escape(value: str) -> str:
    return fmt.quote(value)


# Ниже — подстановки, куда попадает текст, введённый человеком: поисковый
# запрос, имя участника, название чата. `parse_mode="HTML"` включён глобально,
# поэтому без экранирования Telegram отвечает «can't parse entities», и
# сообщение не уходит вообще.


def pong(title: str, members: int) -> str:
    return PONG.format(title=_escape(title), members=members)


def search_header(query: str, count: int) -> str:
    return HEADER_SEARCH.format(query=_escape(query), count=count)


def search_empty(query: str) -> str:
    return EMPTY_SEARCH.format(query=_escape(query))


def family_header(title: str, tz: str, digest: str) -> str:
    return FAMILY_HEADER.format(title=_escape(title), tz=tz, digest=digest)


def family_member(name: str, count: int) -> str:
    return FAMILY_MEMBER.format(name=_escape(name), count=count)


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


def entry_lines(
    entries,
    tz: str,
    now: datetime | None = None,
    *,
    limit: int,
    show_date: bool = True,
) -> list[str]:
    """Строки списка с обрезанным хвостом.

    Без потолка сообщение рано или поздно перерастает 4096 символов, Telegram
    отвечает `TelegramBadRequest`, и дайджест пропадает молча — навсегда, потому
    что `last_digest_on` при этом всё равно проставляется.
    """
    lines = [
        entry_line(e, tz, now, show_date=show_date) for e in entries[:limit]
    ]
    if len(entries) > limit:
        lines.append(MORE_ITEMS.format(count=len(entries) - limit))
    return lines


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
    stamp = tu.day_stamp(day)
    weekday = tu.WEEKDAYS_SHORT[day.weekday()]
    return fmt.bold(f"{label}, {stamp}" if label else f"{weekday}, {stamp}")


def week_header(monday: date) -> str:
    """Заголовок `/week`. Диапазон считается здесь, а не в хендлере."""
    return HEADER_WEEK.format(
        start=tu.day_stamp(monday), end=tu.day_stamp(monday + timedelta(days=6))
    )


# --- Этап 2: напоминания, догонка, дайджест ---------------------------------

REMINDER = "🔔 {text}"
REMINDER_LATE = "🔔 {text}\n<i>⏰ было запланировано на {when}</i>"

MISSED_HEADER = "🕓 <b>Пока меня не было</b> — пропущено: {count}"
MISSED_ITEM = "• {when} — {text}"

DIGEST_HEADER = "☀️ <b>Доброе утро!</b> Вот что на сегодня."
DIGEST_LATE_NOTE = "<i>Сводка задержалась — меня не было в сети.</i>"

REMIND_USAGE = (
    "Как пользоваться: <code>/remind через 2 минуты позвонить маме</code>\n"
    "Можно и наоборот: <code>/remind позвонить маме завтра в 19:00</code>"
)
REMIND_NO_DATE = (
    "Не понял, когда напомнить. Укажите время словами — «через час», "
    "«завтра в 19:00», «в понедельник в 9:00».\n"
    "Или заведите запись через /new."
)
REMIND_NO_TEXT = "Понял время, но не понял, о чём напомнить."
REMIND_RECURRING = (
    "Повторяющиеся напоминания я пока не завожу — ни из текста, ни кнопками. "
    "Заведите разовое: <code>/remind во вторник в 19:00 позвонить маме</code>"
)
REMIND_PAST = "Это время уже прошло: {when}. Напоминание не создано."
REMIND_OK = "🔔 Напомню {when}: {text}"


def reminder_message(
    reminder, tz: str, now: datetime | None = None, *, late: bool = False
) -> str:
    """Само напоминание. Текст пишет человек — только через экранирование."""
    body = _escape(reminder.text)
    if not late:
        return REMINDER.format(text=body)
    return REMINDER_LATE.format(text=body, when=tu.fmt_due(reminder.fire_at, tz, now=now))


def missed_summary(reminders, tz: str, now: datetime | None = None) -> str:
    """Одно сообщение вместо пачки: ПК был выключен дольше порога сводки."""
    lines = [MISSED_HEADER.format(count=len(reminders))]
    for reminder in reminders[:MAX_SUMMARY_ITEMS]:
        lines.append(
            MISSED_ITEM.format(
                when=tu.fmt_due(reminder.fire_at, tz, now=now),
                text=_escape(reminder.text),
            )
        )
    if len(reminders) > MAX_SUMMARY_ITEMS:
        lines.append(MORE_ITEMS.format(count=len(reminders) - MAX_SUMMARY_ITEMS))
    return "\n".join(lines)


def remind_saved(text: str, fire_at: datetime, tz: str, now: datetime | None = None) -> str:
    return REMIND_OK.format(when=tu.fmt_due(fire_at, tz, now=now), text=_escape(text))


def remind_in_past(fire_at: datetime, tz: str, now: datetime | None = None) -> str:
    return REMIND_PAST.format(when=tu.fmt_due(fire_at, tz, now=now))


# --- Этап 2п: живая панель дня ----------------------------------------------

PANEL_HEADER = "📌 <b>Сегодня</b>"
PANEL_FOOTER = "<i>Это сообщение обновляется само — отвечать на него не нужно.</i>"


def panel(body: str) -> str:
    """Обвязка панели вокруг тела дня.

    `body` собирает `digest.build_day` — там всё уже прошло через `_escape`,
    второй раз экранировать нельзя.

    Ничего изменчивого — времени последнего обновления, счётчиков — сюда
    добавлять нельзя: текст обязан совпадать сам с собой, иначе Telegram
    перестанет отвечать «message is not modified», и каждая холостая
    перерисовка станет настоящей правкой и потратит лимит чата.
    """
    return f"{PANEL_HEADER}\n\n{body}\n\n{PANEL_FOOTER}"
