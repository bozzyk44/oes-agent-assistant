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

from sqlalchemy import (
    BigInteger,
    Column,
    ColumnElement,
    Connection,
    Date,
    DateTime,
    Integer,
    SmallInteger,
    Table,
    func,
    select,
)

from assistant.access.equipment import locate
from assistant.access.grants import Grants, site_key
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
# Границы целых колонок: значение за ними база отвергла бы ошибкой, которая оборвала бы
# транзакцию хода, а не дала `invalid`.
_INT_BOUNDS = ((SmallInteger, 2**15), (BigInteger, 2**63), (Integer, 2**31))


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
    # Названное оборудование, которого нет в наборе, когда другое названное нашлось.
    missing_equipment: tuple[str, ...] = ()
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
    """Строки таблицы в пределах полномочий пользователя.

    `denied` не несёт содержимого: ни строк, ни перечня отказанного — как и при отказе
    во всех названных участках.
    """
    table = TABLES.get(query.table) if isinstance(query.table, str) else None
    if table is None:
        return KpiResult(Status.INVALID, problem=f"table: нет таблицы {query.table!r}")
    granted = grants.kpi_sites(query.table)
    if not granted:
        # Запрос не выполняется вовсе: полномочий на таблицу нет.
        return KpiResult(Status.DENIED)
    try:
        conditions, site_filters, equipment_filters = _conditions(table, query)
        limit = _limit(query.limit)
        # Несколько фильтров по участку пересекаются уже после приведения к кодам.
        named_sites = (
            frozenset.intersection(*(_canonical_sites(grants, item) for item in site_filters))
            if site_filters
            else None
        )
        if len(equipment_filters) > 1:
            raise _Invalid("filters: по equipment_id — не больше одного фильтра")
    except _Invalid as error:
        return KpiResult(Status.INVALID, problem=str(error))
    named_equipment = equipment_filters[0] if equipment_filters else None

    scope = granted
    denied_sites: tuple[str, ...] = ()
    if named_sites is not None:
        denied_sites = tuple(sorted(named_sites - granted))
        scope = granted & named_sites
        if not scope:
            # Все явно названные участки вне полномочий; пустой список — просто пусто.
            return KpiResult(Status.DENIED if denied_sites else Status.EMPTY)

    denied_equipment: tuple[str, ...] = ()
    missing_equipment: tuple[str, ...] = ()
    if named_equipment is not None:
        located = locate(connection, named_equipment)
        own = sorted(
            {
                item.equipment_id
                for item in located.values()
                if item.equipment_id is not None and item.site in granted
            }
        )
        unknown = sorted(name for name, item in located.items() if item.site is None)
        foreign = sorted(
            name for name, item in located.items() if item.site and item.site not in granted
        )
        if named_equipment and not own:
            return KpiResult(Status.DENIED if foreign else Status.NOT_FOUND)
        denied_equipment, missing_equipment = tuple(foreign), tuple(unknown)
        # Строки — по написанию из набора, а не по написанию модели.
        conditions.append(table.c[EQUIPMENT_ID].in_(own))

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
        missing_equipment=missing_equipment,
        truncated=len(rows) > limit,
    )


def _canonical_sites(grants: Grants, named: frozenset[str]) -> frozenset[str]:
    """Названные участки — к кодам приложения. Опечатка или название вместо кода —
    неверный аргумент: в ответе все коды участков, и модель повторит вызов с кодом —
    своим или чужим, — а отказ по чужому даст сам запрос.
    """
    codes = {site_key(code): code for code in grants.known_sites}
    unknown = sorted(site for site in named if site_key(site) not in codes)
    if unknown:
        known = ", ".join(sorted(grants.known_sites))
        raise _Invalid(f"filters: {unknown[0]!r} — не код участка; коды участков: {known}")
    return frozenset(codes[site_key(site)] for site in named)


@dataclass(frozen=True)
class Period:
    first: date
    last: date


def reference_day(periods: dict[str, Period]) -> date | None:
    """«Сегодня» для относительных дат — последний день доступных данных. Данные набора
    относятся к своему периоду: календарное «вчера» было бы днём без данных. Одна точка
    отсчёта для сведений о пользователе и для периода запроса к КХД (решение 19)."""
    return max((period.last for period in periods.values()), default=None)


def data_periods(connection: Connection, grants: Grants) -> dict[str, Period]:
    """Период данных в доступных пользователю строках: по датам добычи и началу простоев.

    Нужен для сведений о пользователе во входе модели: вопрос «за вчера» она соотносит
    с периодом данных, а не с текущей датой, которой во входе нет.
    """
    periods: dict[str, Period] = {}
    for name, column in (("production", "date"), ("downtime", "start")):
        sites = grants.kpi_sites(name)
        if not sites:
            continue
        table = TABLES[name]
        found = connection.execute(
            select(func.min(table.c[column]), func.max(table.c[column])).where(
                table.c[SITE].in_(sorted(sites))
            )
        ).one()
        if found[0] is not None:
            first, last = found
            periods[name] = Period(
                first.date() if isinstance(first, datetime) else first,
                last.date() if isinstance(last, datetime) else last,
            )
    return periods


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_LIMIT:
        raise _Invalid(f"limit: ожидалось целое от 1 до {MAX_LIMIT}")
    return value


def _conditions(
    table: Table, query: KpiQuery
) -> tuple[list[ColumnElement[bool]], list[frozenset[str]], list[frozenset[str]]]:
    """Условия фильтров и то, что в них названо явно: участки и оборудование.

    Фильтры по участку и оборудованию в условия не идут: участки становятся областью
    запроса внутри полномочий, оборудование — проверенным списком своего участка.
    """
    conditions: list[ColumnElement[bool]] = []
    sites: list[frozenset[str]] = []
    equipment: list[frozenset[str]] = []
    for item in query.filters:
        if not isinstance(item, Filter) or not isinstance(item.column, str):
            raise _Invalid("filters: ожидался список фильтров с именем колонки")
        if not isinstance(item.op, str):
            raise _Invalid(f"filters: операция для {item.column} — строка")
        if item.column in _HIDDEN or item.column not in table.c:
            raise _Invalid(f"filters: нет колонки {item.column!r} в таблице {query.table}")
        column = table.c[item.column]
        if item.op in _LISTS:
            values = _values(column, item)
            if item.column == SITE:
                sites.append(values)
            elif item.column == EQUIPMENT_ID:
                equipment.append(values)
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
            number = value if isinstance(value, int) else int(str(value).strip())
            bound = next(limit for cls, limit in _INT_BOUNDS if isinstance(kind, cls))
            if not -bound <= number < bound:
                raise ValueError
            return number
        if isinstance(kind, DateTime):
            moment = datetime.fromisoformat(str(value).strip())
            # Время набора — местное, без пояса; значение с поясом база сдвинула бы молча.
            if moment.tzinfo is not None:
                raise ValueError
            return moment
        if isinstance(kind, Date):
            return date.fromisoformat(str(value).strip())
    except TypeError, ValueError:
        raise _Invalid(f"filters: неверное значение для {column.name}") from None
    if not isinstance(value, str):
        raise _Invalid(f"filters: для {column.name} ожидалась строка")
    return value
