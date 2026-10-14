"""Ключ идентификатора оборудования без зависимости от локали.

В 0002 ключ приводил регистр функцией `upper`, которая при правилах сравнения C меняет
только латиницу, а при других — и кириллицу. Ключ в коде сервиса считается так же,
как в базе, и расхождение давало «такого оборудования нет» для идентификаторов
с кириллицей вне похожих букв. Теперь регистр и похожие буквы приводятся явными таблицами,
пробелы — только ASCII, одинаково в базе и в коде.
"""

from alembic import op

revision = "0003"
down_revision = "0002"

LOWER = "abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
# После перевода в верхний регистр: кириллические буквы, неотличимые от латинских.
LOOKALIKE_FROM = "АВЕКМНОРСТУХ"
LOOKALIKE_TO = "ABEKMHOPCTYX"

EQUIPMENT_KEY_EXPLICIT = f"""
create or replace function dwh.equipment_key(p_equipment_id text) returns text
language sql immutable
set search_path = pg_catalog, pg_temp
as $$
    select translate(
        translate(
            regexp_replace(coalesce(p_equipment_id, ''), '[ \\t\\n\\r\\f\\v-]', '', 'g'),
            '{LOWER}', '{UPPER}'
        ),
        '{LOOKALIKE_FROM}', '{LOOKALIKE_TO}'
    )
$$;
"""

EQUIPMENT_KEY_0002 = """
create or replace function dwh.equipment_key(p_equipment_id text) returns text
language sql immutable
set search_path = pg_catalog, pg_temp
as $$
    select upper(translate(
        regexp_replace(coalesce(p_equipment_id, ''), '[[:space:]-]', '', 'g'),
        'АВЕКМНОРСТУХавекмнорстух',
        'ABEKMHOPCTYXABEKMHOPCTYX'
    ))
$$;
"""


def upgrade() -> None:
    op.execute(EQUIPMENT_KEY_EXPLICIT)


def downgrade() -> None:
    op.execute(EQUIPMENT_KEY_0002)
