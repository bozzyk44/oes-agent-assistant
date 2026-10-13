"""Загрузчик: разовый запуск перед сервисом. `python -m assistant.ingest`."""

import os
import sys

from assistant.ingest.startup import StartupError, check_environment, check_server


def main() -> int:
    try:
        check_environment(os.environ)
        version = check_server(
            host=os.environ.get("POSTGRES_HOST", "postgres"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            password=os.environ["POSTGRES_PASSWORD"],
        )
    except StartupError as exc:
        print(f"загрузчик: {exc}", file=sys.stderr)
        return 1
    print(f"загрузчик: PostgreSQL {version} отвечает по сети")
    print("загрузчик: роли, базы, миграции и загрузка набора ещё не реализованы")
    return 0


if __name__ == "__main__":
    sys.exit(main())
