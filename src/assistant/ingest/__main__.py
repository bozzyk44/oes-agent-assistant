"""Разовые шаги перед сервисом: `python -m assistant.ingest <шаг>`.

Три шага, каждый — отдельный запуск и отдельный сервис compose:

- `bootstrap` — роли и базы под администратором сервера;
- `migrate` — миграции схемы и права роли сервиса под владельцем;
- `load` — загрузка набора под владельцем; сама миграции не применяет.

Шаги разделены, чтобы применение миграций было явным и одинаковым локально, в CI и при
деплое, а пароль администратора сервера получал только первый шаг. Любая ошибка — код 1,
следующий шаг и сервис не стартуют.
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import psycopg
from alembic.util import CommandError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from assistant.db.grants import grant_service
from assistant.db.migrate import SchemaMismatch, require_head, upgrade
from assistant.ingest.bootstrap import ensure_roles_and_databases, read_accounts, read_pair
from assistant.ingest.dataset import DatasetError, read_dataset
from assistant.ingest.load import LOADER_VERSION, load_dataset
from assistant.ingest.startup import REQUIRED, StartupError, check_environment, connect_admin

Env = Mapping[str, str]


def bootstrap(env: Env) -> str:
    """Роли и базы. Единственный шаг, которому нужен пароль администратора сервера."""
    check_environment(env, REQUIRED)
    accounts = read_accounts(
        env["DATABASE_OWNER_URL"], env["DATABASE_URL"], env["TOIR_DATABASE_URL"]
    )
    with connect_admin(
        host=env.get("POSTGRES_HOST", "postgres"),
        port=int(env.get("POSTGRES_PORT", "5432")),
        password=env["POSTGRES_PASSWORD"],
    ) as admin:
        ensure_roles_and_databases(admin, accounts)
    return f"роли и базы на месте: {accounts.service.database}, {accounts.toir.database}"


def migrate(env: Env) -> str:
    """Миграции и права роли сервиса. Повторный запуск ничего не меняет."""
    check_environment(env, ("DATABASE_OWNER_URL", "DATABASE_URL"))
    # Шаг запускается и отдельно от остальных, поэтому адреса сверяет сам: права роли
    # сервиса выдаются именно в той базе, которую мигрирует владелец, и не ему самому.
    _, service = read_pair(env["DATABASE_OWNER_URL"], env["DATABASE_URL"])
    engine = create_engine(env["DATABASE_OWNER_URL"])
    try:
        with engine.begin() as connection:
            before, after = upgrade(connection)
            # Права выдаются при каждом запуске: имя роли приходит из окружения.
            grant_service(connection, service.role)
    finally:
        engine.dispose()
    if before != after:
        return f"миграции применены: {before or 'пустая база'} -> {after}"
    return f"схема уже на последней версии: {after}"


def load(env: Env) -> str:
    """Загрузка набора. Схему не меняет: если она отстала, останавливается."""
    check_environment(env, ("DATABASE_OWNER_URL",))
    # Набор читается и проверяется до любых изменений данных в базе.
    dataset = read_dataset(Path(env.get("DATA_DIR") or "data"), LOADER_VERSION)
    engine = create_engine(env["DATABASE_OWNER_URL"])
    try:
        with engine.begin() as connection:
            require_head(connection)
            replaced = load_dataset(connection, dataset)
    finally:
        engine.dispose()
    counts = (
        f"документов {len(dataset.documents)}, пользователей {len(dataset.users)},"
        f" строк КХД {len(dataset.production) + len(dataset.downtime) + len(dataset.equipment)}"
    )
    if dataset.skipped:
        counts += f"; пропущены файлы без frontmatter: {', '.join(dataset.skipped)}"
    if replaced:
        return f"набор загружен: {counts}; сессии и действия очищены"
    return f"набор не изменился: {counts}; сессии и действия сохранены"


STEPS: dict[str, Callable[[Env], str]] = {"bootstrap": bootstrap, "migrate": migrate, "load": load}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="assistant.ingest", description="Шаги перед сервисом")
    parser.add_argument("step", choices=sorted(STEPS), help="какой шаг выполнить")
    step: str = parser.parse_args(argv).step
    try:
        print(f"{step}: {STEPS[step](os.environ)}")
    except (StartupError, DatasetError, SchemaMismatch) as exc:
        print(f"{step}: {exc}", file=sys.stderr)
        return 1
    except CommandError as exc:
        print(f"{step}: миграции не применены: {exc}", file=sys.stderr)
        return 1
    except (psycopg.Error, SQLAlchemyError) as exc:
        # Только первая строка ошибки сервера: без текста запроса и его параметров.
        # Адрес и имя роли в ней бывают, пароль — нет.
        reason = str(getattr(exc, "orig", None) or exc).strip().split("\n")[0]
        print(f"{step}: ошибка базы: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
