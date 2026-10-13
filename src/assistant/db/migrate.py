"""Применение миграций под ролью владельца. Вызывается загрузчиком и тестами."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

_MIGRATIONS = Path(__file__).parent / "migrations"


def upgrade(connection: Connection) -> None:
    """Доводит схему до последней версии; на актуальной базе ничего не делает."""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
