"""Право на заявку: участок оборудования из базы сверяется с полномочием на заявки."""

from dataclasses import dataclass

from sqlalchemy import Connection

from assistant.access.equipment import equipment_sites, normalize
from assistant.access.grants import Grants
from assistant.access.status import Status


@dataclass(frozen=True)
class RepairCheck:
    status: Status
    # Участок — только при `ok`: отказ не сообщает, где стоит оборудование.
    site: str | None = None


def check_repair(connection: Connection, grants: Grants, equipment_id: str) -> RepairCheck:
    """Можно ли пользователю оформить заявку на это оборудование.

    Участок берётся из базы, а не из слов модели. Не совпал с полномочием — `denied`,
    и действие не создаётся.
    """
    equipment_id = normalize(equipment_id)
    site = equipment_sites(connection, [equipment_id]).get(equipment_id)
    if site is None:
        return RepairCheck(Status.NOT_FOUND)
    if site not in grants.repair_sites:
        return RepairCheck(Status.DENIED)
    return RepairCheck(Status.OK, site)
