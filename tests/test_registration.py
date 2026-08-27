"""Авторегистрация семьи и участников — этап 0.7."""

from datetime import datetime

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

    from bot.handlers import new_entry, remind, views

    message = Message(
        message_id=1,
        date=datetime(2026, 8, 27, 12, 0),
        chat=Chat(id=555, type="private"),
        text="/new",
    )
    for module in (views, remind, new_entry):
        passed, _ = await module.router.message.check_root_filters(message)
        assert not passed, module.__name__
