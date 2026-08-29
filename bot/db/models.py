"""Схема БД.

Все datetime хранятся в UTC и **без tzinfo** — SQLite таймзоны не знает.
Таймзона семьи (`Family.tz`) применяется только на границе: при разборе ввода
и при выводе. Конвертация живёт в `bot/services/timeutil.py` (этап 1).
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Family(Base):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    tz: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    digest_time: Mapped[str] = mapped_column(String(5), default="08:00")
    last_digest_on: Mapped[date | None] = mapped_column(Date)
    # Колонка мёртвая: второй режим ('all' — гнать каждое сообщение в LLM на
    # триаж) отменён 28.08.2026, значение всегда 'trigger'. Не удалена потому,
    # что в SQLite это пересборка таблицы `families` на боевой базе — той самой,
    # где переезд в супергруппу уже стоил потерянного апдейта, — ради нуля
    # функциональной выгоды. Снять поле только с модели нельзя: тесты строят
    # схему через `create_all`, и `revision --autogenerate` перестал бы быть пустым
    listen_mode: Mapped[str] = mapped_column(String(16), default="trigger")
    panel_message_id: Mapped[int | None] = mapped_column(Integer)
    # Локальный день, за который выпущена панель. Именно день, а не момент:
    # сравнение идёт с `local_today`, ровно как у `last_digest_on`
    panel_day: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("family_id", "tg_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"), index=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String(255))
    # Заложено на случай отъезда в другой пояс (риск №5), пока не используется
    tz_override: Mapped[str | None] = mapped_column(String(64))
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ListModel(Base):
    __tablename__ = "lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="shopping")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Живая панель списка (этап 4). Своя, а не `families.panel_message_id`:
    # та занята панелью дня, и обе висят в чате одновременно.
    # Близнеца `panel_day` тут нет намеренно — у списка покупок нет понятия
    # «устарел за сутки», перевыпуск ему нужен, только когда он уехал вверх
    panel_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Entry(Base):
    """Одна таблица на task/note/event/shopping — тип записи в поле `kind`."""

    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    kind: Mapped[str] = mapped_column(String(16), index=True)

    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)

    list_id: Mapped[int | None] = mapped_column(ForeignKey("lists.id"), index=True)
    position: Mapped[int | None] = mapped_column(Integer)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))

    # Ссылка на исходное сообщение в группе
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    done_by: Mapped[int | None] = mapped_column(ForeignKey("members.id"))

    # Кто завёл — показывается в каждом списке, поэтому грузим сразу,
    # иначе на каждую строку списка уходит отдельный запрос
    author: Mapped[Member] = relationship(lazy="selectin", foreign_keys=[author_id])
    closer: Mapped[Member | None] = relationship(lazy="selectin", foreign_keys=[done_by])
    # Кому поручено. Колонка `assignee_id` лежала в схеме с самой первой
    # ревизии и всё это время была мёртвой — связь к ней добавлена вместе с
    # кнопкой «👤 Кому» в карточке записи. Миграции не требует: `relationship`
    # схему не меняет, и `test_schema_matches_the_models` это подтверждает
    assignee: Mapped[Member | None] = relationship(
        lazy="selectin", foreign_keys=[assignee_id]
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"), index=True)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("entries.id"), index=True)
    text: Mapped[str] = mapped_column(String(500))
    fire_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    rrule: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(ForeignKey("members.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
