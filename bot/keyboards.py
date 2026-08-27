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


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TASKS)],
            [
                KeyboardButton(text=BTN_BUY),
                KeyboardButton(text=BTN_NOTES),
                KeyboardButton(text=BTN_NEW),
            ],
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


class CaptureCB(CallbackData, prefix="cap"):
    action: str  # 'save' | 'cancel'


def capture_keyboard() -> InlineKeyboardMarkup:
    """Кнопки под карточкой разбора.

    Идентификатора черновика в `callback_data` нет намеренно: ключом служит
    `message_id` сообщения, на котором висит кнопка, — он и так приезжает
    вместе с колбэком. Кнопки правки (`📅 Дата`, `🔀 Тип`, `✏️ Текст`) из
    `PLAN.md` появятся на этапе 3b.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BTN_SAVE, callback_data=CaptureCB(action="save").pack()
                ),
                InlineKeyboardButton(
                    text=BTN_CANCEL, callback_data=CaptureCB(action="cancel").pack()
                ),
            ]
        ]
    )


def entry_list_keyboard(
    entries, view: str, offset: int, total: int, page_size: int
) -> InlineKeyboardMarkup | None:
    """Ряд кнопок «закрыть N-ю запись» + навигация. None, если нечего показывать."""
    rows: list[list[InlineKeyboardButton]] = []

    if view == "tasks" and entries:
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
