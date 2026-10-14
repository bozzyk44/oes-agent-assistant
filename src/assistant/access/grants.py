"""Полномочия пользователя на ход.

Объект создаётся в начале хода из таблиц `app` и не меняется до его конца. Это подсказка
коду, какие условия ставить в запрос; база проверяет то же самое построчной защитой сама,
по идентификатору из привязки транзакции, и расширенный список от сервиса не принимает.
"""

from dataclasses import dataclass, field

from sqlalchemy import Connection, text

# Группа в `acl` документа, открывающая его всем.
EVERYONE = "all"

_USER = text("select name, position_title from app.users where user_id = :user_id")
_GROUPS = text("select group_name from app.user_groups where user_id = :user_id")
_KPI = text("select table_name, site from app.kpi_grants where user_id = :user_id")
_REPAIR = text("select site from app.repair_grants where user_id = :user_id")
# Коды участков набора — из полномочий всех пользователей: это формат, а не содержимое.
_SITES = text("select site from app.kpi_grants union select site from app.repair_grants")

# Кириллические буквы, неотличимые на письме от латинских: модель переносит код участка
# из русского вопроса и пишет кириллическую «К» вместо латинской.
_LOOKALIKES = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")


def site_key(site: str) -> str:
    """Код участка для сравнения: без крайних пробелов, регистра и похожих букв."""
    return site.strip().upper().translate(_LOOKALIKES)


@dataclass(frozen=True)
class Grants:
    user_id: str
    name: str
    position: str
    groups: frozenset[str] = frozenset()
    # Таблица КХД → участки, строки которых доступны. Таблицы нет — нет доступа.
    kpi: dict[str, frozenset[str]] = field(default_factory=dict)
    # Участки, на оборудование которых можно оформить заявку.
    repair_sites: frozenset[str] = frozenset()
    # Все коды участков, известные приложению; по ним названный участок приводится к коду.
    known_sites: frozenset[str] = frozenset()

    @property
    def doc_groups(self) -> list[str]:
        """Группы, с которыми сравнивается `acl` документа: свои и «всем»."""
        return sorted({EVERYONE, *self.groups})

    def kpi_sites(self, table: str) -> frozenset[str]:
        return self.kpi.get(table, frozenset())


def load_grants(connection: Connection, user_id: str) -> Grants | None:
    """Полномочия из базы; `None` — пользователя нет в `app.users`."""
    row = connection.execute(_USER, {"user_id": user_id}).first()
    if row is None:
        return None
    params = {"user_id": user_id}
    kpi: dict[str, set[str]] = {}
    for table, site in connection.execute(_KPI, params):
        kpi.setdefault(table, set()).add(site)
    return Grants(
        user_id=user_id,
        name=row.name,
        position=row.position_title,
        groups=frozenset(connection.scalars(_GROUPS, params)),
        kpi={table: frozenset(sites) for table, sites in kpi.items()},
        repair_sites=frozenset(connection.scalars(_REPAIR, params)),
        known_sites=frozenset(connection.scalars(_SITES)),
    )
