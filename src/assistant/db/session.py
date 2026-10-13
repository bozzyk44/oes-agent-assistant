"""Подключение сервиса к базе и транзакция от имени пользователя."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine

from assistant.db.binding import bind_user


def service_engine(database_url: str) -> Engine:
    """Пул соединений роли сервиса. Проверка перед выдачей: соединение, пережившее
    перезапуск PostgreSQL, заменяется, а не роняет запрос.
    """
    return create_engine(database_url, pool_pre_ping=True)


@contextmanager
def transaction(engine: Engine, user_id: str) -> Iterator[Connection]:
    """Транзакция, привязанная к пользователю первым же действием.

    Единственный способ, которым сервис открывает транзакцию с данными: без привязки
    политики базы не отдают ни одной строки, так что забытая привязка видна сразу.
    """
    with engine.begin() as connection:
        bind_user(connection, user_id)
        yield connection
