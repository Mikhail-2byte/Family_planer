import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter, Command
from aiogram.types import BufferedInputFile, ChatMemberUpdated, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_PRIVATE
from bot.services import backup, export, llm, parse_log, sending
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


@router.message(Command("help"), IN_GROUP)
async def cmd_help(message: Message) -> None:
    """Что бот умеет и когда слушает.

    Выше ловушки `private_chat` — как и все хендлеры этого модуля. Команды в
    личке всё равно не работают: `IN_GROUP` уводит их в ловушку, а она отвечает
    «работаю только в группе», что и есть правильный ответ.
    """
    await message.answer(texts.HELP, reply_markup=kb.main_keyboard())


@router.message(Command("ping"), IN_GROUP)
async def cmd_ping(message: Message, session: AsyncSession, family: Family) -> None:
    members = await repo.members_of(session, family.id)
    await message.answer(texts.pong(family.title or str(family.chat_id), len(members)))


@router.message(Command("ai"), IN_GROUP)
async def cmd_ai(message: Message, family: Family, bot: Bot) -> None:
    """Живая проверка связи с ИИ (этап 12).

    Заведена после 01.09.2026: провайдер моргнул, разбор ушёл на `dateparser`,
    и узнать «сломан ИИ или нет» было неоткуда — в чате обе беды выглядели
    одинаково, а лог лежит на машине бота.

    Запрос настоящий и идёт тем же путём, что разбор: та же цепочка моделей,
    тот же прокси, тот же `response_format`. Проверка по конфигу («ключ задан»)
    не поймала бы ровно тот случай, ради которого команда написана.

    Ответ занимает секунды, поэтому первым уходит «Проверяю…», а отчёт
    приезжает правкой того же сообщения: два сообщения на команду — лишний шум
    в чате, который потом ещё и убирать утренней уборке.
    """
    probe = await message.answer(texts.AI_CHECKING)
    limit = settings.llm_daily_limit
    history = True  # показывать ли строку «последний отказ»

    if parse_log.quota_spent():
        # Живого запроса не делаем: разбор в этом состоянии мы тоже не
        # отправляем, а иначе командой обходился бы собственный суточный лимит
        blocks = [texts.ai_quota(parse_log.calls_today(), limit)]
    else:
        answer = await llm.ask(llm.PROBE_SYSTEM, llm.PROBE_USER)
        parse_log.write(
            event="probe",
            via="llm",
            model=answer.model or "-",
            chat=message.chat.id,
            ok=answer.ok,
            reason=answer.reason,
            detail=answer.detail,
            tried=list(answer.tried),
            calls=answer.calls,
            seconds=round(answer.elapsed, 1),
        )
        calls = parse_log.calls_today()
        if answer.ok:
            blocks = [texts.ai_ok(answer.model, answer.elapsed, calls, limit)]
            if answer.tried and answer.model != answer.tried[0]:
                blocks.append(texts.ai_after_fallback(answer.tried[0]))
        else:
            blocks = [
                texts.ai_down(
                    answer.reason,
                    answer.tried,
                    answer.elapsed,
                    answer.detail,
                    calls,
                    limit,
                )
            ]
            # Историю отказов не показываем: свежайший из них — эта же проба,
            # и строка «последний отказ: только что» повторила бы блок выше
            history = False

    if history:
        blocks.append(_last_failure_line(family.tz))
    # Голос проверяем по конфигу, живого запроса в Groq не делаем: аудио для
    # пробы нет, а пустой файл Whisper-эндпоинт отвергает 400 — рабочий ключ от
    # нерабочего это не отличило бы, зато стоило бы запроса из чужой квоты
    blocks.append(
        texts.ai_voice(settings.stt_model) if settings.stt_key else texts.AI_VOICE_OFF
    )
    report = "\n\n".join(blocks)

    if probe and await sending.edit(bot, family, probe.message_id, report) == sending.OK:
        return
    await message.answer(report)


def _last_failure_line(tz: str) -> str:
    """Когда ИИ отказывал в последний раз — по `parse.log`."""
    record = parse_log.last_failure()
    if record is None:
        return texts.AI_NO_FAILURES
    try:
        # В логе наивный UTC, а показываем в таймзоне семьи
        moment = datetime.fromisoformat(str(record.get("at")))
    except (TypeError, ValueError):
        return texts.AI_NO_FAILURES
    return texts.ai_last_failure(tu.fmt_when(moment, tz), str(record.get("reason", "")))


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
    """Все записи семьи тремя файлами: Markdown, CSV и календарь.

    Файлы собираются в памяти — временных файлов и уборки за ними не нужно
    вовсе. Обрезки нет: выгрузка едет файлом, а не сообщением, и урезанный
    экспорт хуже большого.

    `.ics` — то, что осталось от отменённой интеграции с Google Календарём:
    события видны в любом календаре, а OAuth и хранения токенов не нужно.
    В нём только записи со сроком, поэтому он бывает пустым при непустых
    остальных двух — тогда его просто не отправляем.
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
    calendar = export.to_ics(entries, family.tz, title, tu.now_utc())
    dated = sum(
        1 for e in entries if e.due_at is not None and e.status != "archived"
    )

    # Обрезать выгрузку нельзя — по урезанной ничего не восстановишь, — но и
    # молча упереться в лимит Telegram она не должна: упавшая отправка съест
    # апдейт целиком, и человек не увидит вовсе ничего
    biggest = max(len(markdown), len(table), len(calendar))
    if biggest > MAX_UPLOAD_BYTES:
        await message.answer(texts.export_too_big(biggest))
        return

    await message.answer_document(
        BufferedInputFile(markdown, f"{stem}.md"),
        caption=texts.export_caption(today, len(entries)),
    )
    await message.answer_document(BufferedInputFile(table, f"{stem}.csv"))
    if dated:
        # Пустой календарь (ни одной записи со сроком) не шлём: файл валиден, но
        # человеку он говорит только «что-то пошло не так»
        await message.answer_document(
            BufferedInputFile(calendar, f"{stem}.ics"),
            caption=texts.export_ics_caption(dated),
        )


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
