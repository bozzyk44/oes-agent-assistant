"""Идентификатор оборудования сравнивается по ключу: без регистра, пробелов и дефисов,
с приведением похожих кириллических букв к латинским.

Модель переносит идентификатор из русского вопроса и пишет кириллические «Е» и «Х»
вместо латинских. Посимвольное сравнение давало «такого оборудования нет» и для своего,
и для чужого — отказ превращался в отсутствие данных.
"""

from alembic import op

revision = "0002"
down_revision = "0001"

# Строчные кириллические буквы переводятся отдельно: `upper` в базе с локалью C
# кириллицу не трогает, а латиницу — да.
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

EQUIPMENT_SITE_BY_KEY = """
create or replace function dwh.equipment_site(p_equipment_id text) returns text
language sql stable security definer
set search_path = pg_catalog, pg_temp
as $$
    select e.site from dwh.equipment e
    where dwh.equipment_key(e.equipment_id) = dwh.equipment_key(p_equipment_id)
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
    op.execute(EQUIPMENT_SITE_BY_KEY)


def downgrade() -> None:
    op.execute(EQUIPMENT_SITE_EXACT)
    op.execute("drop function dwh.equipment_key(text)")
