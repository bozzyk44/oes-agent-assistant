"""Проверки загрузчика перед работой с базой.

Шаги перед сервисом убеждаются, что окружение заполнено настоящими значениями, а шаг
ролей и баз — что сервер PostgreSQL принимает вход администратора по сети.
"""

from collections.abc import Mapping

import psycopg

# Метка из .env.example: значение, которое `make run` заменяет случайным секретом.
PLACEHOLDER = "__GENERATE__"

# Переменные шага ролей и баз — самого требовательного. Шагам миграций и загрузки нужна
# только часть: пароль администратора им не передаётся. Токена ТОиР нет ни у одного шага.
REQUIRED = (
    "POSTGRES_PASSWORD",
    "DATABASE_OWNER_URL",
    "DATABASE_URL",
    "TOIR_DATABASE_URL",
)


class StartupError(Exception):
    """Запуск невозможен; текст ошибки безопасно показывать: в нём нет значений переменных."""


def check_environment(env: Mapping[str, str], required: tuple[str, ...] = REQUIRED) -> None:
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise StartupError("не заданы переменные окружения: " + ", ".join(missing))
    unfilled = [name for name in required if PLACEHOLDER in env[name]]
    if unfilled:
        raise StartupError(
            "в переменных остались метки из .env.example: "
            + ", ".join(unfilled)
            + ". Создайте .env командой `make run` или впишите значения вручную"
        )


def connect_admin(host: str, port: int, password: str) -> psycopg.Connection:
    """Входит администратором по сети в режиме автофиксации: базы создаются вне транзакций."""
    try:
        return psycopg.connect(
            host=host,
            port=port,
            user="postgres",
            password=password,
            dbname="postgres",
            connect_timeout=10,
            autocommit=True,
        )
    except psycopg.OperationalError as exc:
        # Текст ошибки драйвера содержит адрес и имя роли, но не пароль.
        message = f"PostgreSQL недоступен: {exc}".strip()
        if "password authentication failed" in str(exc):
            message += (
                ". Пароль администратора сервер запоминает при создании тома базы: "
                "скорее всего, том создан с другим .env. Верните прежний .env "
                "или удалите том командой `make reset`"
            )
        raise StartupError(message) from exc
