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

# Меню команд в поле ввода. Кнопок на клавиатуре шесть, а команд вдвое больше —
# без этого списка про `/week`, `/find`, `/family`, `/settings` и `/cancel`
# узнать неоткуда
COMMANDS = [
    ("new", "Записать что-нибудь"),
    ("today", "План на сегодня"),
    ("week", "План на неделю"),
    ("tasks", "Открытые задачи"),
    ("notes", "Заметки"),
    ("events", "События"),
    ("buy", "Список покупок"),
    ("find", "Поиск: /find молоко"),
    ("remind", "Напомнить: /remind завтра в 19:00 позвонить маме"),
    ("family", "Кто в семье и сколько записал"),
    ("settings", "Таймзона и время утренней сводки"),
    ("export", "Выгрузить записи в Markdown и CSV"),
    ("backup", "Прислать файл базы"),
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

WIZARD_DROPPED = "Запись через /new прервана — начните заново, если она нужна."

EMPTY_TODAY = "На сегодня ничего не запланировано."
EMPTY_WEEK = "На этой неделе ничего не запланировано."
EMPTY_TASKS = "Открытых задач нет."
EMPTY_NOTES = "Заметок пока нет."
EMPTY_EVENTS = "Событий пока нет."
EMPTY_SEARCH = "По запросу «{query}» ничего не нашлось."
FIND_USAGE = "Как искать: <code>/find молоко</code>"

HEADER_WEEK = "📅 <b>Неделя {start} — {end}</b>"
HEADER_TASKS = "✅ <b>Задачи</b> ({shown} из {total})"
HEADER_NOTES = "📝 <b>Заметки</b> ({shown} из {total})"
HEADER_EVENTS = "📅 <b>События</b> ({shown} из {total})"
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
# Тот же лимит, но как число: разбор LLM наращивает не человек, а модель, и
# потолок по числу элементов (`parsing.MAX_ITEMS`) длину не ограничивает —
# десять записей с длинными заголовками и телами дают под 17 000 символов
MESSAGE_LIMIT = 4096

DONE_CONFIRMED = "Готово: {title}"
# У заметки «Готово» звучит странно — её не выполняют, а убирают с глаз.
# Механизм тот же (`status='done'`), а слово должно быть своё
NOTE_CLOSED = "Убрал из заметок: {title}"
DONE_ALREADY = "Эта запись уже закрыта."
SAVED = "Записал."
# Разбор прошёл порог AUTOSAVE_CONFIDENCE и записан без карточки (шаг 3b.6).
# Отдельная строка, чтобы отсутствие вопроса не читалось как сбой
SAVED_AUTO = "Записал сразу — разбор уверенный."

# Карточка подтверждения разбора (этап 3a). Ничего не сохраняется молча —
# инвариант проекта, поэтому путь «разобрал и сразу записал» не предусмотрен
CAPTURE_ASK = "Правильно понял?"
CAPTURE_UNCERTAIN = (
    "<i>В разборе не уверен — проверьте перед сохранением.</i>"
)
CAPTURE_CANCELLED = "Отменено, ничего не записал."
# Карточку нельзя обрезать: подтвердить можно только то, что видно целиком
CAPTURE_TOO_LONG = (
    "Разбор вышел слишком длинным, чтобы показать его целиком, — "
    "ничего не записал. Скажите короче или заведите запись через /new."
)
# Черновики живут в памяти и не переживают перезапуск бота
CAPTURE_STALE = "Эта карточка устарела — напишите фразу заново."
CAPTURE_ALIEN = "Это карточка другого чата."
CAPTURE_EMPTY = "Не понял, что записать. Попробуйте /new — там по шагам."
CAPTURE_FAILED = "Не смог разобрать фразу. Попробуйте /new — там по шагам."
# intent query и complete модель различает, но обработки у них не будет до
# следующего этапа. Молчать нельзя: к боту обратились явно
CAPTURE_NOT_YET = (
    "Это я пока не умею. План — /today и /week, поиск — /find, "
    "закрыть задачу — кнопкой в /tasks."
)

CAPTURE_RRULE_BAD = (
    "Повтор <code>{rule}</code> я не понял — запись сохранил, "
    "напоминание не создал."
)

# --- Этап 3b: запасной разбор, предупреждения и правка карточки --------------

# Разбор без модели беднее: типа записи он не знает (всегда «задача»),
# повторяемость не понимает, из фразы вытаскивает только дату и остаток текста.
# Про это надо сказать вслух, иначе человек подтвердит кнопкой худший разбор,
# думая, что видит обычный
CAPTURE_VIA_FALLBACK = (
    "<i>Разобрал без ИИ — только дату и текст. Проверьте внимательнее.</i>"
)
CAPTURE_RECURRING_FALLBACK = (
    "Модель сейчас недоступна, а повтор («каждый вторник») простым разбором "
    "не понять. Заведите запись через /new."
)

# Дата в прошлом — не ошибка разбора, а опечатка человека («12.03» в сентябре).
# Тикер отработал бы такое напоминание догонкой и выстрелил в ближайший тик
PAST_DATE = "⚠️ дата в прошлом"

CAPTURE_ASK_DATE = (
    "Ответьте на эту карточку новой датой — «завтра в 19:00», «через час», "
    "«в понедельник в 9:00». Чтобы убрать дату, ответьте «без даты»."
)
CAPTURE_ASK_TEXT = "Ответьте на эту карточку новым текстом записи."
CAPTURE_BAD_DATE = (
    "Не понял дату. Напишите словами: «завтра в 19:00», «через час», "
    "«в понедельник в 9:00»."
)
CAPTURE_BAD_TEXT = "Пустой текст записи не годится."
CAPTURE_EDIT_FAILED = "Не смог обновить карточку — попробуйте ещё раз."

# --- Этап 3b.6: /settings ----------------------------------------------------

SETTINGS_HEADER = (
    "⚙️ <b>Настройки семьи</b>\n\n"
    "🌍 Таймзона: <code>{tz}</code>\n"
    "☀️ Утренняя сводка: <code>{digest}</code>"
)
# Строки «режим прослушивания» на экране нет и не будет: режим `all` отменён
# 28.08.2026, режим у бота один. Но на что бот отзывается — сказать надо, иначе
# его молчание на обычную реплику читается как поломка, а не как задумка
SETTINGS_MODE_NOTE = (
    "<i>Бот отзывается только на обращение: команду, ответ на своё сообщение, "
    "упоминание или фразу с «+»; голосовое — после кнопки «🎤 Голосом». "
    "Обычную переписку он не читает и наружу не отправляет.</i>"
)
SETTINGS_ASK_TZ = (
    "Ответьте на это сообщение названием таймзоны — "
    "<code>Asia/Yekaterinburg</code>, <code>Europe/Moscow</code>."
)
SETTINGS_ASK_DIGEST = (
    "Ответьте на это сообщение временем утренней сводки — <code>08:00</code>."
)
SETTINGS_BAD_TZ = (
    "Такой таймзоны я не знаю. Нужно название из базы IANA: "
    "<code>Europe/Moscow</code>, <code>Asia/Yekaterinburg</code>."
)
SETTINGS_BAD_TIME = "Не понял время. Нужен формат <code>08:00</code>."
SETTINGS_SAVED = "Готово."

# --- Этап 5: голос -----------------------------------------------------------

# Голосовому недоступен ни один признак обращения из `IsTrigger`: текста у него
# нет, подписи Telegram к голосовым не даёт, остаётся кнопка-приглашение
VOICE_ASK = (
    "🎤 Слушаю. Запишите голосовое — разберу его как обычную фразу.\n"
    "<i>Приглашение на одно сообщение и на несколько минут; голосовые без "
    "кнопки я не слушаю.</i>"
)
VOICE_OFF = (
    "Расшифровка голоса выключена: не задан <code>STT_KEY</code>. "
    "Напишите фразу текстом с «+» или заведите запись через /new."
)
VOICE_FAILED = (
    "Не смог расшифровать запись. Попробуйте ещё раз или напишите текстом."
)
VOICE_TOO_LONG = (
    "Слишком длинная запись — я разбираю не длиннее {limit} секунд. "
    "Скажите короче или напишите текстом."
)


# Длину этого сообщения задаёт говорящий, а не разработчик, — значит нужен
# потолок: две минуты быстрой речи это тысячи символов, а отказ Telegram на
# 4096 стоил бы всего сообщения целиком. Эхо резать можно (в отличие от
# карточки): подтверждать по нему нечего, а полный текст уходит в разбор.
# Считается по **исходному** тексту, а экранирование раздувает его после
# обрезки — до впятеро (`&` → `&amp;`). Поэтому потолок взят с запасом на
# худший случай: 800 × 5 плюс рамка укладывается в MESSAGE_LIMIT при любом
# тексте, а мерить уже готовую строку и резать её нельзя — обрыв внутри
# HTML-сущности даёт «can't parse entities», то есть потерю всего сообщения.
# Запас невелик — на худшем случае остаётся 75 символов, — поэтому удлинение
# рамки «🎤 Услышал: «…»» требует и уменьшения потолка. Стережёт это
# `test_echo_survives_worst_case_escaping`
VOICE_ECHO_LIMIT = 800


# --- Этап 5п: разбор незакрытого --------------------------------------------

# Потолок такой же, как страница `/tasks`: у не влезших записей кнопок не
# будет, а пронумерованных строк обязано быть ровно столько же, сколько кнопок
MAX_REVIEW_ITEMS = 8

REVIEW_HEADER = "🧹 <b>Осталось незакрытым</b>"
REVIEW_HINT = "<i>Закрыть — ✅, перенести — 📅.</i>"
REVIEW_TAIL = (
    "<i>…и ещё {count} — покажу завтра, когда разберёте эти.</i>"
)
REVIEW_ALL_CLEAR = "🧹 Всё разобрано, незакрытого не осталось."
REVIEW_ASK_DAY = "Куда перенести «{title}»?"
REVIEW_ASK_REMIND = "Перенёс на {when}. Напомнить?"
REVIEW_MOVED = "Перенёс: {title} → {when}"
REVIEW_ASK_DATE = (
    "Ответьте на это сообщение днём — «в пятницу», «через неделю», "
    "«3 сентября». Время у записи останется прежним."
)
REVIEW_BAD_DATE = (
    "Не понял день. Напишите словами: «завтра», «в пятницу», «через неделю»."
)
# Кнопки живут в чате вечно, а запись за это время могли закрыть или перенести
REVIEW_STALE = "Этой записи в разборе больше нет."


# --- Карточка сохранённой записи (этап 7) ------------------------------------

ENTRY_HINT = "<i>Что с ней сделать?</i>"
ENTRY_ASK_TEXT = "Ответьте на это сообщение новым текстом записи."
ENTRY_ASK_DATE = (
    "Ответьте на это сообщение днём — «в пятницу», «через неделю», "
    "«3 сентября». Время у записи останется прежним, а у записи без срока "
    "получится «весь день». Чтобы убрать срок, ответьте «без даты»."
)
ENTRY_BAD_TEXT = "Пустой текст записи не годится."
ENTRY_BAD_DATE = (
    "Не понял день. Напишите словами: «завтра», «в пятницу», «через неделю»."
)
ENTRY_ASK_DELETE = "Удалить «{title}»? Вернуть можно будет сразу, но не потом."
ENTRY_DELETED = "Удалил: {title}"
ENTRY_RESTORED = "Вернул: {title}"
ENTRY_DATE_SAVED = "Срок теперь {when}."
ENTRY_DATE_CLEARED = "Убрал срок."
# Своё «устарело»: `DONE_ALREADY` говорит «уже закрыта» и про удалённую врёт,
# а `REVIEW_STALE` поминает разбор, которого здесь нет
ENTRY_GONE = "Этой записи больше нет."
ENTRY_EDIT_FAILED = "Не смог обновить карточку — попробуйте ещё раз."


def entry_ask_delete(title: str) -> str:
    return ENTRY_ASK_DELETE.format(title=_escape(title[:60]))


def entry_deleted(title: str) -> str:
    return ENTRY_DELETED.format(title=title[:60])


def entry_restored(title: str) -> str:
    return ENTRY_RESTORED.format(title=title[:60])


def review_tail(count: int) -> str:
    return REVIEW_TAIL.format(count=count)


def review_ask_day(title: str) -> str:
    return REVIEW_ASK_DAY.format(title=_escape(title[:60]))


def review_moved(title: str, when: str) -> str:
    return REVIEW_MOVED.format(title=_escape(title[:60]), when=when)


def voice_heard(text: str) -> str:
    """Что бот услышал — до разбора, отдельным сообщением (этап 5)."""
    shown = _escape(text[:VOICE_ECHO_LIMIT])
    tail = "…" if len(text) > VOICE_ECHO_LIMIT else ""
    return f"🎤 Услышал: «<i>{shown}{tail}</i>»"


def voice_too_long(limit: int) -> str:
    return VOICE_TOO_LONG.format(limit=limit)

REMIND_LINE = "🔔 {when}"
RRULE_LINE = "🔁 повторяется: <code>{rule}</code>"
SOURCE_LINK = '🔗 <a href="{url}">исходное сообщение</a>'

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


def settings_card(tz: str, digest: str) -> str:
    """Экран `/settings`. Оба значения задаём мы, а не человек."""
    head = SETTINGS_HEADER.format(tz=tz, digest=digest)
    return f"{head}\n\n{SETTINGS_MODE_NOTE}"


def capture_rrule_bad(rule: str) -> str:
    """Правило приходит от модели, а `<` в нём ломает отправку целиком."""
    return CAPTURE_RRULE_BAD.format(rule=_escape(rule))


def join_under_limit(blocks: list[str]) -> str:
    """Склеить блоки, не перерастив лимит Telegram.

    Резать готовое сообщение посередине нельзя: обрыв внутри HTML-тега
    превращает отправку в `can't parse entities`, то есть в потерю всего
    сообщения — ровно того исхода, от которого спасаемся. Поэтому лишние блоки
    отбрасываются целиком, а на их месте остаётся счётчик.

    Первый блок короткий (заголовок), так что пустым результат не бывает.
    """
    kept: list[str] = []
    for i, block in enumerate(blocks):
        if len("\n\n".join([*kept, block])) > MESSAGE_LIMIT:
            tail = MORE_ITEMS.format(count=len(blocks) - i)
            if len("\n\n".join([*kept, tail])) <= MESSAGE_LIMIT:
                kept.append(tail)
            break
        kept.append(block)
    return "\n\n".join(kept)


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


def _source_url(entry: Entry) -> str | None:
    """Ссылка на сообщение, из которого выросла запись, либо `None` (шаг 3a.7).

    Формат `t.me/c/<id без -100>/<message_id>` существует только у супергрупп и
    открывается только у тех, кто в чате состоит. У обычной группы ссылок на
    сообщения нет в принципе, поэтому строки в карточке тоже не будет.
    Записи мастера `/new` сюда не попадают: `source_message_id` он не пишет —
    у пошагового мастера нет одного «исходного» сообщения.
    """
    if not entry.source_chat_id or not entry.source_message_id:
        return None
    chat = str(entry.source_chat_id)
    if not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{entry.source_message_id}"


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
    url = _source_url(entry)
    if url:
        lines.append(SOURCE_LINK.format(url=url))
    lines.append(_author_suffix(entry, tz, now))
    return "\n".join(lines)


def is_past(item, tz: str, now: datetime | None = None) -> bool:
    """Срок разобранной записи уже прошёл (шаг 3b.2).

    Зовут двое: рендер карточки — чтобы поставить `⚠️`, и хендлер — чтобы
    сменить подпись кнопки на «Всё равно сохранить». Считать это в двух местах
    по-разному значит однажды показать предупреждение без кнопки или наоборот.

    У записи «на весь день» сравниваются **даты**, а не моменты. Срок у неё —
    локальная полночь, и посекундное сравнение объявляло бы просроченным
    обычное «купить молоко сегодня», сказанное днём.
    """
    if item.due_at is None:
        return False
    moment = now or tu.now_utc()
    if item.all_day:
        return item.due_at.date() < tu.to_local(moment, tz).date()
    return tu.to_utc(item.due_at, tz) < moment


def capture_card(items, tz: str, now: datetime | None = None, *, via: str = "llm") -> str:
    """Превью разбора до сохранения (шаг 3a.6).

    Отдельно от `entry_card`, потому что тот принимает уже сохранённый `Entry`
    с подгруженным автором, а здесь на руках только разбор — `parsing.Item`.

    Времена в `Item` локальные, а `fmt_due` ждёт UTC. Прогон через `to_utc` и
    обратно намеренный: так превью показывает ровно ту строку, какую покажет
    потом сохранённая запись, включая пограничные случаи перевода часов.
    """
    blocks = [CAPTURE_ASK]
    numbered = len(items) > 1
    for i, item in enumerate(items, start=1):
        head = f"{i}. " if numbered else ""
        blocks.append(head + _item_block(item, tz, now))
    if via != "llm":
        blocks.append(CAPTURE_VIA_FALLBACK)
    if any(item.uncertain for item in items):
        blocks.append(CAPTURE_UNCERTAIN)
    return "\n\n".join(blocks)


def _item_block(item, tz: str, now: datetime | None) -> str:
    icon = KIND_ICONS.get(item.kind, "•")
    name = KIND_NAMES.get(item.kind, item.kind)
    lines = [f"{icon} <b>{name}:</b> {_escape(item.title)}"]
    if item.body:
        lines.append(_escape(item.body))
    if item.due_at is not None:
        when = tu.fmt_due(
            tu.to_utc(item.due_at, tz), tz, all_day=item.all_day, now=now
        )
        lines.append(f"📅 {when}")
        if is_past(item, tz, now):
            lines.append(PAST_DATE)
    for moment in item.reminders:
        lines.append(
            REMIND_LINE.format(when=tu.fmt_due(tu.to_utc(moment, tz), tz, now=now))
        )
    if item.rrule:
        # Правило показывается как есть: человекочитаемый рендер RRULE — работа
        # отдельная, а карточка нужна ровно для того, чтобы повтор можно было
        # проверить глазами до сохранения
        lines.append(RRULE_LINE.format(rule=_escape(item.rrule)))
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


# --- Этап 4: покупки ----------------------------------------------------------

# Потолок пунктов в списке. Упирается не в символы, а в кнопки: Telegram берёт
# не больше 100 inline-кнопок, и каждому пункту нужна своя. 30 — это четыре
# компактных ряда по восемь, которые ещё видно на телефоне целиком.
MAX_LIST_ITEMS = 30

# Заголовок пункта режется на входе, до сохранения. Причина не косметическая:
# пункт, который не влезает в панель, нечем вычеркнуть — кнопки под ним нет, —
# а значит список никогда не станет полностью закрытым и залипнет навсегда.
# Резать надо сырой ввод: обрезка уже экранированного текста рвёт «&lt;» пополам
MAX_ITEM_TITLE = 100

LIST_HEADER = "🛒 <b>{name}</b>"
LIST_HINT = "<i>Тапните номер, чтобы вычеркнуть.</i>"
LIST_ALL_DONE = "<i>Всё куплено. Следующий список начнётся с /buy.</i>"
LIST_EMPTY = "<i>Список пуст.</i>"
BUY_USAGE = (
    "Что купить? Напишите так: <code>/buy молоко, хлеб, яйца</code>\n"
    "Потом тапайте по номерам, чтобы вычеркнуть купленное."
)
LIST_FULL = "В списке уже {limit} пунктов — вычеркните лишние или закройте список."
# Часть пунктов не поместилась. Молча отбросить их нельзя: человек набрал пять,
# увидит два и решит, что бот их проглотил
LIST_PARTIAL = (
    "Добавил только {added} — в списке потолок в {limit} пунктов. "
    "Не поместились: {dropped}. Вычеркните купленное или закройте список."
)
LIST_ITEM_GONE = "Этого пункта больше нет."
LIST_CLOSED = "Список закрыт. Следующий начнётся с /buy."
# Футер закрытого списка. Отдельно от `LIST_ALL_DONE`: там всё куплено, здесь
# остаток остался недокупленным, и молча показывать «Всё куплено» было бы
# враньём. Про кнопку сказано прямо: закрытие липкое, и без неё остаток
# недостижим
LIST_CLOSED_FOOTER = (
    "<i>Список закрыт, но куплено не всё. Верните его в работу кнопкой ниже или начните новый: /buy</i>"
)
BTN_CLOSE_LIST = "🧹 Список закрыт"
BTN_REOPEN_LIST = "↩️ Вернуть в работу"
LIST_REOPENED = "Список снова в работе."


def shopping_line(entry: Entry, tz: str, now: datetime | None = None) -> str:
    """Строка пункта списка покупок.

    Отдельно от `entry_line`, а не флагом к ней, по двум причинам. Первая: та
    подписывает **автора** (`_author_suffix`), а пункт 4.3 спрашивает «кто
    купил» — это `entry.closer`. Вторая: у открытого пункта подпись вообще
    лишняя, а в списке из тридцати строк она утраивает длину.

    Времени в подписи нет намеренно: `fmt_when` отдаёт относительную строку
    («15 минут назад»), которая меняется сама по себе, — а панель обязана
    совпадать сама с собой, иначе «message is not modified» перестанет
    отсеивать холостые правки. `done_at` при этом в базе пишется.
    """
    title = _escape(entry.title)
    if entry.status != "done":
        return f"☐ {title}"
    who = _escape(entry.closer.display_name) if entry.closer else "кто-то"
    return f"✅ {fmt.strikethrough(title)} — {fmt.italic(who)}"


def shopping_panel(
    name: str,
    entries,
    tz: str,
    now: datetime | None = None,
    *,
    limit: int = MAX_LIST_ITEMS,
    closed: bool = False,
) -> tuple[str, int]:
    """Текст панели списка и **число показанных пунктов**.

    Второе значение — контракт с клавиатурой: кнопок обязано быть ровно
    столько, сколько пронумерованных строк, иначе тап по «3» уйдёт не на тот
    пункт (тот же инвариант, что у `entry_list_keyboard`). Поэтому счёт
    возвращает тот, кто резал, а не тот, кто потом рисует кнопки.

    Режем целыми строками: заголовок пишет человек, `_escape` раздувает «<»
    вчетверо, и обрыв внутри «<s>» даёт `can't parse entities`, то есть потерю
    всей панели вместо потери одной строки.

    Футер считается **до** обрезки и им же меряется длина. Раньше мерили самым
    коротким вариантом, а подставить могли длинный — панель могла перерасти
    лимит на разницу между ними.
    """
    head = LIST_HEADER.format(name=_escape(name))
    if not entries:
        return head + "\n\n" + LIST_EMPTY, 0

    # «Всё куплено» важнее «список закрыт»: это тот же закрытый список, но
    # сообщение точнее. `LIST_CLOSED_FOOTER` остаётся случаю «закрыли кнопкой,
    # а остаток не купили» — там «Всё куплено» было бы враньём
    if all(e.status == "done" for e in entries):
        footer = LIST_ALL_DONE
    elif closed:
        footer = LIST_CLOSED_FOOTER
    else:
        footer = LIST_HINT

    shown: list[str] = []
    for i, entry in enumerate(entries[:limit], start=1):
        line = f"{i}. {shopping_line(entry, tz, now)}"
        probe = [head, *shown, line, MORE_ITEMS.format(count=len(entries)), footer]
        if len("\n".join(probe)) > MESSAGE_LIMIT:
            break
        shown.append(line)

    body = "\n".join(shown)
    if len(shown) < len(entries):
        body += "\n" + MORE_ITEMS.format(count=len(entries) - len(shown))

    return head + "\n\n" + body + "\n\n" + footer, len(shown)


def list_full(limit: int = MAX_LIST_ITEMS) -> str:
    return LIST_FULL.format(limit=limit)


def list_partial(added: int, dropped: int, limit: int = MAX_LIST_ITEMS) -> str:
    return LIST_PARTIAL.format(added=added, dropped=dropped, limit=limit)


# Сводка для дня и утреннего дайджеста. Без склонения «пункт/пункта/пунктов»:
# согласовывать число ради одной строки дороже, чем переписать её нейтрально
SHOPPING_SUMMARY = "🛒 <b>{name}</b> — осталось: {count}. Открыть: /buy"


def shopping_summary(name: str, count: int) -> str:
    return SHOPPING_SUMMARY.format(name=_escape(name), count=count)


# --- Этап 6: бэкапы и экспорт ---

# Здесь только то, что бот говорит в чат. Подписи колонок CSV и названия
# разделов Markdown лежат в `services/export.py`: это содержимое файла, а не
# сообщение, и HTML в нём быть не должно

BACKUP_CAPTION = (
    "Копия базы на {day}. Снята через <code>VACUUM INTO</code> — "
    "открывается любым просмотрщиком SQLite."
)
BACKUP_FAILED = "Не смог снять копию базы. Подробности в логе бота."
BACKUP_TOO_BIG = (
    "База выросла до {size} МБ — Telegram столько от бота не примет. "
    "Копии лежат в <code>data/backups/</code> на машине бота."
)

EXPORT_CAPTION = "Выгрузка на {day}: записей — {count}."
EXPORT_TOO_BIG = (
    "Выгрузка выросла до {size} МБ — Telegram столько от бота не примет. "
    "Заберите базу целиком: /backup."
)
EXPORT_EMPTY = "Выгружать нечего: в семье пока ни одной записи."


def backup_caption(day: date) -> str:
    return BACKUP_CAPTION.format(day=day.isoformat())


def backup_too_big(size_bytes: int) -> str:
    return BACKUP_TOO_BIG.format(size=round(size_bytes / 1024 / 1024, 1))


def export_too_big(size_bytes: int) -> str:
    return EXPORT_TOO_BIG.format(size=round(size_bytes / 1024 / 1024, 1))


def export_caption(day: date, count: int) -> str:
    return EXPORT_CAPTION.format(day=day.isoformat(), count=count)
