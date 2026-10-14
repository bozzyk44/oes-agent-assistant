"""Участок оборудования — из базы, а не из слов модели.

Функция базы идёт в обход политик и возвращает только код участка: право на заявку задаётся
отдельно от права на таблицу оборудования, и пользователь может оформлять заявки, не видя
этой таблицы. Идентификатор сравнивается по ключу, который считает база: без регистра,
пробелов и дефисов, с приведением похожих кириллических букв к латинским.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, text

_SITES = text(
    "select id, dwh.equipment_key(id), dwh.equipment_site(id)"
    " from unnest(cast(:ids as text[])) as id"
)


@dataclass(frozen=True)
class Located:
    # Ключ сравнения: по нему же фильтруются строки таблиц.
    key: str
    # `None` — такого оборудования нет.
    site: str | None


def locate(connection: Connection, ids: Iterable[str]) -> dict[str, Located]:
    """Ключ и участок каждого названного идентификатора — одним запросом."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    return {row[0]: Located(row[1], row[2]) for row in connection.execute(_SITES, {"ids": wanted})}
