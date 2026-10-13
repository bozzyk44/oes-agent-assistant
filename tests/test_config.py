"""Настройки сервиса из окружения."""

import pytest

from assistant.config import PLACEHOLDER, ConfigError, load_settings, load_toir_secret

BASE = {"DATABASE_URL": "postgresql+psycopg://app:db-password-marker@postgres:5432/assistant"}


def test_defaults() -> None:
    settings = load_settings(BASE)
    # Каталог набора по умолчанию — из задания; сервис его не читает, читает загрузчик.
    assert settings.data_dir == "data/"
    assert settings.toir_url == "http://toir:8001"
    assert settings.max_message_chars > 0
    assert 0 < settings.turn_timeout_seconds < settings.session_lease_seconds
    # Без переменных модели настройки собираются: сервис стартует.
    assert settings.model.configured is False


def test_values_come_from_environment() -> None:
    settings = load_settings(
        {
            **BASE,
            "DATA_DIR": "/data",
            "MODEL_BASE_URL": "http://model/v1",
            "MODEL_API_KEY": "model-key-marker",
            "MODEL_NAME": "some/model",
            "TOIR_URL": "http://toir.example",
            "MAX_MESSAGE_CHARS": "500",
            "TURN_TIMEOUT_SECONDS": "30",
            "SESSION_LEASE_SECONDS": "45.5",
        }
    )
    assert settings.data_dir == "/data"
    assert settings.model.configured is True
    assert settings.model.name == "some/model"
    assert settings.toir_url == "http://toir.example"
    assert (settings.max_message_chars, settings.turn_timeout_seconds) == (500, 30)
    assert settings.session_lease_seconds == 45.5


@pytest.mark.parametrize("missing", ["MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"])
def test_model_needs_all_three_variables(missing: str) -> None:
    env = {**BASE, "MODEL_BASE_URL": "http://m/v1", "MODEL_API_KEY": "k", "MODEL_NAME": "n"}
    del env[missing]
    assert load_settings(env).model.configured is False


def test_secrets_are_not_in_repr() -> None:
    settings = load_settings({**BASE, "MODEL_API_KEY": "model-key-marker"})
    assert "db-password-marker" not in repr(settings)
    assert "model-key-marker" not in repr(settings)
    secret = load_toir_secret({"TOIR_TOKEN": "toir-token-marker"})
    assert secret.token == "toir-token-marker"
    assert "toir-token-marker" not in repr(secret)
    # Токена ТОиР в настройках, которые получает ход, нет вовсе.
    assert not any("toir_token" in name or name == "token" for name in vars(settings))


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "DATABASE_URL"),
        ({"DATABASE_URL": f"postgresql://app:{PLACEHOLDER}@postgres/assistant"}, "DATABASE_URL"),
        ({**BASE, "TURN_TIMEOUT_SECONDS": "много"}, "TURN_TIMEOUT_SECONDS"),
        ({**BASE, "MAX_MESSAGE_CHARS": "0"}, "MAX_MESSAGE_CHARS"),
        ({**BASE, "SESSION_LEASE_SECONDS": "-5"}, "SESSION_LEASE_SECONDS"),
        # «Числа», которые молча выключили бы предел времени или роняли бы каждый ход.
        ({**BASE, "TURN_TIMEOUT_SECONDS": "nan"}, "TURN_TIMEOUT_SECONDS"),
        ({**BASE, "SESSION_LEASE_SECONDS": "inf"}, "SESSION_LEASE_SECONDS"),
        ({**BASE, "SESSION_LEASE_SECONDS": "1e400"}, "SESSION_LEASE_SECONDS"),
        ({**BASE, "MAX_MESSAGE_CHARS": "0.5"}, "MAX_MESSAGE_CHARS"),
        ({**BASE, "MAX_MESSAGE_CHARS": "1e400"}, "MAX_MESSAGE_CHARS"),
        ({**BASE, "MAX_MESSAGE_CHARS": "-3"}, "MAX_MESSAGE_CHARS"),
        # Предел времени хода обязан быть короче срока отметки занятости.
        ({**BASE, "TURN_TIMEOUT_SECONDS": "120", "SESSION_LEASE_SECONDS": "120"}, "меньше"),
    ],
)
def test_bad_environment_is_named_without_values(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(env)
    assert message in str(caught.value)
    assert PLACEHOLDER not in str(caught.value)
    assert "db-password-marker" not in str(caught.value)


@pytest.mark.parametrize("env", [{}, {"TOIR_TOKEN": ""}, {"TOIR_TOKEN": PLACEHOLDER}])
def test_toir_token_must_be_real(env: dict[str, str]) -> None:
    # Метку вместо секрета сервис проверяет сам: у шагов запуска токена ТОиР нет.
    with pytest.raises(ConfigError, match="TOIR_TOKEN"):
        load_toir_secret(env)
