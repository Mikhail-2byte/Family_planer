"""Авторегистрация семьи и участников — этап 0.7."""

from datetime import date, datetime

import pytest

from bot.db import repo
from tests.conftest import FakeMessage


@pytest.mark.asyncio
async def test_family_created_once(session):
    a = await repo.get_or_create_family(session, -1001, "Семья")
    b = await repo.get_or_create_family(session, -1001, "Семья")
    assert a.id == b.id
    assert a.tz and a.listen_mode == "trigger"


@pytest.mark.asyncio
async def test_members_are_unique_per_family(session):
    family = await repo.get_or_create_family(session, -1001, "Семья")
    await repo.get_or_create_member(session, family.id, 111, "Миша")
    await repo.get_or_create_member(session, family.id, 111, "Миша")
    await repo.get_or_create_member(session, family.id, 222, "Аня")
    assert len(await repo.members_of(session, family.id)) == 2


@pytest.mark.asyncio
async def test_display_name_is_refreshed(session):
    family = await repo.get_or_create_family(session, -1001)
    await repo.get_or_create_member(session, family.id, 111, "Миша")
    member = await repo.get_or_create_member(session, family.id, 111, "Михаил")
    assert member.display_name == "Михаил"


@pytest.mark.asyncio
async def test_group_migrated_to_supergroup(session):
    family = await repo.get_or_create_family(session, -1001, "Семья")
    assert await repo.migrate_family_chat_id(session, -1001, -100200300)

    assert await repo.get_family(session, -1001) is None
    moved = await repo.get_family(session, -100200300)
    assert moved is not None and moved.id == family.id


@pytest.mark.asyncio
async def test_migration_survives_autocreated_family_at_the_new_chat_id(session):
    """Гонка живого переезда 27.08.2026: пустышка на новом chat_id.

    Telegram шлёт разом служебное сообщение о переезде и апдейт о боте в новом
    чате, а aiogram обрабатывает апдейты параллельно — автосоздание успевает
    завести семью на новом `chat_id` раньше. Слепой `UPDATE` падал на
    `UNIQUE constraint failed`, апдейт терялся (offset уже сдвинут), и в базе
    оставались две семьи: старая со всей историей и пустая новая, которой и
    начинал жить бот.
    """
    old = await repo.get_or_create_family(session, -1001, "Семья")
    anya = await repo.get_or_create_member(session, old.id, 222, "Аня")
    await repo.create_entry(
        session, family_id=old.id, author_id=anya.id, kind="task", title="Молоко"
    )
    # Автосоздание опередило служебное сообщение
    stub = await repo.get_or_create_family(session, -100200300, "Семья")
    await repo.get_or_create_member(session, stub.id, 222, "Аня")
    assert stub.id != old.id

    assert await repo.migrate_family_chat_id(session, -1001, -100200300)

    moved = await repo.get_family(session, -100200300)
    assert moved is not None and moved.id == old.id  # переехала настоящая семья
    assert await repo.get_family(session, -1001) is None
    assert len(await repo.all_families(session)) == 1  # пустышки больше нет
    assert len(await repo.members_of(session, old.id)) == 1
    entries, total = await repo.entries_by_kind(session, old.id, "task")
    assert total == 1 and entries[0].title == "Молоко"


@pytest.mark.asyncio
async def test_migration_keeps_hands_off_a_new_family_with_data(session):
    """Если на новом chat_id уже есть семья с записями — отказ, а не склейка.

    Случай почти невозможный, но тихо слить две семьи хуже, чем громко отказать:
    сливать пришлось бы участников с дедупликацией, и ошибка здесь необратима.
    """
    old = await repo.get_or_create_family(session, -1001, "Семья")
    other = await repo.get_or_create_family(session, -100200300, "Чужая")
    member = await repo.get_or_create_member(session, other.id, 333, "Миша")
    await repo.create_entry(
        session, family_id=other.id, author_id=member.id, kind="task", title="Чужое"
    )

    assert not await repo.migrate_family_chat_id(session, -1001, -100200300)
    assert len(await repo.all_families(session)) == 2
    assert (await repo.get_family(session, -1001)).id == old.id


@pytest.mark.asyncio
async def test_migration_drops_the_panel_of_the_old_chat(session):
    """message_id панели принадлежал старому чату — в новом он уже не наш."""
    family = await repo.get_or_create_family(session, -1001, "Семья")
    await repo.set_panel(session, family, 158, date(2026, 8, 27))

    await repo.migrate_family_chat_id(session, -1001, -100200300)

    moved = await repo.get_family(session, -100200300)
    assert moved.panel_message_id is None and moved.panel_day is None


@pytest.mark.asyncio
async def test_second_migration_message_is_harmless(session):
    """Служебных сообщений о переезде приходит два — из старого чата и нового."""
    await repo.get_or_create_family(session, -1001, "Семья")
    assert await repo.migrate_family_chat_id(session, -1001, -100200300)
    assert not await repo.migrate_family_chat_id(session, -1001, -100200300)
    assert len(await repo.all_families(session)) == 1


@pytest.mark.asyncio
async def test_migration_of_unknown_chat_is_noop(session):
    assert not await repo.migrate_family_chat_id(session, -999, -100999)


# --- Приветствие: вместе с ним приходит нижняя клавиатура --------------------


@pytest.mark.asyncio
async def test_greeting_carries_the_main_keyboard(family, bot):
    """Иначе клавиатуры в чате нет вообще, пока кто-нибудь не наберёт /today."""
    from bot import keyboards as kb
    from bot import texts
    from bot.handlers.admin import bot_added

    await bot_added(None, bot, family)

    assert bot.sent == [(family.chat_id, texts.GREETING)]
    assert bot.kwargs[0]["reply_markup"] == kb.main_keyboard()


# --- Личка: бот обязан отвечать на что угодно, а не только на /ping ----------


def test_in_private_matches_only_private_chats():
    """Промах в другую сторону означал бы отказ «я работаю в группе»
    на каждое сообщение семьи."""
    from bot.filters import IN_PRIVATE

    assert IN_PRIVATE.resolve(FakeMessage(chat_type="private"))
    assert not IN_PRIVATE.resolve(FakeMessage(chat_type="supergroup"))
    assert not IN_PRIVATE.resolve(FakeMessage(chat_type="group"))


@pytest.mark.asyncio
async def test_any_private_message_gets_an_answer():
    """Кнопка START шлёт /start, а команды-инициализации у бота нет —
    без ловушки бот в личке молчит и выглядит сломанным."""
    from bot import texts
    from bot.handlers.admin import private_chat

    for text in ("/start", "привет", "/ping"):
        message = FakeMessage(text, chat_type="private", chat_id=555)
        await private_chat(message)
        assert message.texts == [texts.PRIVATE_CHAT]


@pytest.mark.asyncio
async def test_group_routers_never_run_in_private():
    """Ловушка личка-на-всё стоит в первом роутере и безопасна ровно потому,
    что у соседей на `message` висит IN_GROUP. Потеряет кто-то этот фильтр —
    его хендлеры молча уйдут в отказ, и найти это будет нечем."""
    from aiogram.types import Chat, Message

    from bot.handlers import capture, lists, new_entry, remind, views

    message = Message(
        message_id=1,
        date=datetime(2026, 8, 27, 12, 0),
        chat=Chat(id=555, type="private"),
        text="/new",
    )
    for module in (views, lists, remind, new_entry, capture):
        passed, _ = await module.router.message.check_root_filters(message)
        assert not passed, module.__name__
