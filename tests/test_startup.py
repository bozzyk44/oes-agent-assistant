import os

import pytest

from assistant.ingest.startup import (
    PLACEHOLDER,
    REQUIRED,
    StartupError,
    check_environment,
    connect_admin,
)

FILLED = {name: f"value-of-{name.lower()}" for name in REQUIRED}


def test_filled_environment_passes() -> None:
    check_environment(FILLED)


@pytest.mark.parametrize("name", REQUIRED)
def test_missing_variable_is_named(name: str) -> None:
    env = {key: value for key, value in FILLED.items() if key != name}
    with pytest.raises(StartupError, match=name):
        check_environment(env)


@pytest.mark.parametrize("name", REQUIRED)
def test_empty_variable_is_named(name: str) -> None:
    with pytest.raises(StartupError, match=name):
        check_environment({**FILLED, name: ""})


@pytest.mark.parametrize("name", REQUIRED)
def test_placeholder_is_rejected_and_value_not_shown(name: str) -> None:
    value = f"postgresql://role:{PLACEHOLDER}@postgres:5432/base"
    with pytest.raises(StartupError) as caught:
        check_environment({**FILLED, name: value})
    assert name in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.db
def test_server_answers_over_network() -> None:
    with connect_admin(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        password=os.environ["POSTGRES_PASSWORD"],
    ) as admin:
        row = admin.execute("select current_setting('server_version'), current_user").fetchone()
    assert row is not None
    assert row[0].split(".")[0].isdigit()
    assert row[1] == "postgres"


@pytest.mark.db
def test_wrong_password_fails_without_echoing_it() -> None:
    wrong = "wrong-password-marker-5f1c"
    with pytest.raises(StartupError) as caught:
        connect_admin(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            password=wrong,
        )
    assert wrong not in str(caught.value)
    # Новый .env поверх старого тома — частый случай; ошибка должна говорить, что делать.
    assert "make reset" in str(caught.value)
