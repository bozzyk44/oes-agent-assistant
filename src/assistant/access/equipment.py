"""Оборудование, названное моделью: написание из набора и участок — из базы.

Функции базы идут в обход политик и возвращают по одному значению: право на заявку
задаётся отдельно от права на таблицу оборудования, и пользователь может оформлять заявки,
не видя этой таблицы. Идентификатор находится точно или по однозначному ключу —
без регистра, пробелов и дефисов, с приведением похожих кириллических букв к латинским.
Ключ, общий у двух разных идентификаторов, не находит ни один: иначе первый попавшийся
мог бы выдать чужое оборудование за своё.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, text

_LOCATE = text(
    "select name, dwh.equipment_id(name), dwh.equipment_site(name)"
    " from unnest(cast(:names as text[])) as name"
)


# Тот же ключ, что считает база функцией `dwh.equipment_key` (миграция 0002); совпадение
# проверяет тест.
_LOOKALIKES = str.maketrans("АВЕКМНОРСТУХавекмнорстух", "ABEKMHOPCTYXABEKMHOPCTYX")
_SPACES_AND_HYPHENS = re.compile(r"[\s-]")


def equipment_key(equipment_id: str) -> str:
    """Ключ сравнения идентификатора: без регистра, пробелов и дефисов, похожие
    кириллические буквы приведены к латинским.
    """
    return _SPACES_AND_HYPHENS.sub("", equipment_id).translate(_LOOKALIKES).upper()


@dataclass(frozen=True)
class Located:
    # Написание из набора; `None` — такого оборудования нет или написание неоднозначно.
    equipment_id: str | None
    site: str | None


def locate(connection: Connection, names: Iterable[str]) -> dict[str, Located]:
    """Каждое названное оборудование — одним запросом."""
    wanted = sorted(set(names))
    if not wanted:
        return {}
    rows = connection.execute(_LOCATE, {"names": wanted})
    return {row[0]: Located(row[1], row[2]) for row in rows}
