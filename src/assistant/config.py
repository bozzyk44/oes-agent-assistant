"""Настройки сервиса из переменных окружения.

Секрет ТОиР — в отдельном объекте. Приложение держит его рядом с собой и отдаёт только
`actions/`; цикл хода, инструменты и клиент модели работают с `Settings`, в которых токена
нет. Попасть во вход модели ему неоткуда.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

# Метка из .env.example: значение, которое `make run` заменяет случайным секретом.
PLACEHOLDER = "__GENERATE__"
# Верхние границы настроек: сутки и миллион символов заведомо больше любого разумного хода.
_MAX_SECONDS = 86_400
_MAX_CHARS = 1_000_000


class ConfigError(Exception):
    """Сервис не может стартовать; в тексте только имена переменных, значений нет."""


FormatMode = Literal["last", "all", "off"]
FORMAT_MODES: tuple[FormatMode, ...] = ("last", "all", "off")


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    # Ключ не показывается ни в repr настроек, ни в журнале.
    api_key: str = field(repr=False)
    name: str
    # Потолок выхода одного вызова: ограничивает цену и задержку хода.
    max_output_tokens: int = 2048
    # Предел времени одного вызова, секунды; сумма по ходу — меньше срока захвата сессии.
    timeout_seconds: float = 45.0
    # Принуждение к формату ответа: только на последнем вызове хода, на всех или выключено.
    response_format: FormatMode = "last"
    # Параметры конкретного провайдера, которых нет в общем протоколе. Значения могут
    # быть чувствительными, поэтому в repr их нет.
    extra_body: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def configured(self) -> bool:
        """Модель задана адресом и именем; без любого из них ход завершается с `error`.

        Ключ не обязателен: локальному серверу вроде Ollama или vLLM он не нужен.
        """
        return bool(self.base_url and self.name)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    # Каталог набора из задания. Сервис его не читает — набор приходит из базы, куда его
    # кладёт загрузчик; значение держится здесь как часть контракта окружения.
    data_dir: str
    model: ModelSettings
    toir_url: str
    # Сообщение пользователя длиннее — 422: оно ограничивает вход первого вызова модели.
    max_message_chars: int
    # Общий предел времени хода и срок отметки занятости сессии, секунды. Предел короче
    # срока: ход, уложившийся в предел, всегда успевает записать результат под своей меткой.
    turn_timeout_seconds: float
    session_lease_seconds: float


@dataclass(frozen=True)
class ToirSecret:
    token: str = field(repr=False)


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}: ожидалось число") from None
    # nan и inf — тоже «числа»: первое молча выключило бы предел, второе роняло бы ход.
    if not math.isfinite(value) or not 0 < value <= _MAX_SECONDS:
        raise ConfigError(f"{name}: ожидалось число секунд от 0 до {_MAX_SECONDS}")
    return value


def _count(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) <= _MAX_CHARS:
        raise ConfigError(f"{name}: ожидалось целое число от 1 до {_MAX_CHARS}")
    return int(raw)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"не задана переменная окружения {name}")
    if PLACEHOLDER in value:
        raise ConfigError(
            f"в переменной {name} осталась метка из .env.example."
            " Создайте .env командой `make run` или впишите значение вручную"
        )
    return value


def _format_mode(env: Mapping[str, str]) -> FormatMode:
    raw = env.get("MODEL_RESPONSE_FORMAT", "").strip() or "last"
    for mode in FORMAT_MODES:
        if raw == mode:
            return mode
    raise ConfigError(f"MODEL_RESPONSE_FORMAT: ожидалось одно из {', '.join(FORMAT_MODES)}")


def _extra_body(env: Mapping[str, str]) -> dict[str, Any]:
    raw = env.get("MODEL_EXTRA_BODY", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise ConfigError("MODEL_EXTRA_BODY: ожидался объект JSON") from None
    if not isinstance(value, dict):
        raise ConfigError("MODEL_EXTRA_BODY: ожидался объект JSON")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    settings = Settings(
        database_url=_required(env, "DATABASE_URL"),
        data_dir=env.get("DATA_DIR") or "data/",
        model=ModelSettings(
            base_url=env.get("MODEL_BASE_URL", "").strip(),
            api_key=env.get("MODEL_API_KEY", "").strip(),
            name=env.get("MODEL_NAME", "").strip(),
            max_output_tokens=_count(env, "MODEL_MAX_OUTPUT_TOKENS", 2048),
            timeout_seconds=_seconds(env, "MODEL_TIMEOUT_SECONDS", 45),
            response_format=_format_mode(env),
            extra_body=_extra_body(env),
        ),
        toir_url=env.get("TOIR_URL") or "http://toir:8001",
        max_message_chars=_count(env, "MAX_MESSAGE_CHARS", 4000),
        turn_timeout_seconds=_seconds(env, "TURN_TIMEOUT_SECONDS", 90),
        session_lease_seconds=_seconds(env, "SESSION_LEASE_SECONDS", 120),
    )
    if settings.turn_timeout_seconds >= settings.session_lease_seconds:
        raise ConfigError(
            "TURN_TIMEOUT_SECONDS должен быть меньше SESSION_LEASE_SECONDS: иначе ход может"
            " пережить свою отметку занятости сессии"
        )
    return settings


def load_toir_secret(env: Mapping[str, str]) -> ToirSecret:
    return ToirSecret(token=_required(env, "TOIR_TOKEN"))
