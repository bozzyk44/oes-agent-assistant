"""Миграции схемы под ролью владельца. Вызываются шагом миграций и тестами."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

_MIGRATIONS = Path(__file__).parent / "migrations"


class SchemaOutdated(Exception):
    """Схема базы не на последней версии: сначала шаг миграций."""


def _config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    """Последняя версия схемы по файлам миграций."""
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:
        raise RuntimeError("в каталоге миграций нет ни одной версии")
    return head


def current_revision(connection: Connection) -> str | None:
    """Версия схемы в базе; `None` — миграции не применялись."""
    return MigrationContext.configure(connection).get_current_revision()


def upgrade(connection: Connection) -> bool:
    """Доводит схему до последней версии. Возвращает, менялось ли что-нибудь."""
    before = current_revision(connection)
    if before == head_revision():
        return False
    command.upgrade(_config(connection), "head")
    return True


def downgrade(connection: Connection, revision: str = "base") -> None:
    """Откатывает схему; нужен проверке миграций, в запуске не используется."""
    command.downgrade(_config(connection), revision)


def require_head(connection: Connection) -> None:
    """Останавливает того, кто собрался работать со схемой не последней версии."""
    current, head = current_revision(connection), head_revision()
    if current != head:
        raise SchemaOutdated(
            f"схема базы не на последней версии: в базе {current or 'нет миграций'},"
            f" нужна {head}. Примените миграции: `make migrate`"
        )
