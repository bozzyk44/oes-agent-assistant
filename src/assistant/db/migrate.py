"""Миграции схемы под ролью владельца. Вызываются шагом миграций и тестами."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Connection

_MIGRATIONS = Path(__file__).parent / "migrations"


class SchemaMismatch(Exception):
    """Версия схемы в базе не та, с которой может работать эта сборка."""


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


def _known(revision: str) -> bool:
    try:
        return ScriptDirectory.from_config(_config()).get_revision(revision) is not None
    except CommandError:
        return False


def _reject_newer(current: str | None) -> None:
    # Версии из базы нет среди файлов миграций: базу мигрировала более новая сборка.
    # Это не «отстала», и довести её миграциями этой сборки нельзя.
    if current is not None and not _known(current):
        raise SchemaMismatch(
            f"в базе версия схемы {current}, которой нет в этой сборке: база новее кода."
            " Разверните сборку, в которой эта миграция есть, или удалите том базы: `make reset`"
        )


def upgrade(connection: Connection) -> tuple[str | None, str]:
    """Доводит схему до последней версии. Возвращает версию до и после; на актуальной
    базе они совпадают и ничего не меняется.
    """
    before, head = current_revision(connection), head_revision()
    _reject_newer(before)
    if before != head:
        command.upgrade(_config(connection), "head")
    return before, head


def downgrade(connection: Connection, revision: str = "base") -> None:
    """Откатывает схему; нужен проверке миграций, в запуске не используется."""
    command.downgrade(_config(connection), revision)


def require_head(connection: Connection) -> None:
    """Останавливает того, кто собрался работать со схемой не последней версии."""
    current, head = current_revision(connection), head_revision()
    _reject_newer(current)
    if current != head:
        raise SchemaMismatch(
            f"схема базы не на последней версии: в базе {current or 'нет миграций'},"
            f" нужна {head}. Примените миграции: `make migrate`"
        )
