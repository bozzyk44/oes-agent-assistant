"""Ключ идентификатора оборудования убирает и неразрывные пробелы, и тире разных видов.

Текст, скопированный из документа или набранный на телефоне, приносит неразрывный пробел
и длинное тире вместо дефиса. Те же таблицы — в `assistant.lookalikes`; совпадение ключа
в коде и в базе проверяет тест. Значения записаны здесь буквально: миграция не должна
меняться вместе с кодом.
"""

from alembic import op

revision = "0004"
down_revision = "0003"

EQUIPMENT_KEY_SEPARATORS = """
create or replace function dwh.equipment_key(p_equipment_id text) returns text
language sql immutable
set search_path = pg_catalog, pg_temp
as $$
    select translate(
        translate(
            regexp_replace(
                coalesce(p_equipment_id, ''),
                '[ \\t\\n\\r\\f\\v  ‐‑‒–—―−-]', '', 'g'
            ),
            'abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюя',
            'ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ'
        ),
        'АВЕКМНОРСТУХ', 'ABEKMHOPCTYX'
    )
$$;
"""

LOWER = "abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"

# Тело из 0003 буквально: откат сверяется со слепком схемы до этой миграции.
EQUIPMENT_KEY_0003 = f"""
create or replace function dwh.equipment_key(p_equipment_id text) returns text
language sql immutable
set search_path = pg_catalog, pg_temp
as $$
    select translate(
        translate(
            regexp_replace(coalesce(p_equipment_id, ''), '[ \\t\\n\\r\\f\\v-]', '', 'g'),
            '{LOWER}', '{UPPER}'
        ),
        'АВЕКМНОРСТУХ', 'ABEKMHOPCTYX'
    )
$$;
"""


def upgrade() -> None:
    op.execute(EQUIPMENT_KEY_SEPARATORS)


def downgrade() -> None:
    op.execute(EQUIPMENT_KEY_0003)
