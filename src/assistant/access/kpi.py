"""Строки таблиц КХД.

Запрос приходит структурой из закрытых списков, а не SQL: SQL строит код. Условие по
участкам пользователя добавляется к любому запросу, и параметрами его не снять: фильтр
по участку может только сузить набор разрешённых участков. Построчная защита `dwh`
проверяет то же условие второй раз.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import Column, ColumnElement, Connection, Date, DateTime, Integer, Table, select
from sqlalchemy import text as sql_text

from assistant.access.grants import Grants
from assistant.access.status import Status
from assistant.db import models

TABLES: dict[str, Table] = {
    "production": models.Production.__table__,  # type: ignore[dict-item]
    "downtime": models.Downtime.__table__,  # type: ignore[dict-item]
    "equipment": models.Equipment.__table__,  # type: ignore[dict-item]
}
SITE = "site"
EQUIPMENT_ID = "equipment_id"
# Служебный ключ строки, которого нет в наборе: не фильтруется и не выдаётся.
_HIDDEN = frozenset({"row_id"})
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

Op = Literal["eq", "in", "gt", "gte", "lt", "lte"]
_LISTS = frozenset({"eq", "in"})
_COMPARISONS = frozenset({"gt", "gte", "lt", "lte"})

_EQUIPMENT_SITE = sql_text("select dwh.equipment_site(:equipment_id)")


@dataclass(frozen=True)
class Filter:
    column: str
    op: Op
    # Для `in` — список, для остальных — одно значение.
    value: Any


@dataclass(frozen=True)
class KpiQuery:
    table: str
    filters: tuple[Filter, ...] = ()
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class KpiResult:
    status: Status
    rows: tuple[dict[str, Any], ...] = ()
    # По каким участкам посчитано.
    sites: tuple[str, ...] = ()
    # Явно названные участки и оборудование, в которых отказано.
    denied_sites: tuple[str, ...] = ()
    denied_equipment: tuple[str, ...] = ()
    # Строк больше, чем `limit`: выдача неполная.
    truncated: bool = False
    # Что не так с аргументами — для `invalid`.
    problem: str = ""

    @property
    def partially_denied(self) -> bool:
        """Отказ по части названного — сигнал отказа, даже если данные по остальному есть."""
        return bool(self.denied_sites or self.denied_equipment)


class _Invalid(Exception):
    pass


def query_rows(connection: Connection, grants: Grants, query: KpiQuery) -> KpiResult:
    """Строки таблицы в пределах полномочий пользователя."""
    table = TABLES.get(query.table)
    if table is None:
        return KpiResult(Status.INVALID, problem=f"table: нет таблицы {query.table!r}")
    granted = grants.kpi_sites(query.table)
    if not granted:
        # Запрос не выполняется вовсе: полномочий на таблицу нет.
        return KpiResult(Status.DENIED)
    try:
        conditions, named_sites, named_equipment = _conditions(table, query)
        limit = _limit(query.limit)
    except _Invalid as error:
        return KpiResult(Status.INVALID, problem=str(error))

    scope = granted
    denied_sites: tuple[str, ...] = ()
    if named_sites is not None:
        denied_sites = tuple(sorted(named_sites - granted))
        scope = granted & named_sites
        if not scope:
            # Все явно названные участки вне полномочий; пустой список — просто пусто.
            return KpiResult(Status.DENIED if denied_sites else Status.EMPTY)

    denied_equipment: tuple[str, ...] = ()
    if named_equipment is not None:
        own, foreign, unknown = _split_equipment(connection, named_equipment, granted)
        denied_equipment = tuple(sorted(foreign))
        if not own:
            if foreign:
                return KpiResult(Status.DENIED, denied_sites=denied_sites)
            if unknown:
                return KpiResult(Status.NOT_FOUND)
        conditions.append(table.c[EQUIPMENT_ID].in_(sorted(own)))

    site: Column[str] = table.c[SITE]
    statement = (
        select(*(column for column in table.c if column.name not in _HIDDEN))
        .where(site.in_(sorted(scope)), *conditions)
        .order_by(table.c.row_id)
        .limit(limit + 1)
    )
    rows = [dict(row._mapping) for row in connection.execute(statement)]
    return KpiResult(
        Status.OK if rows else Status.EMPTY,
        rows=tuple(rows[:limit]),
        sites=tuple(sorted(scope)),
        denied_sites=denied_sites,
        denied_equipment=denied_equipment,
        truncated=len(rows) > limit,
    )


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_LIMIT:
        raise _Invalid(f"limit: ожидалось целое от 1 до {MAX_LIMIT}")
    return value


def _conditions(
    table: Table, query: KpiQuery
) -> tuple[list[ColumnElement[bool]], frozenset[str] | None, frozenset[str] | None]:
    """Условия фильтров и то, что в них названо явно: участки и оборудование.

    Фильтры по участку и оборудованию в условия не идут: участки становятся областью
    запроса внутри полномочий, оборудование — проверенным списком своего участка.
    """
    conditions: list[ColumnElement[bool]] = []
    sites: frozenset[str] | None = None
    equipment: frozenset[str] | None = None
    for item in query.filters:
        if item.column in _HIDDEN or item.column not in table.c:
            raise _Invalid(f"filters: нет колонки {item.column!r} в таблице {query.table}")
        column = table.c[item.column]
        if item.op in _LISTS:
            values = _values(column, item)
            if item.column == SITE:
                sites = values if sites is None else sites & values
            elif item.column == EQUIPMENT_ID:
                equipment = values if equipment is None else equipment & values
            else:
                conditions.append(column.in_(sorted(values)))
        elif item.op in _COMPARISONS:
            if not isinstance(column.type, Integer | Date | DateTime):
                raise _Invalid(f"filters: колонку {item.column} нельзя сравнивать по величине")
            value = _parse(column, item.value)
            operator = {
                "gt": column.__gt__,
                "gte": column.__ge__,
                "lt": column.__lt__,
                "lte": column.__le__,
            }[item.op]
            conditions.append(operator(value))
        else:
            raise _Invalid(f"filters: нет операции {item.op!r}")
    return conditions, sites, equipment


def _values(column: Column[Any], item: Filter) -> frozenset[Any]:
    raw: Sequence[Any] = item.value if item.op == "in" else [item.value]
    if item.op == "in" and (isinstance(raw, str) or not isinstance(raw, Sequence)):
        raise _Invalid(f"filters: для {item.column} с операцией in ожидался список")
    return frozenset(_parse(column, value) for value in raw)


def _parse(column: Column[Any], value: Any) -> Any:
    """Значение фильтра в тип колонки; не приводится — `invalid` с именем колонки."""
    kind = column.type
    try:
        if isinstance(kind, Integer):
            if isinstance(value, bool):
                raise TypeError
            return int(value) if isinstance(value, int) else int(str(value).strip())
        if isinstance(kind, DateTime):
            return datetime.fromisoformat(str(value).strip())
        if isinstance(kind, Date):
            return date.fromisoformat(str(value).strip())
    except TypeError, ValueError:
        raise _Invalid(f"filters: неверное значение для {column.name}") from None
    if not isinstance(value, str):
        raise _Invalid(f"filters: для {column.name} ожидалась строка")
    return value


def _split_equipment(
    connection: Connection, named: frozenset[str], granted: frozenset[str]
) -> tuple[set[str], set[str], set[str]]:
    """Оборудование из фильтра: своего участка, чужого и неизвестное.

    Участок берётся из базы, а не из слов модели; функция возвращает только код участка.
    """
    own: set[str] = set()
    foreign: set[str] = set()
    unknown: set[str] = set()
    for equipment_id in named:
        site = connection.scalar(_EQUIPMENT_SITE, {"equipment_id": equipment_id})
        if site is None:
            unknown.add(equipment_id)
        elif site in granted:
            own.add(equipment_id)
        else:
            foreign.add(equipment_id)
    return own, foreign, unknown
