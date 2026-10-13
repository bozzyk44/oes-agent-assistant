"""Проверки загрузчика перед работой с базой.

Каркас: загрузчик убеждается, что окружение заполнено настоящими значениями
и что сервер PostgreSQL принимает вход администратора по сети. Роли, базы,
миграции и загрузка набора появляются в задаче про схему базы.
"""

from collections.abc import Mapping

import psycopg

# Метка из .env.example: значение, которое `make run` заменяет случайным секретом.
PLACEHOLDER = "__GENERATE__"

# Переменные загрузчика. Токена ТОиР среди них нет: загрузчику он не нужен и не передаётся.
REQUIRED = (
    "POSTGRES_PASSWORD",
    "DATABASE_OWNER_URL",
    "DATABASE_URL",
    "TOIR_DATABASE_URL",
)


class StartupError(Exception):
    """Запуск невозможен; текст ошибки безопасно показывать: в нём нет значений переменных."""


def check_environment(env: Mapping[str, str]) -> None:
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise StartupError("не заданы переменные окружения: " + ", ".join(missing))
    unfilled = [name for name in REQUIRED if PLACEHOLDER in env[name]]
    if unfilled:
        raise StartupError(
            "в переменных остались метки из .env.example: "
            + ", ".join(unfilled)
            + ". Создайте .env командой `make run` или впишите значения вручную"
        )


def check_server(host: str, port: int, password: str) -> str:
    """Входит администратором по сети и возвращает версию сервера."""
    try:
        with psycopg.connect(
            host=host,
            port=port,
            user="postgres",
            password=password,
            dbname="postgres",
            connect_timeout=10,
        ) as conn:
            row = conn.execute("select current_setting('server_version')").fetchone()
    except psycopg.OperationalError as exc:
        # Текст ошибки драйвера содержит адрес и имя роли, но не пароль.
        raise StartupError(f"PostgreSQL недоступен: {exc}".strip()) from exc
    if row is None:
        raise StartupError("PostgreSQL не вернул версию сервера")
    return str(row[0])
