"""Телеметрия хода: одна трасса на ход, спаны модели по соглашениям для генеративного ИИ,
ни одного секрета в атрибутах. Спаны собираются в памяти — без сети и приёмника.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from assistant import telemetry
from assistant.tools.schemas import SEARCH
from assistant.turn.agent import AgentHandler
from tests.conftest import Database
from tests.modelstub import ScriptedModel, call, final, tools
from tests.servicekit import MODEL_KEY, TOIR_TOKEN, make_service, new_session, settings
from tests.test_actions import (  # noqa: F401 — фикстуры заглушки ТОиР
    Toir,
    _confirm,
    _propose,
    toir,
    toir_engine,
)
from tests.test_actions import _service as action_service

pytestmark = pytest.mark.db

_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def _provider() -> None:
    # Глобальный провайдер ставится один раз на процесс; трассировщик модуля телеметрии —
    # заместитель, который начинает писать в него после установки.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    _EXPORTER.clear()
    yield _EXPORTER


def _turn(database: Database, model: ScriptedModel) -> str:
    handler = AgentHandler(
        database.service, model, settings(database.service_url, model=True).model
    )
    service = make_service(database.service, database.service_url, handler, model=True)
    reply = service.turn("u-eng", new_session(), "Что в памятке?")
    assert reply.status == 200, reply.body
    return str(reply.body["outcome"])


def _named(finished: tuple[ReadableSpan, ...], name: str) -> list[ReadableSpan]:
    return [item for item in finished if item.name == name]


def test_turn_is_one_trace_of_model_and_tool_spans(
    database: Database, small: Path, spans: InMemorySpanExporter
) -> None:
    model = ScriptedModel([tools(call(SEARCH, query="памятка")), final("ок")])
    assert _turn(database, model) == "answer"
    finished = spans.get_finished_spans()
    # Корень трассы — HTTP-запрос: его спан пишет сам FastAPI; ход — внутри него.
    (request,) = [item for item in finished if item.parent is None]
    assert request.name == "POST /turn" and request.context is not None
    assert {item.context.trace_id for item in finished if item.context} == {
        request.context.trace_id
    }
    (root,) = _named(finished, "turn")
    chats = _named(finished, "chat test-model")
    (tool,) = _named(finished, f"execute_tool {SEARCH}")
    assert len(chats) == 2
    assert root.context is not None
    for child in [*chats, tool]:
        assert child.parent is not None
        assert child.parent.span_id == root.context.span_id
    attributes = chats[0].attributes or {}
    assert attributes["gen_ai.operation.name"] == "chat"
    assert attributes["gen_ai.request.model"] == "test-model"
    assert attributes["gen_ai.usage.input_tokens"] == 100
    assert attributes["gen_ai.response.finish_reasons"] == ("tool_calls",)
    assert (tool.attributes or {})["gen_ai.tool.name"] == SEARCH
    assert (tool.attributes or {})["assistant.tool.status"] == "ok"
    root_attributes = root.attributes or {}
    assert root_attributes["assistant.outcome"] == "answer"
    assert root_attributes["assistant.model_calls"] == 2


def test_telemetry_carries_no_secrets_or_model_text(
    database: Database,
    small: Path,
    toir: Toir,  # noqa: F811 — фикстура из test_actions
    spans: InMemorySpanExporter,
) -> None:
    model = ScriptedModel([tools(call(SEARCH, query="памятка")), final("секретный ответ")])
    _turn(database, model)
    service = action_service(database, toir)
    assert _confirm(service, _propose(service)).status_code == 200
    finished = spans.get_finished_spans()
    assert _named(finished, "action confirm") and _named(finished, "POST /requests")
    dump = json.dumps(
        [{"name": item.name, "attributes": dict(item.attributes or {})} for item in finished],
        ensure_ascii=False,
        default=str,
    )
    for secret in (TOIR_TOKEN, MODEL_KEY, "секретный ответ", "памятка"):
        assert secret not in dump


def test_export_is_off_without_endpoint() -> None:
    assert telemetry.setup({}) is False
    assert telemetry.setup({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}) is False


def test_readiness_checks_are_not_traced() -> None:
    from opentelemetry.sdk.trace.sampling import Decision

    from assistant.telemetry import _SkipReadiness

    sampler = _SkipReadiness()
    assert sampler.should_sample(None, 1, "GET /healthz").decision is Decision.DROP
    by_path = sampler.should_sample(None, 1, "GET", attributes={"url.path": "/healthz"})
    assert by_path.decision is Decision.DROP
    assert sampler.should_sample(None, 1, "POST /turn").decision is Decision.RECORD_AND_SAMPLE
