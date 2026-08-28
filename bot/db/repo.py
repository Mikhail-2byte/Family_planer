"""Запросы к БД. Ничего не знает про aiogram — принимает сессию и данные."""

import logging
from datetime import date, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import sqlite as _sqlite  # noqa: F401  — регистрирует lower_unicode
from bot.db.models import Entry, Family, ListModel, Member, Reminder
from bot.services.timeutil import now_utc

# Сколько напоминаний забирает один тик. Если бот лежал месяц, не нужно тянуть
# из базы всё сразу: остаток догонится следующим тиком, а по факту схлопнется
# в одну сводку.
DUE_LIMIT = 200

log = logging.getLogger(__name__)


async def get_family(session: AsyncSession, chat_id: int) -> Family | None:
    return await session.scalar(select(Family).where(Family.chat_id == chat_id))


async def get_family_by_id(session: AsyncSession, family_id: int) -> Family | None:
    return await session.get(Family, family_id)


async def all_families(session: AsyncSession) -> list[Family]:
    result = await session.scalars(select(Family).order_by(Family.id))
    return list(result)


async def get_or_create_family(
    session: AsyncSession, chat_id: int, title: str | None = None
) -> Family:
    """Семья заводится сама по первому же сообщению — команды `/start` нет."""
    family = await get_family(session, chat_id)
    if family is None:
        family = Family(
            chat_id=chat_id,
            title=title,
            tz=settings.tz_default,
            digest_time=settings.digest_time,
        )
        session.add(family)
        await session.commit()
        await session.refresh(family)
    elif title and family.title != title:
        family.title = title
        await session.commit()
    return family


async def get_or_create_member(
    session: AsyncSession, family_id: int, tg_user_id: int, display_name: str
) -> Member:
    member = await session.scalar(
        select(Member).where(
            Member.family_id == family_id, Member.tg_user_id == tg_user_id
        )
    )
    if member is None:
        member = Member(
            family_id=family_id, tg_user_id=tg_user_id, display_name=display_name
        )
        session.add(member)
        await session.commit()
        await session.refresh(member)
    elif member.display_name != display_name:
        member.display_name = display_name
        await session.commit()
    return member


async def members_of(session: AsyncSession, family_id: int) -> list[Member]:
    result = await session.scalars(
        select(Member).where(Member.family_id == family_id).order_by(Member.joined_at)
    )
    return list(result)


async def create_entry(
    session: AsyncSession,
    *,
    family_id: int,
    author_id: int,
    kind: str,
    title: str,
    body: str | None = None,
    due_at: datetime | None = None,
    all_day: bool = False,
    list_id: int | None = None,
    position: int | None = None,
    assignee_id: int | None = None,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
) -> Entry:
    entry = Entry(
        family_id=family_id,
        author_id=author_id,
        kind=kind,
        title=title,
        body=body,
        due_at=due_at,
        all_day=all_day,
        list_id=list_id,
        position=position,
        assignee_id=assignee_id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def get_entry(session: AsyncSession, entry_id: int) -> Entry | None:
    return await session.get(Entry, entry_id)


async def entries_for_range(
    session: AsyncSession,
    family_id: int,
    start_utc: datetime,
    end_utc: datetime,
    *,
    statuses: tuple[str, ...] = ("open", "done"),
) -> list[Entry]:
    """Записи со сроком внутри [start, end). Отсортированы по времени."""
    result = await session.scalars(
        select(Entry)
        .where(
            Entry.family_id == family_id,
            Entry.status.in_(statuses),
            Entry.due_at >= start_utc,
            Entry.due_at < end_utc,
        )
        .order_by(Entry.all_day.desc(), Entry.due_at)
    )
    return list(result)


async def overdue_entries(
    session: AsyncSession, family_id: int, before_utc: datetime | None = None
) -> list[Entry]:
    result = await session.scalars(
        select(Entry)
        .where(
            Entry.family_id == family_id,
            Entry.status == "open",
            Entry.due_at.is_not(None),
            Entry.due_at < (before_utc or now_utc()),
        )
        .order_by(Entry.due_at)
    )
    return list(result)


async def entries_by_kind(
    session: AsyncSession,
    family_id: int,
    kind: str,
    *,
    status: str | None = "open",
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[Entry], int]:
    """Страница записей одного типа + общее число. Сначала со сроком, затем без."""
    where = [Entry.family_id == family_id, Entry.kind == kind]
    if status:
        where.append(Entry.status == status)

    total = await session.scalar(select(func.count()).select_from(Entry).where(*where))
    result = await session.scalars(
        select(Entry)
        .where(*where)
        .order_by(Entry.due_at.is_(None), Entry.due_at, Entry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result), int(total or 0)


SEARCH_LIMIT = 20


def _like_pattern(query: str) -> str:
    """`%` и `_` в запросе человека — обычные символы, а не подстановки.
    Без экранирования `/find %` находит вообще всё."""
    escaped = query.strip().lower()
    for char in ("\\", "%", "_"):
        escaped = escaped.replace(char, f"\\{char}")
    return f"%{escaped}%"


async def search_entries(
    session: AsyncSession, family_id: int, query: str, limit: int = SEARCH_LIMIT
) -> list[Entry]:
    """Поиск по заголовку и телу, без учёта регистра."""
    pattern = _like_pattern(query)
    result = await session.scalars(
        select(Entry)
        .where(
            Entry.family_id == family_id,
            func.lower_unicode(Entry.title).like(pattern, escape="\\")
            | func.lower_unicode(func.coalesce(Entry.body, "")).like(
                pattern, escape="\\"
            ),
        )
        .order_by(Entry.created_at.desc())
        .limit(limit)
    )
    return list(result)


async def complete_entry(
    session: AsyncSession, entry_id: int, family_id: int, member_id: int
) -> Entry | None:
    """Закрыть запись. `family_id` — чтобы колбэк из чужого чата ничего не тронул."""
    entry = await session.get(Entry, entry_id)
    if entry is None or entry.family_id != family_id or entry.status != "open":
        return None
    entry.status = "done"
    entry.done_at = now_utc()
    entry.done_by = member_id
    await session.commit()
    await session.refresh(entry)
    return entry


async def reschedule_entry(
    session: AsyncSession, entry_id: int, family_id: int, due_at: datetime
) -> Entry | None:
    """Перенести запись на другой срок. Контракт — как у `complete_entry`.

    Отдельная функция, а не флаг у `complete_entry`: та закрывает необратимо, и
    на её «уже закрыта → None» держится `DONE_ALREADY` в `/tasks`. Три причины
    вернуть `None` — нет записи, чужая семья, уже закрыта — неразличимы
    намеренно: звонящему во всех трёх случаях говорить одно и то же.

    Единственное место в проекте, меняющее `due_at` уже сохранённой записи.
    """
    entry = await session.get(Entry, entry_id)
    if entry is None or entry.family_id != family_id or entry.status != "open":
        return None
    entry.due_at = due_at
    await session.commit()
    await session.refresh(entry)
    return entry


async def entry_reminders(session: AsyncSession, entry_id: int) -> list[Reminder]:
    """Живые напоминания записи: активные и ещё не отработавшие."""
    result = await session.scalars(
        select(Reminder).where(
            Reminder.entry_id == entry_id,
            Reminder.active.is_(True),
            Reminder.sent_at.is_(None),
        )
    )
    return list(result)


async def entry_counts_by_author(
    session: AsyncSession, family_id: int
) -> dict[int, int]:
    rows = await session.execute(
        select(Entry.author_id, func.count())
        .where(Entry.family_id == family_id)
        .group_by(Entry.author_id)
    )
    return {author_id: count for author_id, count in rows}


async def create_reminder(
    session: AsyncSession,
    *,
    family_id: int,
    created_by: int,
    text: str,
    fire_at: datetime,
    entry_id: int | None = None,
    rrule: str | None = None,
) -> Reminder:
    reminder = Reminder(
        family_id=family_id,
        created_by=created_by,
        text=text,
        fire_at=fire_at,
        entry_id=entry_id,
        rrule=rrule,
    )
    session.add(reminder)
    await session.commit()
    await session.refresh(reminder)
    return reminder


async def due_reminders(
    session: AsyncSession, now: datetime | None = None, *, limit: int = DUE_LIMIT
) -> list[Reminder]:
    """Созревшие напоминания всех семей, самые старые первыми.

    Напоминания уже закрытой записи отсеиваются: `complete_entry` про
    `reminders` ничего не знает, поэтому иначе выполненная задача продолжала бы
    пинговать до самого срока.
    """
    moment = now or now_utc()
    result = await session.scalars(
        select(Reminder)
        .outerjoin(Entry, Reminder.entry_id == Entry.id)
        .where(
            Reminder.active.is_(True),
            Reminder.sent_at.is_(None),
            Reminder.fire_at <= moment,
            (Reminder.entry_id.is_(None)) | (Entry.status == "open"),
        )
        .order_by(Reminder.fire_at)
        .limit(limit)
    )
    return list(result)


async def mark_reminder_sent(
    session: AsyncSession, reminder: Reminder, sent_at: datetime | None = None
) -> None:
    """Закрыть разовое напоминание."""
    reminder.sent_at = sent_at or now_utc()
    await session.commit()


async def reschedule_reminder(
    session: AsyncSession, reminder: Reminder, fire_at: datetime
) -> None:
    """Сдвинуть повторяющееся напоминание на следующий срок.

    `sent_at` намеренно остаётся пустым: у повторяющегося признак «отработало»
    — это `fire_at` в будущем. Иначе пришлось бы усложнять выборку тикера.
    """
    reminder.fire_at = fire_at
    await session.commit()


async def deactivate_reminder(session: AsyncSession, reminder: Reminder) -> None:
    reminder.active = False
    await session.commit()


async def set_last_digest_on(
    session: AsyncSession, family: Family, day: date
) -> None:
    family.last_digest_on = day
    await session.commit()


async def set_family_settings(
    session: AsyncSession,
    family: Family,
    *,
    tz: str | None = None,
    digest_time: str | None = None,
) -> None:
    """Настройки семьи из `/settings` (шаг 3b.6). Меняется по одной за раз."""
    if tz is not None:
        family.tz = tz
    if digest_time is not None:
        family.digest_time = digest_time
    await session.commit()


async def set_panel(
    session: AsyncSession,
    family: Family,
    message_id: int | None,
    day: date | None,
) -> None:
    """Панель и её день меняются только вместе.

    Порознь перевыпуск теряет след: id без дня не даст понять, что панель
    осталась за вчера, а день без id — что редактировать.
    """
    family.panel_message_id = message_id
    family.panel_day = day
    await session.commit()


async def _family_is_empty(session: AsyncSession, family_id: int) -> bool:
    """У семьи нет ничего, кроме неё самой и автозаведённых участников."""
    for model in (Entry, Reminder, ListModel):
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.family_id == family_id)
        )
        if count:
            return False
    return True


async def migrate_family_chat_id(
    session: AsyncSession, old_chat_id: int, new_chat_id: int
) -> bool:
    """Группа стала супергруппой — у неё сменился chat_id (см. PLAN.md, п. 1d).

    Без этого бот просто замолчит: семья привязана к старому chat_id.

    Слепой `UPDATE ... WHERE chat_id = old` здесь недостаточен, и это выяснилось
    живым переездом 27.08. Telegram присылает разом и служебное сообщение о
    переезде, и апдейт о боте в новом чате, а aiogram обрабатывает апдейты
    параллельно — автосоздание успевает завести семью на новом `chat_id` раньше,
    и `UPDATE` падает на `UNIQUE constraint failed: families.chat_id`. Апдейт при
    этом теряется навсегда (offset Telegram уже сдвинут), а в базе остаются две
    семьи: старая со всей историей и пустая новая, которой и начинает жить бот.

    Поэтому пустышка на новом `chat_id` удаляется, а переезжает старая семья —
    вместе с записями, напоминаниями и участниками.
    """
    if old_chat_id == new_chat_id:
        return False

    family = await get_family(session, old_chat_id)
    if family is None:
        return False  # уже переехали: служебных сообщений о переезде приходит два

    stub = await get_family(session, new_chat_id)
    if stub is not None and stub.id != family.id:
        if not await _family_is_empty(session, stub.id):
            # Успела набрать данные — сливать их автоматически не рискуем:
            # тихая склейка двух семей хуже, чем громкий отказ
            log.error(
                "Переезд %s → %s: на новом chat_id уже есть семья #%s с данными",
                old_chat_id, new_chat_id, stub.id,
            )
            return False
        # Участники у пустышки — дубликаты настоящих, записей за ними нет
        await session.execute(delete(Member).where(Member.family_id == stub.id))
        await session.execute(delete(Family).where(Family.id == stub.id))

    family.chat_id = new_chat_id
    # Панель жила в старом чате, и её message_id в новом уже не наш
    family.panel_message_id = None
    family.panel_day = None
    # То же и с панелями списков покупок (этап 4): править сообщение по мёртвому
    # id значит на каждый тап получать «message to edit not found»
    await session.execute(
        update(ListModel)
        .where(ListModel.family_id == family.id)
        .values(panel_message_id=None)
    )
    await session.commit()
    return True


# --- Списки покупок (этап 4) --------------------------------------------------

# Список в интерфейсе один. Схема допускает несколько (`lists.name`) и второй
# вид (`kind='checklist'`), но и то и другое остаётся заделом: пока семье
# хватает одного «Покупки», выбор списка — лишний тап на каждое действие.
DEFAULT_LIST_NAME = "Покупки"


async def active_list(session: AsyncSession, family_id: int) -> ListModel | None:
    """Открытый список покупок семьи. Закрытые (archived) сюда не попадают."""
    return await session.scalar(
        select(ListModel)
        .where(
            ListModel.family_id == family_id,
            ListModel.kind == "shopping",
            ListModel.archived.is_(False),
        )
        .order_by(ListModel.id.desc())
    )


async def get_or_create_active_list(
    session: AsyncSession, family_id: int, name: str = DEFAULT_LIST_NAME
) -> ListModel:
    """Список заводится сам по первому обращению — как семья и участники (0.7)."""
    existing = await active_list(session, family_id)
    if existing is not None:
        return existing
    lst = ListModel(family_id=family_id, name=name, kind="shopping")
    session.add(lst)
    await session.commit()
    await session.refresh(lst)
    return lst


async def closed_list_with_leftovers(
    session: AsyncSession, family_id: int
) -> ListModel | None:
    """Свежий закрытый список, в котором остались непокупленные пункты.

    Нужен, чтобы закрытый список не пропадал бесследно: `/buy` показывает его с
    кнопкой «вернуть в работу». Без этого пункты, до которых не дошли руки,
    становятся недостижимы — из кнопки список не открыть, а тап по старой
    панели его не оживит (оживляет только снятая галка, а её там нет).
    """
    return await session.scalar(
        select(ListModel)
        .join(Entry, Entry.list_id == ListModel.id)
        .where(
            ListModel.family_id == family_id,
            ListModel.kind == "shopping",
            ListModel.archived.is_(True),
            Entry.status == "open",
        )
        .order_by(ListModel.id.desc())
        .limit(1)
    )


async def shopping_slot(session: AsyncSession, family_id: int) -> tuple[int, int]:
    """`list_id` и `position` для новой покупки, заведённой не через `/buy`.

    Покупка обязана попасть в список, откуда бы она ни пришла — из мастера
    `/new` или из разбора «+». Иначе она не видна нигде: `/buy` смотрит на
    `list_id`, день и «Просрочено» — на `due_at` (а его у покупки чаще нет),
    `/tasks` и `/notes` — на `kind`. Такая запись находилась только через
    `/find`, то есть терялась сразу после «Записал.».

    Закрытый список для этого не оживляем: новая покупка начинает новый список,
    а старый остаётся закрытым — так и задумано.
    """
    lst = await get_or_create_active_list(session, family_id)
    last = await session.scalar(
        select(func.max(Entry.position)).where(Entry.list_id == lst.id)
    )
    return lst.id, (last or 0) + 1


async def list_items(session: AsyncSession, list_id: int) -> list[Entry]:
    """Пункты списка в порядке добавления.

    Сортировка по `position`, а `id` — тай-брейк: `position` заполняем мы сами,
    и при гонке двух добавлений два пункта могут получить одинаковый номер.
    Без второго ключа их порядок между перерисовками плавал бы, а вместе с ним
    поехала бы и нумерация кнопок.
    """
    result = await session.scalars(
        select(Entry)
        .where(Entry.list_id == list_id)
        .order_by(Entry.position, Entry.id)
    )
    return list(result)


async def add_items(
    session: AsyncSession,
    *,
    family_id: int,
    author_id: int,
    list_id: int,
    titles: list[str],
) -> list[Entry]:
    """Добавить пункты в конец списка.

    Срок (`due_at`) пунктам не ставится намеренно: на нём держится то, что
    список не протекает в `/today`, `/week`, дайджест и панель дня — все они
    выбирают записи по непустому `due_at`.
    """
    last = await session.scalar(
        select(func.max(Entry.position)).where(Entry.list_id == list_id)
    )
    position = (last or 0) + 1
    created = []
    for title in titles:
        created.append(
            await create_entry(
                session,
                family_id=family_id,
                author_id=author_id,
                kind="shopping",
                title=title,
                list_id=list_id,
                position=position,
            )
        )
        position += 1
    return created


async def toggle_bought(
    session: AsyncSession, entry_id: int, family_id: int, member_id: int
) -> Entry | None:
    """Переключить «куплено» у пункта списка. Чужую семью не трогает.

    Отдельно от `complete_entry`, а не флагом к ней: та закрывает необратимо
    («уже закрыта» → `None`), и на этом держится `DONE_ALREADY` в `/tasks`.
    Здесь наоборот — повторный тап обязан снимать галку, иначе промах пальцем в
    магазине не откатить.

    Снятие обнуляет `done_at` и `done_by`: иначе «купила Аня» осталось бы
    висеть на пункте, который никто не покупал.
    """
    entry = await session.get(Entry, entry_id)
    if entry is None or entry.family_id != family_id or entry.list_id is None:
        return None
    if entry.status == "open":
        entry.status = "done"
        entry.done_at = now_utc()
        entry.done_by = member_id
    else:
        entry.status = "open"
        entry.done_at = None
        entry.done_by = None
    await session.commit()
    await session.refresh(entry)
    return entry


async def sync_list_archived(session: AsyncSession, lst: ListModel) -> bool:
    """Закрыть список, если в нём не осталось открытых пунктов.

    **Только в одну сторону** — закрывает, но никогда не открывает. Обратный ход
    делает `reopen_list`, и только когда тап снял галку. Симметричный пересчёт
    выглядел проще, но стирал явное закрытие: список, закрытый кнопкой с
    недокупленным остатком, оживал от первого же тапа по этому остатку, и
    кнопка «Список закрыт» не значила ничего.

    Пустой список закрытым не считается — иначе только что созданный сразу
    уезжал бы в архив.
    """
    if lst.archived:
        return True
    total = await session.scalar(
        select(func.count()).select_from(Entry).where(Entry.list_id == lst.id)
    )
    open_left = await session.scalar(
        select(func.count())
        .select_from(Entry)
        .where(Entry.list_id == lst.id, Entry.status == "open")
    )
    if total and not open_left:
        lst.archived = True
        await session.commit()
    return lst.archived


async def reopen_list(session: AsyncSession, lst: ListModel) -> None:
    """Вернуть список в работу: снятая галка отменяет промах пальцем.

    Зовётся ровно из одного места — тапа, который перевёл пункт в «открыт».
    Тап в другую сторону (купили ещё один) закрытый список не оживляет: человек
    закрыл его сознательно.
    """
    if lst.archived:
        lst.archived = False
        await session.commit()


async def close_list(session: AsyncSession, lst: ListModel) -> None:
    """Закрыть список явно, не вычёркивая остаток («сметаны не было»)."""
    if not lst.archived:
        lst.archived = True
        await session.commit()


async def set_list_panel(
    session: AsyncSession, lst: ListModel, message_id: int | None
) -> None:
    """Запомнить сообщение панели списка — она переживает перезапуск бота."""
    lst.panel_message_id = message_id
    await session.commit()


async def list_with_panel(
    session: AsyncSession, family_id: int, list_id: int
) -> ListModel | None:
    """Список по id с проверкой семьи — для колбэков из чата."""
    lst = await session.get(ListModel, list_id)
    if lst is None or lst.family_id != family_id:
        return None
    return lst
