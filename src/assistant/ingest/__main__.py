"""Загрузчик: разовый запуск перед сервисом. `python -m assistant.ingest`.

Шаги: роли и базы под администратором; миграции, права роли сервиса и загрузка набора
под владельцем. Любая ошибка — код 1, и сервис не стартует.
"""

import os
import sys
from pathlib import Path

import psycopg
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from assistant.db.grants import grant_service
from assistant.db.migrate import upgrade
from assistant.ingest.bootstrap import ensure_roles_and_databases, read_accounts
from assistant.ingest.dataset import DatasetError, read_dataset
from assistant.ingest.load import LOADER_VERSION, load_dataset
from assistant.ingest.startup import StartupError, check_environment, connect_admin


def run(env: dict[str, str]) -> str:
    check_environment(env)
    accounts = read_accounts(
        env["DATABASE_OWNER_URL"], env["DATABASE_URL"], env["TOIR_DATABASE_URL"]
    )
    # Набор читается и проверяется до любых изменений в базе.
    dataset = read_dataset(Path(env.get("DATA_DIR") or "data"), LOADER_VERSION)

    with connect_admin(
        host=env.get("POSTGRES_HOST", "postgres"),
        port=int(env.get("POSTGRES_PORT", "5432")),
        password=env["POSTGRES_PASSWORD"],
    ) as admin:
        ensure_roles_and_databases(admin, accounts)

    engine = create_engine(env["DATABASE_OWNER_URL"])
    try:
        with engine.begin() as connection:
            upgrade(connection)
            grant_service(connection, accounts.service.role)
        with engine.begin() as connection:
            replaced = load_dataset(connection, dataset)
    finally:
        engine.dispose()
    counts = (
        f"документов {len(dataset.documents)}, пользователей {len(dataset.users)},"
        f" строк КХД {len(dataset.production) + len(dataset.downtime) + len(dataset.equipment)}"
    )
    if replaced:
        return f"набор загружен: {counts}; сессии и действия очищены"
    return f"набор не изменился: {counts}; сессии и действия сохранены"


def main() -> int:
    try:
        print(f"загрузчик: {run(dict(os.environ))}")
    except (StartupError, DatasetError) as exc:
        print(f"загрузчик: {exc}", file=sys.stderr)
        return 1
    except (psycopg.Error, SQLAlchemyError) as exc:
        # Только первая строка ошибки сервера: без текста запроса и его параметров.
        # Адрес и имя роли в ней бывают, пароль — нет.
        reason = str(getattr(exc, "orig", None) or exc).strip().split("\n")[0]
        print(f"загрузчик: ошибка базы: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
