"""Модели таблиц. Схему создаёт миграция; соответствие моделей и схемы проверяет тест.

Три схемы — по тому, чьи это данные в продукте: `kb` — индекс документов из СЭД,
`dwh` — внешнее хранилище данных заказчика, `app` — собственные данные приложения.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    FetchedValue,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ACTION_STATUSES = ("proposed", "confirming", "done", "rejected", "failed")
# Разрешённые переходы действия; всё остальное отклоняет триггер таблицы.
ACTION_TRANSITIONS = frozenset(
    {
        ("proposed", "confirming"),
        ("proposed", "rejected"),
        ("confirming", "done"),
        ("confirming", "failed"),
    }
)
OUTCOMES = ("answer", "refusal", "no_data", "action", "budget_exceeded", "error")


# Значение ставит база: после вставки модель читает его оттуда, а не считает пустым.
_BY_DB = FetchedValue()


class Base(DeclarativeBase):
    pass


class DatasetVersion(Base):
    __tablename__ = "dataset_version"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    singleton: Mapped[bool] = mapped_column(primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_BY_DB)


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    position_title: Mapped[str] = mapped_column(Text)


class UserGroup(Base):
    __tablename__ = "user_groups"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    user_id: Mapped[str] = mapped_column(ForeignKey("app.users.user_id"), primary_key=True)
    group_name: Mapped[str] = mapped_column(Text, primary_key=True)


class KpiGrant(Base):
    __tablename__ = "kpi_grants"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    user_id: Mapped[str] = mapped_column(ForeignKey("app.users.user_id"), primary_key=True)
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    site: Mapped[str] = mapped_column(Text, primary_key=True)


class RepairGrant(Base):
    __tablename__ = "repair_grants"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    user_id: Mapped[str] = mapped_column(ForeignKey("app.users.user_id"), primary_key=True)
    site: Mapped[str] = mapped_column(Text, primary_key=True)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    user_id: Mapped[str] = mapped_column(ForeignKey("app.users.user_id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Отметка занятости: срок и случайная метка захвата; обе пусты, когда сессия свободна.
    busy_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_token: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_BY_DB)


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    turn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=_BY_DB)
    model_calls: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    max_call_input_tokens: Mapped[int] = mapped_column(Integer)
    prefix_hash: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer)
    # Запись хода для диагностики: то, что видела модель от имени автора.
    record: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_BY_DB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_BY_DB)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
    turn_id: Mapped[str] = mapped_column(ForeignKey("app.turns.turn_id"))
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    chunk_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=_BY_DB)


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = {"schema": "app"}  # noqa: RUF012

    action_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("app.users.user_id"))
    session_id: Mapped[str] = mapped_column(Text)
    turn_id: Mapped[str] = mapped_column(ForeignKey("app.turns.turn_id"))
    tool: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=_BY_DB)
    # Создаётся базой вместе со строкой и больше не меняется; после вставки читается из базы.
    idempotency_key: Mapped[UUID] = mapped_column(server_default=_BY_DB)
    request_id: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_BY_DB)
    # Меняется триггером при каждом переходе.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=_BY_DB, server_onupdate=_BY_DB
    )


class Production(Base):
    __tablename__ = "production"
    __table_args__ = {"schema": "dwh"}  # noqa: RUF012

    row_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    shift: Mapped[int] = mapped_column(SmallInteger)
    site: Mapped[str] = mapped_column(Text)
    tonnes: Mapped[int] = mapped_column(Integer)


class Downtime(Base):
    __tablename__ = "downtime"
    __table_args__ = {"schema": "dwh"}  # noqa: RUF012

    row_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    equipment_id: Mapped[str] = mapped_column(Text)
    site: Mapped[str] = mapped_column(Text)
    # Время местное, без часового пояса — как в наборе.
    start: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    reason: Mapped[str] = mapped_column(Text)


class Equipment(Base):
    __tablename__ = "equipment"
    __table_args__ = {"schema": "dwh"}  # noqa: RUF012

    row_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    equipment_id: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    site: Mapped[str] = mapped_column(Text)
    motor_hours: Mapped[int] = mapped_column(Integer)
    last_ppr_motor_hours: Mapped[int] = mapped_column(Integer)


class Document(Base):
    """Реестр идентификаторов: роль сервиса читает его только через функцию базы."""

    __tablename__ = "documents"
    __table_args__ = {"schema": "kb"}  # noqa: RUF012

    doc_id: Mapped[str] = mapped_column(Text, primary_key=True)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = {"schema": "kb"}  # noqa: RUF012

    chunk_id: Mapped[str] = mapped_column(Text, primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("kb.documents.doc_id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    section_no: Mapped[int] = mapped_column(Integer)
    part_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    doc_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    acl: Mapped[list[str]] = mapped_column(ARRAY(Text))
    supersedes: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[str | None] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    # Идентификаторы и числа без стемминга: ищутся точным совпадением.
    terms: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=_BY_DB)
    # Считает база: название, заголовок раздела и текст, «ё» приведена к «е».
    tsv: Mapped[Any] = mapped_column(TSVECTOR, server_default=_BY_DB)
