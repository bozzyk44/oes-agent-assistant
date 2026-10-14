"""Запрос к КХД: строки берёт слой доступа, агрегаты и производные колонки считает код.

Права целиком на слое доступа: сюда приходят только строки разрешённых участков.
Простой попадает в период, если пересекается с ним; в длительность идёт только часть
внутри периода. При группировке по дням интервал через полночь делится между сутками.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Connection

from assistant.access import Filter, Grants, KpiQuery, KpiResult, Status, query_rows
from assistant.access.kpi import MAX_LIMIT
from assistant.tools.schemas import KPI_COLUMNS, KpiArgs

DURATION = "duration_hours"
SINCE_PPR = "hours_since_ppr"
DATE = "date"
_NUMERIC = {
    "production": ("tonnes", "shift"),
    "downtime": (DURATION,),
    "equipment": ("motor_hours", "last_ppr_motor_hours", SINCE_PPR),
}


def round_half_up(value: Decimal, decimals: int) -> Decimal:
    """Округление «половина вверх» — одно в запросе к КХД и в инструменте расчёта."""
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


@dataclass
class KpiAnswer:
    status: Status
    data: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    partially_denied: bool = False


class _Invalid(Exception):
    pass


def run_kpi(connection: Connection, grants: Grants, args: KpiArgs) -> KpiAnswer:
    columns = KPI_COLUMNS[args.table]
    try:
        _check_columns(args, columns)
    except _Invalid as error:
        return KpiAnswer(Status.INVALID, note=str(error))

    filters = [Filter(item.column, item.op, item.value) for item in args.filters]
    if any(item.column in (DURATION, SINCE_PPR) for item in filters):
        return KpiAnswer(Status.INVALID, note="filters: производные колонки не фильтруются")
    bounds = _bounds(args)
    if bounds is not None:
        begin, end = bounds
        if args.table == "production":
            filters += [Filter("date", "gte", begin.date()), Filter("date", "lt", end.date())]
        elif args.table == "downtime":
            filters += [
                Filter("start", "lt", end.isoformat()),
                Filter("end", "gt", begin.isoformat()),
            ]

    # Агрегатам нужны все строки; без них хватает первых `limit`.
    limit = MAX_LIMIT if args.aggregates or args.group_by else args.limit
    found = query_rows(connection, grants, KpiQuery(args.table, tuple(filters), limit))
    answer = KpiAnswer(found.status, partially_denied=found.partially_denied)
    answer.note = _note(found, args, bounds)
    if found.status is not Status.OK:
        if found.status is Status.INVALID:
            answer.note = found.problem
        return answer

    rows = [_with_derived(args.table, row, bounds) for row in found.rows]
    if args.group_by and DATE in args.group_by and args.table == "downtime":
        rows = list(_split_by_day(rows, bounds))
    data: dict[str, Any] = {"sites": list(found.sites)}
    if found.denied_sites:
        data["denied_sites"] = list(found.denied_sites)
    if found.denied_equipment:
        data["denied_equipment"] = list(found.denied_equipment)
    if found.missing_equipment:
        data["missing_equipment"] = list(found.missing_equipment)
    if args.aggregates:
        data["groups"] = _aggregate(rows, args)
        if found.truncated:
            answer.note += " Строк больше предела выборки: итог неполный."
    else:
        data["rows"] = [_plain(row, args.decimals) for row in rows]
        if found.truncated:
            data["more_rows"] = True
            answer.note += f" Показаны первые {len(rows)} строк; сузьте фильтры."
    answer.data = data
    return answer


def _check_columns(args: KpiArgs, columns: tuple[str, ...]) -> None:
    for column in args.group_by:
        if column not in columns and column != DATE:
            raise _Invalid(f"group_by: нет колонки {column!r} в таблице {args.table}")
    for item in args.aggregates:
        if item.function == "count":
            continue
        if item.column not in _NUMERIC[args.table]:
            raise _Invalid(
                f"aggregates: {item.function} — только по числовым колонкам"
                f" {', '.join(_NUMERIC[args.table])}"
            )
    if args.period is not None and args.period.date_from > args.period.date_to:
        raise _Invalid("period: date_from позже date_to")


def _bounds(args: KpiArgs) -> tuple[datetime, datetime] | None:
    if args.period is None:
        return None
    begin = datetime.combine(args.period.date_from, time())
    end = datetime.combine(args.period.date_to + timedelta(days=1), time())
    return begin, end


def _note(found: KpiResult, args: KpiArgs, bounds: tuple[datetime, datetime] | None) -> str:
    parts: list[str] = []
    if found.sites:
        parts.append(f"Посчитано по участкам: {', '.join(found.sites)}.")
    if found.denied_sites:
        parts.append(f"В доступе отказано по участкам: {', '.join(found.denied_sites)}.")
    if found.denied_equipment:
        parts.append(f"В доступе отказано по оборудованию: {', '.join(found.denied_equipment)}.")
    if found.missing_equipment:
        parts.append(f"Такого оборудования нет: {', '.join(found.missing_equipment)}.")
    if found.status is Status.NOT_FOUND:
        parts.append("Такого оборудования нет; список своего — запросом к equipment без фильтра.")
    if args.table == "downtime" and bounds is not None:
        parts.append(
            "Длительность простоя — часть интервала внутри периода; при группировке по дням"
            " интервал через полночь делится между сутками."
        )
    return " ".join(parts)


def _with_derived(
    table: str, row: dict[str, Any], bounds: tuple[datetime, datetime] | None
) -> dict[str, Any]:
    row = dict(row)
    if table == "downtime":
        start, end = row["start"], row["end"]
        if bounds is not None:
            start, end = max(start, bounds[0]), min(end, bounds[1])
        row[DURATION] = Decimal(max(int((end - start).total_seconds()), 0)) / 3600
        row["_clip"] = (start, end)
    elif table == "equipment":
        row[SINCE_PPR] = row["motor_hours"] - row["last_ppr_motor_hours"]
    return row


def _split_by_day(
    rows: Iterable[dict[str, Any]], bounds: tuple[datetime, datetime] | None
) -> Iterable[dict[str, Any]]:
    for row in rows:
        start, end = row["_clip"]
        cursor = start
        while cursor < end:
            midnight = datetime.combine(cursor.date() + timedelta(days=1), time())
            piece_end = min(end, midnight)
            yield {
                **row,
                DATE: cursor.date(),
                DURATION: Decimal(int((piece_end - cursor).total_seconds())) / 3600,
            }
            cursor = piece_end


def _group_key(row: dict[str, Any], args: KpiArgs) -> tuple[Any, ...]:
    key = []
    for column in args.group_by:
        value = row.get(column)
        if column == DATE and value is None:
            value = row["start"].date() if "start" in row else row.get("date")
        key.append(value)
    return tuple(key)


def _aggregate(rows: list[dict[str, Any]], args: KpiArgs) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_group_key(row, args)].append(row)
    result = []
    for key in sorted(groups, key=lambda item: tuple(str(part) for part in item)):
        members = groups[key]
        values: dict[str, Any] = {}
        for item in args.aggregates:
            name = f"{item.function}_{item.column}" if item.column else item.function
            if item.function == "count" or item.column is None:
                values[name] = len(members)
                continue
            column = item.column
            numbers = [Decimal(member[column]) for member in members]
            match item.function:
                case "sum":
                    value = sum(numbers, Decimal(0))
                case "avg":
                    value = sum(numbers, Decimal(0)) / len(numbers)
                case "min":
                    value = min(numbers)
                case _:
                    value = max(numbers)
            values[name] = _number(round_half_up(value, args.decimals))
        pairs = zip(args.group_by, key, strict=True)
        group = {column: _plain_value(part) for column, part in pairs}
        result.append({"group": group, "values": values})
    return result


def _number(value: Decimal) -> int | float | str:
    """Число в результате — строкой без лишних нулей: его переносят в ответ как есть."""
    if value == value.to_integral_value():
        return int(value)
    return format(value.normalize(), "f")


def _plain_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _plain(row: dict[str, Any], decimals: int) -> dict[str, Any]:
    plain: dict[str, Any] = {}
    for column, value in row.items():
        if column.startswith("_"):
            continue
        if isinstance(value, Decimal):
            plain[column] = _number(round_half_up(value, decimals))
        else:
            plain[column] = _plain_value(value)
    return plain
