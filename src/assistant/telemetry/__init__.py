"""Телеметрия хода: трассы, метрики и журнал по OTLP. Основание — решения 5 и 9.

Без адреса приёмника (`OTEL_EXPORTER_OTLP_ENDPOINT`) ничего не включается: API OpenTelemetry
по умолчанию ничего не делает, и ход от телеметрии не зависит. Экспорт идёт в фоне
пакетами; недоступный приёмник ход не задерживает и не ломает.

Что попадает в атрибуты. Спаны вызовов модели — по соглашениям OpenTelemetry для
генеративного ИИ (`gen_ai.*`): модель, адрес провайдера без учётных данных, токены, причина
остановки. Текста входа и ответа модели в телеметрии нет: в нём данные набора, доступные
одному пользователю, а у приёмника своих прав нет. Секретов — ключа модели и `TOIR_TOKEN` —
этот модуль не получает.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

__all__ = ["Span", "SpanKind", "set_attributes", "setup", "span"]

SERVICE_NAME = "oes-assistant"
_SCOPE = "assistant"

tracer = trace.get_tracer(_SCOPE)
_meter = metrics.get_meter(_SCOPE)
# Метрики клиента генеративного ИИ — имена и единицы из соглашений OpenTelemetry.
token_usage = _meter.create_histogram(
    "gen_ai.client.token.usage", unit="{token}", description="Токены одного вызова модели"
)
operation_duration = _meter.create_histogram(
    "gen_ai.client.operation.duration", unit="s", description="Длительность вызова модели"
)
turns = _meter.create_counter("assistant.turns", unit="{turn}", description="Ходы по исходу")

log = logging.getLogger(__name__)


def setup(environ: Mapping[str, str]) -> bool:
    """Включает экспорт, если задан адрес приёмника. `True` — включён. Вызывается один раз
    при старте процесса: глобальные провайдеры OpenTelemetry второй раз не ставятся.
    """
    endpoint = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        return False

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": SERVICE_NAME})
    traces = TracerProvider(resource=resource, sampler=ParentBased(root=_SkipReadiness()))
    traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(traces)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(f"{endpoint}/v1/metrics"))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    logs = LoggerProvider(resource=resource)
    logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(f"{endpoint}/v1/logs")))
    set_logger_provider(logs)
    # Журнал сервиса уходит и в приёмник; в журнал секреты не пишутся — это проверяют тесты.
    # Записи с INFO — только от сервиса: итог каждого хода связан с его трассой.
    logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logs))
    logging.getLogger("assistant").setLevel(logging.INFO)
    log.info("телеметрия: экспорт по OTLP включён")
    return True


class _SkipReadiness(Sampler):
    """Сэмплер: трассы проверки готовности не пишутся. Compose дёргает /healthz каждые
    десять секунд, и без этого трассы ходов тонули бы в них."""

    def should_sample(
        self,
        parent_context: Any,
        trace_id: int,
        name: str,
        kind: Any = None,
        attributes: Any = None,
        links: Any = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        # Спан запроса FastAPI получает имя с маршрутом позже; путь есть в атрибутах сразу.
        if name.endswith("/healthz") or (attributes or {}).get("url.path") == "/healthz":
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes, trace_state)

    def get_description(self) -> str:
        return "SkipReadiness"


@contextmanager
def span(
    name: str, attributes: Mapping[str, Any] | None = None, kind: SpanKind = SpanKind.INTERNAL
) -> Iterator[Span]:
    """Спан с атрибутами; исключение помечает его ошибкой и уходит дальше. Пустые значения
    не пишутся: у OpenTelemetry нет атрибута «нет значения».
    """
    with tracer.start_as_current_span(
        name, kind=kind, attributes=_clean(attributes or {}), record_exception=False
    ) as current:
        try:
            yield current
        except Exception as error:
            # Тип ошибки, а не её текст: в тексте исключения может оказаться что угодно.
            current.set_status(Status(StatusCode.ERROR, type(error).__name__))
            current.set_attribute("error.type", type(error).__name__)
            raise


def set_attributes(current: Span, attributes: Mapping[str, Any]) -> None:
    current.set_attributes(_clean(attributes))


def _clean(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attributes.items() if value is not None}
