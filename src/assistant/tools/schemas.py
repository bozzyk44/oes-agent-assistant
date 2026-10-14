"""Схемы пяти инструментов: как их видит модель и как код проверяет аргументы.

Схемы входят в неизменную часть входа и в `prefix_hash`: порядок фиксирован, значений
из набора в них нет — только имена таблиц и колонок из формата задания. Имя
`create_repair_request` и его параметры заданы заданием и не переименовываются.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SEARCH = "search_documents"
DOCUMENT = "get_document"
KPI = "query_kpi"
CALCULATE = "calculate"
REPAIR = "create_repair_request"

KPI_COLUMNS = {
    "production": ("date", "shift", "site", "tonnes"),
    "downtime": ("equipment_id", "site", "start", "end", "reason", "duration_hours"),
    "equipment": (
        "equipment_id",
        "type",
        "model",
        "site",
        "motor_hours",
        "last_ppr_motor_hours",
        "hours_since_ppr",
    ),
}


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchArgs(_Args):
    query: str = Field(min_length=1, max_length=500)
    include_superseded: bool = False


class DocumentArgs(_Args):
    ref: str = Field(min_length=1, max_length=100)


class KpiFilter(_Args):
    column: str
    op: Literal["eq", "in", "gt", "gte", "lt", "lte"]
    value: Any


class KpiPeriod(_Args):
    # Включительно, календарные дни.
    date_from: date
    date_to: date


class KpiAggregate(_Args):
    function: Literal["sum", "avg", "min", "max", "count"]
    column: str | None = None


class KpiArgs(_Args):
    table: Literal["production", "downtime", "equipment"]
    filters: list[KpiFilter] = Field(default_factory=list, max_length=10)
    period: KpiPeriod | None = None
    group_by: list[str] = Field(default_factory=list, max_length=3)
    aggregates: list[KpiAggregate] = Field(default_factory=list, max_length=5)
    decimals: int = Field(default=1, ge=0, le=4)
    limit: int = Field(default=50, ge=1, le=200)


class CalculateArgs(_Args):
    operation: Literal["percent", "ratio", "difference", "sum", "expression"]
    numbers: list[Decimal] = Field(default_factory=list, max_length=50)
    expression: str | None = Field(default=None, max_length=300)
    decimals: int = Field(default=1, ge=0, le=6)


class RepairArgs(_Args):
    equipment_id: str
    description: str
    priority: Any


ARGUMENTS: dict[str, type[_Args]] = {
    SEARCH: SearchArgs,
    DOCUMENT: DocumentArgs,
    KPI: KpiArgs,
    CALCULATE: CalculateArgs,
    REPAIR: RepairArgs,
}


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


_FILTER = {
    "type": "object",
    "properties": {
        "column": {"type": "string", "description": "Колонка таблицы"},
        "op": {"type": "string", "enum": ["eq", "in", "gt", "gte", "lt", "lte"]},
        "value": {"description": "Значение; для in — список"},
    },
    "required": ["column", "op", "value"],
    "additionalProperties": False,
}

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    _tool(
        SEARCH,
        "Поиск по документам предприятия: регламенты, порядки, приказы, справочники. Возвращает"
        " до четырёх лучших доступных фрагментов с идентификаторами для цитат. Утратившие силу"
        " редакции — только при include_superseded.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слова для поиска, по-русски"},
                "include_superseded": {
                    "type": "boolean",
                    "description": "Искать и в утративших силу редакциях — только для истории",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    _tool(
        DOCUMENT,
        "Документ или его фрагмент по идентификатору из выдачи поиска: «<документ>» или"
        " «<документ>#<раздел>».",
        {
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "Идентификатор"}},
            "required": ["ref"],
            "additionalProperties": False,
        },
    ),
    _tool(
        KPI,
        "Данные КХД: production (date, shift, site, tonnes) — добыча за смену; downtime"
        " (equipment_id, site, start, end, reason, duration_hours) — простои; equipment"
        " (equipment_id, type, model, site, motor_hours, last_ppr_motor_hours,"
        " hours_since_ppr) — оборудование. Без агрегатов — строки, с агрегатами — итоги по"
        " группам. period ограничивает даты, простой входит частью внутри периода. Участки"
        " в site — коды участков.",
        {
            "type": "object",
            "properties": {
                "table": {"type": "string", "enum": ["production", "downtime", "equipment"]},
                "filters": {"type": "array", "items": _FILTER},
                "period": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD, включительно"},
                    },
                    "required": ["date_from", "date_to"],
                    "additionalProperties": False,
                },
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Колонки группировки; date — по дням",
                },
                "aggregates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "function": {
                                "type": "string",
                                "enum": ["sum", "avg", "min", "max", "count"],
                            },
                            "column": {"type": "string"},
                        },
                        "required": ["function"],
                        "additionalProperties": False,
                    },
                },
                "decimals": {"type": "integer", "description": "Знаков после запятой, 0–4"},
                "limit": {"type": "integer", "description": "Строк без агрегатов, до 200"},
            },
            "required": ["table"],
            "additionalProperties": False,
        },
    ),
    _tool(
        CALCULATE,
        "Арифметика над числами из результатов инструментов. percent: первое от второго в"
        " процентах; ratio: первое к второму; difference: первое минус второе; sum: сумма"
        " всех; expression: выражение из чисел, + - * / и скобок в поле expression, например"
        " доля факта от плана на период. Округление половина вверх до decimals знаков.",
        {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["percent", "ratio", "difference", "sum", "expression"],
                },
                "numbers": {"type": "array", "items": {"type": "number"}},
                "expression": {
                    "type": "string",
                    "description": "Для expression: только числа, + - * / и скобки",
                },
                "decimals": {"type": "integer"},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    ),
    _tool(
        REPAIR,
        "Подготовить заявку на ремонт оборудования в ТОиР. Заявка не создаётся, пока"
        " пользователь её не подтвердит. Приоритет — по порядку оформления заявок из документов.",
        {
            "type": "object",
            "properties": {
                "equipment_id": {
                    "type": "string",
                    "description": "Идентификатор оборудования, как его назвал пользователь",
                },
                "description": {"type": "string", "description": "Описание неисправности"},
                "priority": {"type": "integer", "description": "Приоритет 1–3"},
            },
            "required": ["equipment_id", "description", "priority"],
            "additionalProperties": False,
        },
    ),
)
