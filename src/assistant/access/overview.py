"""Обзор того, что пользователь может спросить: для страницы демонстрации.

Те же права, что у инструментов: документы — по группам пользователя, оборудование — по
участкам, где у него есть и данные КХД об оборудовании, и право на заявку. Условие по
правам стоит в запросе, построчная защита базы проверяет его второй раз.
"""

from dataclasses import dataclass

from sqlalchemy import Connection, text

from assistant.access.grants import Grants
from assistant.access.kpi import data_periods

# Только действующие редакции: ответ опирается на них, прежние — история.
_DOCUMENTS = text(
    "select distinct doc_id, title, doc_date from kb.chunks"
    " where acl && cast(:groups as text[]) and status = 'active'"
    " order by doc_id"
)
_EQUIPMENT = text(
    "select distinct equipment_id, type, site from dwh.equipment"
    " where site = any(cast(:sites as text[])) order by site, equipment_id limit :limit"
)
EQUIPMENT_LIMIT = 6


@dataclass(frozen=True)
class Overview:
    documents: list[dict[str, str]]
    kpi: list[dict[str, object]]
    repair_sites: list[str]
    equipment: list[dict[str, str]]


def overview(connection: Connection, grants: Grants) -> Overview:
    documents = [
        {"doc_id": row[0], "title": row[1], "date": row[2].isoformat()}
        for row in connection.execute(_DOCUMENTS, {"groups": grants.doc_groups})
    ]
    periods = data_periods(connection, grants)
    kpi: list[dict[str, object]] = []
    for table, sites in sorted(grants.kpi.items()):
        if not sites:
            continue
        period = periods.get(table)
        kpi.append(
            {
                "table": table,
                "sites": sorted(sites),
                "first": period.first.isoformat() if period else None,
                "last": period.last.isoformat() if period else None,
            }
        )
    # Оборудование для примера заявки: участки, где есть и право на заявку, и данные об
    # оборудовании. Без данных — пример без идентификатора не строится.
    repairable = sorted(grants.repair_sites & grants.kpi_sites("equipment"))
    equipment = (
        [
            {"equipment_id": row[0], "type": row[1], "site": row[2]}
            for row in connection.execute(
                _EQUIPMENT, {"sites": repairable, "limit": EQUIPMENT_LIMIT}
            )
        ]
        if repairable
        else []
    )
    return Overview(documents, kpi, sorted(grants.repair_sites), equipment)
