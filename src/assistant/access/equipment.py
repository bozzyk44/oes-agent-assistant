"""Участок оборудования — из базы, а не из слов модели.

Функция базы идёт в обход политик и возвращает только код участка: право на заявку задаётся
отдельно от права на таблицу оборудования, и пользователь может оформлять заявки, не видя
этой таблицы. Идентификатор сравнивается как есть, без крайних пробелов.
"""

from collections.abc import Iterable

from sqlalchemy import Connection, text

_SITES = text("select id, dwh.equipment_site(id) from unnest(cast(:ids as text[])) as id")


def normalize(equipment_id: str) -> str:
    return equipment_id.strip()


def equipment_sites(connection: Connection, ids: Iterable[str]) -> dict[str, str | None]:
    """Участок каждого идентификатора одним запросом; `None` — такого оборудования нет."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    return {row[0]: row[1] for row in connection.execute(_SITES, {"ids": wanted})}
