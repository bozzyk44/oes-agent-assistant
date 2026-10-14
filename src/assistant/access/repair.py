"""Право на заявку: участок оборудования из базы сверяется с полномочием на заявки."""

from dataclasses import dataclass

from sqlalchemy import Connection

from assistant.access.equipment import locate
from assistant.access.grants import Grants
from assistant.access.status import Status


@dataclass(frozen=True)
class RepairCheck:
    status: Status
    # Участок и написание из набора — только при `ok`: отказ не сообщает, где стоит
    # оборудование. В параметры заявки идёт это написание, а не написание модели.
    site: str | None = None
    equipment_id: str | None = None


def check_repair(connection: Connection, grants: Grants, equipment_id: str) -> RepairCheck:
    """Можно ли пользователю оформить заявку на это оборудование.

    Участок берётся из базы, а не из слов модели. Не совпал с полномочием — `denied`,
    и действие не создаётся.
    """
    found = locate(connection, [equipment_id])[equipment_id]
    if found.site is None:
        return RepairCheck(Status.NOT_FOUND)
    if found.site not in grants.repair_sites:
        return RepairCheck(Status.DENIED)
    return RepairCheck(Status.OK, found.site, found.equipment_id)
