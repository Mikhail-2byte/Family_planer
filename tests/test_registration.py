"""Авторегистрация семьи и участников — этап 0.7."""

import pytest

from bot.db import repo


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
