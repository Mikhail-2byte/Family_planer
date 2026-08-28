from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_TODAY = "📅 Сегодня"
BTN_BUY = "🛒 Покупки"
BTN_TASKS = "✅ Задачи"
BTN_NOTES = "📝 Заметки"
BTN_NEW = "➕ Новое"
BTN_VOICE = "🎤 Голосом"


def main_keyboard() -> ReplyKeyboardMarkup:
    # Три ряда по двое, а не два ряда: шести подписей с эмодзи в два ряда на
    # телефоне уже не хватает ширины — три в одном ряду не помещались и на пяти
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TASKS)],
            [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_NOTES)],
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_VOICE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


BTN_SAVE = "✅ Сохранить"
BTN_CANCEL = "❌ Отмена"


class PageCB(CallbackData, prefix="page"):
    view: str  # 'tasks' | 'notes'
    offset: int


class DoneCB(CallbackData, prefix="done"):
    entry_id: int
    offset: int


BTN_SAVE_ANYWAY = "✅ Всё равно сохранить"
BTN_EDIT_DATE = "📅 Дата"
BTN_EDIT_KIND = "🔀 Тип"
BTN_EDIT_TEXT = "✏️ Текст"


class CaptureCB(CallbackData, prefix="cap"):
    # 'save' | 'cancel' | 'date' | 'text' | 'kind' | 'setkind' | 'back'
    action: str
    # Заполняется только у 'setkind'. Отдельным полем, а не «kind:task» внутри
    # `action`: двоеточие у aiogram — разделитель полей, и такое значение
    # отвергается на упаковке
    kind: str = ""


def capture_keyboard(
    *, warn: bool = False, editable: bool = True
) -> InlineKeyboardMarkup:
    """Кнопки под карточкой разбора.

    Идентификатора черновика в `callback_data` нет намеренно: ключом служит
    `message_id` сообщения, на котором висит кнопка, — он и так приезжает
    вместе с колбэком.

    `warn` — срок записи уже прошёл (шаг 3b.2). Подпись «Всё равно сохранить»
    и есть то самое «явное подтверждение» из `PLAN.md`: отдельный шаг «вы
    уверены?» стоил бы лишний тап и не добавил бы ни бита информации.

    `editable` — показывать ли кнопки правки. Они появляются только у карточки
    с одной записью: у нескольких пришлось бы класть в `callback_data` номер
    элемента и рисовать по ряду кнопок на каждый — втрое больше кода ради
    случая «купи молока и хлеба, но дату поправь только у хлеба».
    """
    rows = [
        [
            InlineKeyboardButton(
                text=BTN_SAVE_ANYWAY if warn else BTN_SAVE,
                callback_data=CaptureCB(action="save").pack(),
            ),
            InlineKeyboardButton(
                text=BTN_CANCEL, callback_data=CaptureCB(action="cancel").pack()
            ),
        ]
    ]
    if editable:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text=BTN_EDIT_DATE, callback_data=CaptureCB(action="date").pack()
                ),
                InlineKeyboardButton(
                    text=BTN_EDIT_KIND, callback_data=CaptureCB(action="kind").pack()
                ),
                InlineKeyboardButton(
                    text=BTN_EDIT_TEXT, callback_data=CaptureCB(action="text").pack()
                ),
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Подписи те же, что у мастера /new, — тип записи человек выбирает одинаково
# независимо от того, каким путём она заводится
KIND_BUTTONS = [
    ("✅ Задача", "task"),
    ("📝 Заметка", "note"),
    ("📅 Событие", "event"),
    ("🛒 Покупка", "shopping"),
]


def capture_kind_keyboard() -> InlineKeyboardMarkup:
    """Выбор типа вместо обычных кнопок карточки (шаг 3b.4).

    Отдельный экран, а не перебор типов по кругу: перебором человек не видит,
    какие варианты вообще есть, и промахивается мимо нужного.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=CaptureCB(action="setkind", kind=kind).pack(),
                )
                for text, kind in KIND_BUTTONS[:2]
            ],
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=CaptureCB(action="setkind", kind=kind).pack(),
                )
                for text, kind in KIND_BUTTONS[2:]
            ],
            [
                InlineKeyboardButton(
                    text="← Назад", callback_data=CaptureCB(action="back").pack()
                )
            ],
        ]
    )


def entry_list_keyboard(
    entries, view: str, offset: int, total: int, page_size: int
) -> InlineKeyboardMarkup | None:
    """Ряд кнопок «закрыть N-ю запись» + навигация. None, если нечего показывать."""
    rows: list[list[InlineKeyboardButton]] = []

    if entries:
        # Заметки закрываются той же кнопкой, что и задачи. Без неё они копились
        # бы вечно: закрыть запись умеет только этот колбэк, а заметка с
        # прошедшим сроком вдобавок навсегда оседала в блоке «Просрочено».
        # Кнопок ровно столько же, сколько пронумерованных строк в тексте:
        # рассинхрон номеров молча уводит «Готово» не на ту запись
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ {i}",
                    callback_data=DoneCB(entry_id=e.id, offset=offset).pack(),
                )
                for i, e in enumerate(entries[:page_size], start=1)
            ]
        )

    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="←",
                callback_data=PageCB(
                    view=view, offset=max(0, offset - page_size)
                ).pack(),
            )
        )
    if offset + page_size < total:
        nav.append(
            InlineKeyboardButton(
                text="→",
                callback_data=PageCB(view=view, offset=offset + page_size).pack(),
            )
        )
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --- Этап 5п: разбор незакрытого ---------------------------------------------


class ReviewCB(CallbackData, prefix="rev"):
    """Весь шаг переноса едет в самой кнопке — ни FSM, ни словаря не нужно.

    Отдельный класс, а не поле в `DoneCB`: у кнопок, уже висящих в чате,
    `callback_data` вида `done:42:0`, и лишнее поле сделало бы их
    неразбираемыми — тап по старому списку молча перестал бы работать.
    """

    # 'done' | 'move' | 'day' | 'other' | 'rem' | 'norem' | 'back'
    action: str
    entry_id: int
    # Смысл зависит от действия: у 'day' — сдвиг в днях от сегодня,
    # у 'rem' — за сколько минут напомнить
    value: int = 0


BTN_REVIEW_DONE = "✅"
BTN_REVIEW_MOVE = "📅"

# Сдвиг считается от сегодня, а не от старого срока: разбираемая запись уже
# просрочена, и «завтра» человек имеет в виду завтра, а не «через день после
# того, как оно должно было случиться»
REVIEW_DAYS = [("Завтра", 1), ("Послезавтра", 2), ("Через неделю", 7)]

# Варианты те же, что на шаге напоминания в мастере `/new`, и по той же
# причине разведены: у записи «на весь день» срок — полночь, и «за 15 минут»
# означало бы 23:45 накануне
REVIEW_REMIND = [("За 15 минут", 15), ("За час", 60), ("В момент", 0)]
REVIEW_REMIND_ALLDAY = [("Утром в 09:00", 0), ("Накануне вечером", 840)]
BTN_REVIEW_NO_REMIND = "Без напоминания"
BTN_REVIEW_OTHER_DAY = "🗓 Другая дата"
BTN_REVIEW_BACK = "← Назад"


def review_keyboard(entries) -> InlineKeyboardMarkup | None:
    """По ряду на запись: закрыть или перенести.

    Кнопок ровно столько, сколько пронумерованных строк в тексте, — иначе
    номера в списке и номера на кнопках разъедутся.
    """
    if not entries:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{BTN_REVIEW_DONE} {i}",
                    callback_data=ReviewCB(action="done", entry_id=e.id).pack(),
                ),
                InlineKeyboardButton(
                    text=f"{BTN_REVIEW_MOVE} {i}",
                    callback_data=ReviewCB(action="move", entry_id=e.id).pack(),
                ),
            ]
            for i, e in enumerate(entries, start=1)
        ]
    )


def review_day_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=ReviewCB(
                        action="day", entry_id=entry_id, value=days
                    ).pack(),
                )
                for title, days in REVIEW_DAYS
            ],
            [
                InlineKeyboardButton(
                    text=BTN_REVIEW_OTHER_DAY,
                    callback_data=ReviewCB(action="other", entry_id=entry_id).pack(),
                ),
                InlineKeyboardButton(
                    text=BTN_REVIEW_BACK,
                    callback_data=ReviewCB(action="back", entry_id=entry_id).pack(),
                ),
            ],
        ]
    )


def review_remind_keyboard(entry_id: int, *, all_day: bool) -> InlineKeyboardMarkup:
    options = REVIEW_REMIND_ALLDAY if all_day else REVIEW_REMIND
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=ReviewCB(
                        action="rem", entry_id=entry_id, value=minutes
                    ).pack(),
                )
                for title, minutes in options
            ],
            [
                InlineKeyboardButton(
                    text=BTN_REVIEW_NO_REMIND,
                    callback_data=ReviewCB(action="norem", entry_id=entry_id).pack(),
                )
            ],
        ]
    )
