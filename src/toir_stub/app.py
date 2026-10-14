"""Заглушка ТОиР — отдельное приложение и отдельный процесс.

Изображает чужую систему: своя база `toir` под своей ролью, свой токен. Сервис ассистента
знает о ней только адрес и токен и ходит сюда по HTTP.

Заявка создаётся по новому ключу идемпотентности; по известному возвращается прежняя
с тем же идентификатором. Уникальность ключа держит ограничение в таблице, а не проверка
в коде: два одновременных запроса с одним ключом дают одну заявку, и второй получает её же.
"""

import hashlib
import json
import logging
import os
import secrets
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Engine, create_engine, text

log = logging.getLogger(__name__)

# Метка из .env.example: значение, которое `make run` заменяет случайным секретом.
PLACEHOLDER = "__GENERATE__"
MAX_KEY_LENGTH = 200

# Ключ блокировки на время создания схемы: два процесса заглушки не создают её наперегонки.
_SCHEMA_LOCK = 7_035_012
_SCHEMA = """
create schema if not exists toir;
create table if not exists toir.requests (
    id bigint generated always as identity primary key,
    idempotency_key text not null unique,
    -- Отпечаток тела: тот же ключ с другим телом — конфликт, а не прежняя заявка.
    body_hash text not null,
    equipment_id text not null,
    description text not null,
    priority smallint not null check (priority between 1 and 3),
    created_at timestamptz not null default now()
);
"""
_INSERT = text(
    """
    insert into toir.requests (idempotency_key, body_hash, equipment_id, description, priority)
    values (:key, :body_hash, :equipment_id, :description, :priority)
    on conflict (idempotency_key) do nothing
    returning id, idempotency_key, body_hash, equipment_id, description, priority, created_at
    """
)
_BY_KEY = text(
    "select id, idempotency_key, body_hash, equipment_id, description, priority, created_at"
    " from toir.requests where idempotency_key = :key"
)
_ALL = text(
    "select id, idempotency_key, body_hash, equipment_id, description, priority, created_at"
    " from toir.requests where cast(:key as text) is null or idempotency_key = :key order by id"
)


class ConfigError(Exception):
    """Заглушка не может стартовать; в тексте только имена переменных, значений нет."""


class RepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equipment_id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)
    priority: int = Field(ge=1, le=3, strict=True)

    @field_validator("equipment_id", "description")
    @classmethod
    def storable(cls, value: str) -> str:
        # Текст, который база не сохранит, — неверный запрос, а не ошибка сервера: иначе
        # сервис повторял бы его без конца. Одиночный суррогат pydantic отвергает сам.
        if "\x00" in value:
            raise ValueError("нулевой символ")
        return value


class StoredRequest(BaseModel):
    request_id: str
    idempotency_key: str
    equipment_id: str
    description: str
    priority: int
    created_at: datetime


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"не задана переменная окружения {name}")
    if PLACEHOLDER in value:
        raise ConfigError(
            f"в переменной {name} осталась метка из .env.example."
            " Создайте .env командой `make run` или впишите значение вручную"
        )
    return value


def create_app() -> FastAPI:
    """Точка входа процесса: настройки из окружения. С меткой вместо секрета не стартует."""
    database_url = _required(os.environ, "TOIR_DATABASE_URL")
    token = _required(os.environ, "TOIR_TOKEN")
    return build_app(create_engine(database_url, pool_pre_ping=True), token)


def ensure_schema(engine: Engine) -> None:
    """Схему заглушка создаёт сама, под своей ролью; повтор ничего не меняет."""
    with engine.begin() as connection:
        connection.execute(text("select pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK})
        connection.exec_driver_sql(_SCHEMA)


def _check_key(key: str | None) -> str:
    """Ключ идемпотентности: непустой, до `MAX_KEY_LENGTH` символов, без нулевого символа."""
    if not key or len(key) > MAX_KEY_LENGTH or "\x00" in key:
        raise HTTPException(
            status_code=400,
            detail=f"ключ идемпотентности: непустой, до {MAX_KEY_LENGTH} символов",
        )
    return key


def _body_hash(body: RepairRequest) -> str:
    canonical = json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _stored(row: Any) -> StoredRequest:
    return StoredRequest(
        request_id=f"REQ-{row.id:06d}",
        idempotency_key=row.idempotency_key,
        equipment_id=row.equipment_id,
        description=row.description,
        priority=row.priority,
        created_at=row.created_at,
    )


def build_app(engine: Engine, token: str) -> FastAPI:
    ensure_schema(engine)
    app = FastAPI(title="Заглушка ТОиР", docs_url=None, redoc_url=None, openapi_url=None)
    expected = token.encode()

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        # Схема — без учёта регистра, токен — за постоянное время; что именно не так,
        # отказ не сообщает.
        scheme, _, given = (authorization or "").strip().partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            given.strip().encode(), expected
        ):
            raise HTTPException(status_code=401, detail="нужен токен ТОиР")

    guarded = [Depends(authorized)]

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            with engine.connect() as connection:
                connection.execute(text("select 1 from toir.requests limit 1"))
        except Exception as error:
            # Проверку зовут каждые несколько секунд: без трассировки, одной строкой.
            log.warning("проверка готовности: база недоступна: %s", type(error).__name__)
            return JSONResponse(status_code=503, content={"status": "база недоступна"})
        return JSONResponse(content={"status": "ok"})

    @app.post("/requests", dependencies=guarded)
    async def create_request(
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        # Порядок проверок: токен (зависимость выше), ключ, тело. Тело разбирается здесь,
        # а не фреймворком: иначе неразборчивый JSON без токена получал бы 422, а не 401.
        key = _check_key(idempotency_key)
        try:
            body = RepairRequest.model_validate_json(await request.body())
        except ValidationError as error:
            details = jsonable_encoder(error.errors(include_input=False, include_url=False))
            return JSONResponse(status_code=422, content={"detail": details})
        return await run_in_threadpool(_create, key, body)

    def _create(idempotency_key: str, body: RepairRequest) -> JSONResponse:
        values = {"key": idempotency_key, "body_hash": _body_hash(body), **body.model_dump()}
        with engine.begin() as connection:
            created = connection.execute(_INSERT, values).first()
            # Ключ уже есть — в том числе от одновременного запроса, который успел раньше:
            # вставка дождалась его фиксации и ничего не сделала, строка уже видна.
            row = created or connection.execute(_BY_KEY, {"key": idempotency_key}).one()
        if row.body_hash != values["body_hash"]:
            raise HTTPException(
                status_code=409, detail="ключ идемпотентности уже использован с другим телом"
            )
        return JSONResponse(
            status_code=201 if created else 200,
            content=_stored(row).model_dump(mode="json"),
        )

    @app.get("/requests", dependencies=guarded)
    def list_requests(idempotency_key: str | None = None) -> list[StoredRequest]:
        """Все заявки по порядку создания; с `idempotency_key` — только заявка с этим ключом."""
        if idempotency_key is not None:
            _check_key(idempotency_key)
        with engine.connect() as connection:
            return [_stored(row) for row in connection.execute(_ALL, {"key": idempotency_key})]

    return app
