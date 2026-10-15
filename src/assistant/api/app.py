"""Приложение сервиса ассистента: маршруты, пользователь из заголовка, ошибки."""

import logging
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import Engine, text

from assistant import telemetry
from assistant.actions.confirm import (
    Actions,
    Conflict,
    Forbidden,
    NotFound,
    StoreResult,
    ToirFailed,
    store_result,
)
from assistant.actions.toir import ToirClient, Transport, http_transport
from assistant.api.schemas import (
    ActionDone,
    ActionRejected,
    Citation,
    PendingAction,
    TurnRequest,
    TurnResponse,
    Usage,
)
from assistant.config import Settings, ToirSecret, load_settings, load_toir_secret
from assistant.db.migrate import head_revision
from assistant.db.session import service_engine, transaction
from assistant.model.openai_client import OpenAICompatibleClient
from assistant.turn.agent import AgentHandler
from assistant.turn.runner import (
    INTERNAL_ERROR,
    NotReadyHandler,
    Runtime,
    SessionBusy,
    TurnHandler,
    run_turn,
)

log = logging.getLogger(__name__)


class TurnNotStarted(Exception):
    """Ход не начат из-за сбоя до его начала; ответ всё равно по контракту."""


_USER_EXISTS = text("select 1 from app.users where user_id = :user_id")
_TURN_RECORD = text(
    "select turn_id, session_id, question, outcome, answer, citations, model_calls,"
    " input_tokens, cached_input_tokens, output_tokens, max_call_input_tokens, prefix_hash,"
    " latency_ms, record, created_at from app.turns where turn_id = :turn_id"
)
_DATASET = text("select content_hash from app.dataset_version")
_USERS = text("select user_id, name, position_title from app.users order by user_id")
# Страница для демонстрации: один файл без сборки, логики прав в нём нет.
_PAGE = Path(__file__).with_name("page.html")
_READY = text(
    "select (select version_num from public.alembic_version),"
    " (select count(*) from app.dataset_version)"
)


def _public_url(url: str) -> str:
    """Адрес провайдера без учётных данных в адресе и без параметров запроса."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def default_runtime(settings: Settings, engine: Engine, handler: TurnHandler) -> Runtime:
    return Runtime(
        settings=settings,
        engine=engine,
        handler=handler,
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
        new_turn_id=lambda: f"t-{secrets.token_hex(6)}",
        new_token=uuid4,
    )


def create_app() -> FastAPI:
    """Точка входа процесса: настройки из окружения. С меткой вместо секрета не стартует."""
    settings = load_settings(os.environ)
    secret = load_toir_secret(os.environ)
    telemetry.setup(os.environ)
    engine = service_engine(settings.database_url)
    return build_app(default_runtime(settings, engine, make_handler(settings, engine)), secret)


def make_handler(settings: Settings, engine: Engine) -> TurnHandler:
    """Без адреса и имени модели сервис стартует, а ход честно отвечает `error`."""
    if not settings.model.configured:
        return NotReadyHandler(settings)
    model = settings.model
    client = OpenAICompatibleClient(
        model.base_url,
        model.api_key,
        model.name,
        max_output_tokens=model.max_output_tokens,
        timeout=model.timeout_seconds,
        extra_body=model.extra_body,
    )
    return AgentHandler(engine, client, model)


def build_app(
    runtime: Runtime,
    toir_secret: ToirSecret,
    toir_transport: Transport | None = None,
    store: StoreResult = store_result,
) -> FastAPI:
    """Приложение. Транспорт до ТОиР и запись результата подставляются тестами."""
    app = FastAPI(title="Ассистент сотрудника", docs_url=None, redoc_url=None, openapi_url=None)
    engine = runtime.engine
    # Секрет ТОиР уходит только в модуль действий: ходу и модели его не передают.
    transport = toir_transport or http_transport(runtime.settings.toir_url)
    actions = Actions(engine, ToirClient(transport, toir_secret.token), store)
    # Последняя версия схемы постоянна на всю жизнь процесса: читается один раз.
    schema_head = head_revision()

    def authenticate(x_user_id: str | None) -> str:
        """Одна проверка пользователя на все маршруты. Заголовок объявлен необязательным
        и проверяется здесь: обязательный фреймворк отклонил бы с кодом 422, а нужен 401.
        Хода от имени «по умолчанию» не бывает.
        """
        if not x_user_id:
            raise HTTPException(status_code=401, detail="нет заголовка X-User-ID")
        with transaction(engine, x_user_id) as connection:
            known = connection.execute(_USER_EXISTS, {"user_id": x_user_id}).first()
        if known is None:
            raise HTTPException(status_code=401, detail="неизвестный пользователь")
        return x_user_id

    def current_user(x_user_id: Annotated[str | None, Header()] = None) -> str:
        return authenticate(x_user_id)

    def turn_user(x_user_id: Annotated[str | None, Header()] = None) -> str:
        # Зависимость выполняется раньше проверки полей тела: без заголовка и для
        # неизвестного пользователя ответ — 401, какими бы ни были поля. Тело, которое
        # не разбирается как JSON вовсе, фреймворк отклоняет ещё раньше. Если база
        # недоступна уже здесь, клиент получает ответ по контракту, а не 500.
        try:
            return authenticate(x_user_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise TurnNotStarted from exc

    user = Annotated[str, Depends(current_user)]

    def not_started() -> TurnResponse:
        # Идентификатор выдан, но хода в базе нет: записать его было некуда.
        return TurnResponse(
            turn_id=runtime.new_turn_id(),
            outcome="error",
            answer=INTERNAL_ERROR,
            usage=Usage(prefix_hash=runtime.handler.prefix_hash),
        )

    @app.exception_handler(TurnNotStarted)
    def turn_not_started(request: Request, exc: TurnNotStarted) -> JSONResponse:
        log.error("ход не начат", exc_info=exc.__cause__)
        return JSONResponse(content=not_started().model_dump())

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Где ошибка и какая — без самого ввода: возвращать его незачем, а текст, который
        # не кодируется в UTF-8, сломал бы и этот ответ.
        details = [
            {
                "loc": [str(part) for part in error["loc"]],
                "msg": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Трассировка — в журнал; клиенту — только код и общая фраза.
        log.error("необработанная ошибка на %s", request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "внутренняя ошибка сервиса"})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Готовность для compose: база отвечает, схема на последней версии, набор загружен."""
        try:
            with engine.connect() as connection:
                version, datasets = connection.execute(_READY).one()
        except Exception:
            log.exception("проверка готовности: база недоступна")
            return JSONResponse(status_code=503, content={"status": "база недоступна"})
        if version != schema_head:
            return JSONResponse(status_code=503, content={"status": "схема не той версии"})
        if not datasets:
            return JSONResponse(status_code=503, content={"status": "набор не загружен"})
        return JSONResponse(content={"status": "ok"})

    @app.get("/", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        """Чат для демонстрации. Всё, что он показывает, приходит из маршрутов ниже."""
        return HTMLResponse(_PAGE.read_text(encoding="utf-8"))

    @app.get("/users")
    def users() -> JSONResponse:
        """Список пользователей для выбора на странице. Пользователь в этом сервисе задаётся
        доверенным заголовком `X-User-ID`, поэтому список прав не открывает: полномочий и
        групп в нём нет — только идентификатор, ФИО и должность."""
        with engine.connect() as connection:
            rows = connection.execute(_USERS).all()
        return JSONResponse(
            content=[{"user_id": row[0], "name": row[1], "position": row[2]} for row in rows]
        )

    @app.get("/turns/{turn_id}")
    def turn_record(turn_id: str, user_id: user) -> JSONResponse:
        """Запись хода для диагностики — только автору: чужой ход неотличим от несуществующего.

        В записи — то, что видела модель от имени автора, и ничего, что не прошло его права.
        """
        with transaction(engine, user_id) as connection:
            row = connection.execute(_TURN_RECORD, {"turn_id": turn_id}).first()
        if row is None:
            raise HTTPException(status_code=404, detail="нет такого хода")
        body = dict(row._mapping)
        body["created_at"] = body["created_at"].isoformat()
        return JSONResponse(content=body)

    @app.get("/info")
    def info() -> JSONResponse:
        """Сведения о сборке для прогона: на чём проверялось."""
        try:
            with engine.connect() as connection:
                dataset = connection.scalar(_DATASET)
        except Exception:
            log.exception("сведения о сборке: база недоступна")
            dataset = None
        model = runtime.settings.model
        return JSONResponse(
            content={
                "commit": os.environ.get("GIT_COMMIT", ""),
                "dataset_hash": dataset,
                "prefix_hash": runtime.handler.prefix_hash,
                "model": model.name,
                "model_base_url": _public_url(model.base_url),
                "sampling": {
                    "temperature": 0,
                    "max_output_tokens": model.max_output_tokens,
                    "response_format": model.response_format,
                },
            }
        )

    @app.post("/turn")
    def turn(body: TurnRequest, user_id: Annotated[str, Depends(turn_user)]) -> TurnResponse:
        if len(body.message) > runtime.settings.max_message_chars:
            raise HTTPException(
                status_code=422,
                detail=f"сообщение длиннее {runtime.settings.max_message_chars} символов",
            )
        try:
            finished = run_turn(runtime, user_id, body.session_id, body.message)
        except SessionBusy:
            raise HTTPException(status_code=409, detail="в сессии уже идёт ход") from None
        except Exception:
            # До тела хода дело не дошло — например, база недоступна при захвате сессии.
            log.exception("ход не начат")
            return not_started()
        result, usage = finished.result, finished.usage
        pending = (
            PendingAction(
                action_id=finished.action_id, tool=result.action.tool, params=result.action.params
            )
            if result.action is not None and finished.action_id is not None
            else None
        )
        return TurnResponse(
            turn_id=finished.turn_id,
            outcome=result.outcome,  # type: ignore[arg-type]
            answer=result.answer,
            citations=[
                Citation(doc_id=item.doc_id, chunk_id=item.chunk_id) for item in result.citations
            ],
            pending_action=pending,
            usage=Usage(
                model_calls=usage.model_calls,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                max_call_input_tokens=usage.max_call_input_tokens,
                prefix_hash=result.prefix_hash,
                latency_ms=finished.latency_ms,
            ),
        )

    def refused(error: Exception) -> HTTPException:
        # Текст отказа — категория, без параметров заявки и без ответа ТОиР.
        codes = ((NotFound, 404), (Forbidden, 403), (Conflict, 409), (ToirFailed, 502))
        status = next(code for kind, code in codes if isinstance(error, kind))
        detail = {
            404: "нет такого действия",
            403: "действие подтверждает его автор, пока у него есть полномочие",
            409: "действие уже отклонено или подтверждено",
            502: "ТОиР недоступен или отверг заявку; подтверждение можно повторить",
        }[status]
        return HTTPException(status_code=status, detail=detail)

    @app.post("/actions/{action_id}/confirm")
    def confirm(action_id: str, user_id: user) -> ActionDone:
        try:
            return ActionDone(request_id=actions.confirm(action_id, user_id))
        except (NotFound, Forbidden, Conflict, ToirFailed) as error:
            raise refused(error) from None

    @app.post("/actions/{action_id}/reject")
    def reject(action_id: str, user_id: user) -> ActionRejected:
        try:
            actions.reject(action_id, user_id)
        except (NotFound, Forbidden, Conflict) as error:
            raise refused(error) from None
        return ActionRejected()

    return app
