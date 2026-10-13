"""Автомат действия на уровне базы: полная матрица «статус × переход»."""

from itertools import count, product
from uuid import uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from assistant.db.binding import bind_user
from assistant.db.models import ACTION_STATUSES, ACTION_TRANSITIONS
from tests.conftest import Database

pytestmark = pytest.mark.db

# Идентификаторы свои на каждый запуск: база между тестами и запусками не откатывается.
_RUN = uuid4().hex[:8]
_numbers = count(1)
# Как действие попадает в статус разрешёнными переходами.
_PATH = {
    "proposed": (),
    "confirming": ("confirming",),
    "rejected": ("rejected",),
    "done": ("confirming", "done"),
    "failed": ("confirming", "failed"),
}


def _move(connection: Connection, action_id: str, status: str) -> None:
    extra = {"done": ", request_id = 'R-1'", "failed": ", failure_reason = 'отвергнут'"}
    connection.execute(
        text(f"update app.actions set status = :s{extra.get(status, '')} where action_id = :a"),  # noqa: S608
        {"s": status, "a": action_id},
    )


def _action(connection: Connection, status: str = "proposed") -> str:
    """Создаёт сессию, ход и действие под ролью сервиса и доводит до статуса."""
    number = next(_numbers)
    action_id = f"a-{_RUN}-{number}"
    session_id, turn_id = f"s-act-{_RUN}-{number}", f"t-act-{_RUN}-{number}"
    bind_user(connection, "u-eng")
    connection.execute(
        text("insert into app.sessions (user_id, session_id) values ('u-eng', :s)"),
        {"s": session_id},
    )
    connection.execute(
        text(
            "insert into app.turns (turn_id, user_id, session_id, question, outcome, answer,"
            " model_calls, input_tokens, output_tokens, max_call_input_tokens, prefix_hash,"
            " latency_ms) values (:t, 'u-eng', :s, 'в', 'action', 'о', 1, 1, 1, 1, 'h', 1)"
        ),
        {"t": turn_id, "s": session_id},
    )
    connection.execute(
        text(
            "insert into app.actions (action_id, user_id, session_id, turn_id, tool, params)"
            " values (:a, 'u-eng', :s, :t, 'create_repair_request',"
            """ '{"equipment_id": "EX-1", "priority": 2}')"""
        ),
        {"a": action_id, "t": turn_id, "s": session_id},
    )
    for step in _PATH[status]:
        _move(connection, action_id, step)
    return action_id


@pytest.mark.parametrize(("source", "target"), list(product(ACTION_STATUSES, ACTION_STATUSES)))
def test_transition_matrix(database: Database, small: object, source: str, target: str) -> None:
    with database.service.begin() as connection:
        action_id = _action(connection, source)
    # Диагональ тоже проверяется: обновление без перехода — например, второе из двух
    # одновременных подтверждений — отклоняется, а не проходит молча.
    allowed = (source, target) in ACTION_TRANSITIONS
    try:
        with database.service.begin() as connection:
            _move(connection, action_id, target)
    except DBAPIError as error:
        assert not allowed, f"{source} -> {target} должен проходить"
        expected = "уже в статусе" if source == target else "недопустимый переход"
        assert expected in str(error)
    else:
        assert allowed, f"{source} -> {target} должен отклоняться"
    with database.service.connect() as connection:
        status = connection.scalar(
            text("select status from app.actions where action_id = :a"), {"a": action_id}
        )
    assert status == (target if allowed else source)


def test_idempotency_key_is_created_once_and_kept(database: Database, small: object) -> None:
    with database.service.begin() as connection:
        first, second = _action(connection), _action(connection)
        keys = connection.execute(
            text("select action_id, idempotency_key from app.actions where action_id in (:a, :b)"),
            {"a": first, "b": second},
        ).all()
    by_id = {row[0]: row[1] for row in keys}
    assert by_id[first] is not None
    assert by_id[first] != by_id[second]
    with database.service.begin() as connection:
        _move(connection, first, "confirming")
        _move(connection, first, "done")
        kept = connection.scalar(
            text("select idempotency_key from app.actions where action_id = :a"), {"a": first}
        )
    # Ключ пережил оба перехода: повтор вызова ТОиР идёт с тем же ключом.
    assert kept == by_id[first]


@pytest.mark.parametrize(
    ("status", "change", "message"),
    [
        ("proposed", "idempotency_key = gen_random_uuid()", "только статус и результат"),
        ("confirming", "idempotency_key = gen_random_uuid()", "только статус и результат"),
        ("proposed", """params = '{"equipment_id": "EX-2"}'""", "только статус и результат"),
        ("proposed", "user_id = 'u-k1'", "только статус и результат"),
        ("proposed", "tool = 'other'", "только статус и результат"),
        ("done", "request_id = 'R-2'", "уже в статусе"),
        ("failed", "failure_reason = 'другая'", "уже в статусе"),
        ("proposed", "request_id = 'R-1'", "уже в статусе"),
        ("confirming", "updated_at = now()", "уже в статусе"),
        ("confirming", "status = 'done'", "actions_request_iff_done"),
        ("confirming", "status = 'failed'", "actions_reason_iff_failed"),
    ],
)
def test_action_fields_are_protected(
    database: Database, small: object, status: str, change: str, message: str
) -> None:
    with database.service.begin() as connection:
        action_id = _action(connection, status)
    with database.service.begin() as connection, pytest.raises(DBAPIError, match=message):
        connection.execute(
            text(f"update app.actions set {change} where action_id = :a"),  # noqa: S608
            {"a": action_id},
        )


def test_action_is_created_only_as_proposed(database: Database, small: object) -> None:
    with database.service.begin() as connection:
        action_id = _action(connection)
        row = connection.execute(
            text("select session_id, turn_id from app.actions where action_id = :a"),
            {"a": action_id},
        ).one()
    with database.service.begin() as connection:
        bind_user(connection, "u-eng")
        # В одном ходе — не больше одного действия.
        with pytest.raises(DBAPIError, match="actions_turn_id_key"):
            connection.execute(
                text(
                    "insert into app.actions (action_id, user_id, session_id, turn_id, tool,"
                    " params) values ('a-second', 'u-eng', :s, :t, 'x', '{}')"
                ),
                {"s": row[0], "t": row[1]},
            )
    with database.service.begin() as connection:
        other = _action(connection)
        session, turn = connection.execute(
            text("select session_id, turn_id from app.actions where action_id = :a"), {"a": other}
        ).one()
    # Освобождаем ход: действие удаляет владелец, роли сервиса удаление не дано.
    with database.owner.begin() as connection:
        connection.execute(text("delete from app.actions where action_id = :a"), {"a": other})
    insert = (
        "insert into app.actions (action_id, user_id, session_id, turn_id, tool, params{})"
        " values (:a, 'u-eng', :s, :t, 'x', '{{}}'{})"
    )
    with database.service.begin() as connection, pytest.raises(DBAPIError, match="proposed"):
        connection.execute(
            text(insert.format(", status", ", 'confirming'")),
            {"a": f"a-{_RUN}-confirmed", "s": session, "t": turn},
        )
    # Ключ и время создания ставит база, что бы ни передал код.
    forced = "00000000-0000-0000-0000-000000000001"
    with database.service.begin() as connection:
        connection.execute(
            text(insert.format(", idempotency_key, created_at", ", :k, '2000-01-01'")),
            {"a": f"a-{_RUN}-forced", "s": session, "t": turn, "k": forced},
        )
        key, year = connection.execute(
            text(
                "select idempotency_key::text, extract(year from created_at)"
                " from app.actions where action_id = :a"
            ),
            {"a": f"a-{_RUN}-forced"},
        ).one()
    assert key != forced
    assert year > 2000


def test_action_author_is_turn_author(database: Database, small: object) -> None:
    # На авторе действия держится проверка «подтверждает автор»: он тот же, что у хода.
    with database.service.begin() as connection:
        mine = _action(connection)
        session, turn = connection.execute(
            text("select session_id, turn_id from app.actions where action_id = :a"), {"a": mine}
        ).one()
    with database.owner.begin() as connection:
        connection.execute(text("delete from app.actions where action_id = :a"), {"a": mine})
    with database.service.begin() as connection, pytest.raises(DBAPIError, match="foreign key"):
        connection.execute(
            text(
                "insert into app.actions (action_id, user_id, session_id, turn_id, tool, params)"
                " values (:a, 'u-k1', :s, :t, 'x', '{}')"
            ),
            {"a": f"a-{_RUN}-foreign", "s": session, "t": turn},
        )


def test_action_state_survives_reconnect(database: Database, small: object) -> None:
    # Рестарт сервиса между вызовом ТОиР и записью результата: состояние живёт в базе.
    with database.service.begin() as connection:
        action_id = _action(connection, "confirming")
        key = connection.scalar(
            text("select idempotency_key from app.actions where action_id = :a"), {"a": action_id}
        )
    database.service.dispose()
    with database.service.begin() as connection:
        row = connection.execute(
            text("select status, idempotency_key from app.actions where action_id = :a"),
            {"a": action_id},
        ).one()
        assert row == ("confirming", key)
        _move(connection, action_id, "done")
