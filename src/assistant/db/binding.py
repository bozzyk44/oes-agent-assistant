"""Привязка транзакции к пользователю — второй барьер прав."""

from sqlalchemy import Connection, text

_BIND = text("select set_config('app.user_id', :user_id, true)")


def bind_user(connection: Connection, user_id: str) -> None:
    """Сообщает базе, от чьего имени идёт текущая транзакция.

    Значение передаётся параметром, а не подстановкой в текст запроса, и действует
    до конца транзакции: следующая транзакция на том же соединении его не видит.
    """
    connection.execute(_BIND, {"user_id": user_id})
