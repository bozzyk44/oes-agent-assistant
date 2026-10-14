"""Идентификатор оборудования — точно или по однозначному ключу.

Модель переносит идентификатор из русского вопроса и пишет кириллические «Е» и «Х»
вместо латинских. Посимвольное сравнение давало «такого оборудования нет» и для своего,
и для чужого — отказ превращался в отсутствие данных.

Ключ — без регистра, пробелов и дефисов, с приведением похожих кириллических букв
к латинским. По ключу идентификатор находится, только если ключ однозначен: два разных
идентификатора с одним ключом могут стоять на разных участках, и первый попавшийся
выдал бы чужое оборудование за своё. Точное совпадение важнее ключа.
"""

from alembic import op

revision = "0002"
down_revision = "0001"

# Строчные кириллические буквы переводятся отдельно: `upper` в базе с локалью C
# кириллицу не трогает, а латиницу — да. Функция вызывается только внутри функций
# в обход политик, с правами владельца; роли сервиса право вызова не нужно.
EQUIPMENT_KEY = """
create function dwh.equipment_key(p_equipment_id text) returns text
language sql immutable
set search_path = pg_catalog, pg_temp
as $$
    select upper(translate(
        regexp_replace(coalesce(p_equipment_id, ''), '[[:space:]-]', '', 'g'),
        'АВЕКМНОРСТУХавекмнорстух',
        'ABEKMHOPCTYXABEKMHOPCTYX'
    ))
$$;
revoke all on function dwh.equipment_key(text) from public;
"""

# Написание из набора: точное совпадение, иначе единственный идентификатор с тем же
# ключом. Ключ неоднозначен — `null`, как для несуществующего.
EQUIPMENT_ID = """
create function dwh.equipment_id(p_equipment_id text) returns text
language sql stable security definer
set search_path = pg_catalog, pg_temp
as $$
    select coalesce(
        (
            select e.equipment_id from dwh.equipment e
            where e.equipment_id = p_equipment_id
            limit 1
        ),
        (
            select min(e.equipment_id) from dwh.equipment e
            where dwh.equipment_key(e.equipment_id) = dwh.equipment_key(p_equipment_id)
            having count(distinct e.equipment_id) = 1
        )
    )
$$;
revoke all on function dwh.equipment_id(text) from public;
"""

EQUIPMENT_SITE_BY_ID = """
create or replace function dwh.equipment_site(p_equipment_id text) returns text
language sql stable security definer
set search_path = pg_catalog, pg_temp
as $$
    select e.site from dwh.equipment e
    where e.equipment_id = dwh.equipment_id(p_equipment_id)
    order by e.row_id limit 1
$$;
"""

EQUIPMENT_SITE_EXACT = """
create or replace function dwh.equipment_site(p_equipment_id text) returns text
language sql stable security definer
set search_path = pg_catalog, pg_temp
as $$
    select e.site from dwh.equipment e
    where e.equipment_id = p_equipment_id
    order by e.row_id limit 1
$$;
"""


def upgrade() -> None:
    op.execute(EQUIPMENT_KEY)
    op.execute(EQUIPMENT_ID)
    op.execute(EQUIPMENT_SITE_BY_ID)


def downgrade() -> None:
    op.execute(EQUIPMENT_SITE_EXACT)
    op.execute("drop function dwh.equipment_id(text)")
    op.execute("drop function dwh.equipment_key(text)")
