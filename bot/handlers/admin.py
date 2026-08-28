import logging
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter, Command
from aiogram.types import BufferedInputFile, ChatMemberUpdated, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_PRIVATE
from bot.services import backup, export
from bot.services import timeutil as tu

router = Router()
log = logging.getLogger(__name__)

# У ботов лимит загрузки 50 МБ. Берём с запасом: правило «сообщение, размер
# которого задаёт не разработчик, обязано иметь потолок» распространяется и на
# файлы, а проваленная отправка выглядит как поломка бота
MAX_UPLOAD_BYTES = 45 * 1024 * 1024


# Фильтр группы обязателен: тот же переход `kicked → member` прилетает из лички,
# когда человек разблокировал бота, а там middleware не кладёт `family`
@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER), IN_GROUP)
async def bot_added(event: ChatMemberUpdated, bot: Bot, family: Family) -> None:
    """Бота добавили в группу — здороваемся один раз."""
    # Клавиатуру прикладываем к первому же сообщению: иначе до первого
    # `/today` её в чате нет вообще, и половина бота выглядит несуществующей
    await bot.send_message(
        family.chat_id, texts.GREETING, reply_markup=kb.main_keyboard()
    )
    log.info("Добавлен в чат %s (семья #%s)", family.chat_id, family.id)


@router.message(Command("ping"), IN_GROUP)
async def cmd_ping(message: Message, session: AsyncSession, family: Family) -> None:
    members = await repo.members_of(session, family.id)
    await message.answer(texts.pong(family.title or str(family.chat_id), len(members)))


@router.message(Command("backup"), IN_GROUP)
async def cmd_backup(message: Message, family: Family) -> None:
    """Свежая копия базы файлом в чат (шаг 6.2).

    Снимок берётся заново, а не отдаётся вчерашний из `data/backups/`: человек,
    попросивший копию, ждёт копию на сейчас. Кладётся во временный каталог —
    ручной снимок не должен вмешиваться в ротацию ежедневных.

    Оговорка, которую стоит знать: в чат уезжает **весь** файл базы, то есть
    данные всех семей, какие в нём есть. Изоляция по `family_id`, на которой
    держится остальной бот, здесь не работает по природе вещей — бот на одну
    семью, и это осознанное упрощение. У `/export` изоляция соблюдена.
    """
    today = tu.local_today(family.tz)
    workdir = Path(tempfile.mkdtemp(prefix="familybackup-"))
    try:
        dest = workdir / f"family-{today.isoformat()}.db"
        try:
            await backup.snapshot(dest)
        except Exception:
            log.exception("Ручной бэкап не снят")
            await message.answer(texts.BACKUP_FAILED)
            return

        size = dest.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            await message.answer(texts.backup_too_big(size))
            return

        await message.answer_document(
            FSInputFile(dest), caption=texts.backup_caption(today)
        )
    finally:
        # Копия базы — не тот файл, который стоит забывать во временном каталоге
        shutil.rmtree(workdir, ignore_errors=True)


@router.message(Command("export"), IN_GROUP)
async def cmd_export(
    message: Message, session: AsyncSession, family: Family
) -> None:
    """Все записи семьи двумя файлами: Markdown и CSV (шаг 6.3).

    Файлы собираются в памяти — временных файлов и уборки за ними не нужно
    вовсе. Обрезки нет: выгрузка едет файлом, а не сообщением, и урезанный
    экспорт хуже большого.
    """
    entries = await repo.all_entries(session, family.id)
    if not entries:
        await message.answer(texts.EXPORT_EMPTY, reply_markup=kb.main_keyboard())
        return

    today = tu.local_today(family.tz)
    stem = f"family-{today.isoformat()}"
    title = family.title or "Семья"

    markdown = export.to_markdown(entries, family.tz, title, today)
    table = export.to_csv(entries, family.tz)

    # Обрезать выгрузку нельзя — по урезанной ничего не восстановишь, — но и
    # молча упереться в лимит Telegram она не должна: упавшая отправка съест
    # апдейт целиком, и человек не увидит вовсе ничего
    biggest = max(len(markdown), len(table))
    if biggest > MAX_UPLOAD_BYTES:
        await message.answer(texts.export_too_big(biggest))
        return

    await message.answer_document(
        BufferedInputFile(markdown, f"{stem}.md"),
        caption=texts.export_caption(today, len(entries)),
    )
    await message.answer_document(BufferedInputFile(table, f"{stem}.csv"))


# Последним в модуле и без других фильтров: в личке бот отвечает одно и то же
# на что угодно. Кнопка START шлёт `/start`, а команды-инициализации у бота нет
# (инвариант «Никакого /start»), — без этого хендлера бот в личке просто молчит.
# Ловушка стоит в первом роутере, но соседей не перехватывает: у `views`,
# `remind` и `new_entry` на `message` висит IN_GROUP, в личке они не срабатывают
# в принципе. Потеряет кто-то из них этот фильтр — его хендлеры молча уйдут
# сюда; на этот случай есть тест `test_group_routers_never_run_in_private`.
# Клавиатуру не прикладываем: все её кнопки ведут в групповые хендлеры.
@router.message(IN_PRIVATE)
async def private_chat(message: Message) -> None:
    await message.answer(texts.PRIVATE_CHAT)
