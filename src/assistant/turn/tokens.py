"""Оценка токенов без сети и без словаря токенизатора.

Модель проверяющих неизвестна, а библиотеки точного подсчёта загружают словари из сети.
Оценка идёт по классам символов: буквы дешевле, цифры, знаки и разметка дороже — в строках
таблиц и в JSON токенов на символ заметно больше, чем в тексте. Коэффициенты по умолчанию
завышают оценку; поправка — один множитель на модель, растёт только вверх по фактическому
входу из ответа провайдера и хранится в базе.
"""

import json
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from assistant.config import CharCosts
from assistant.model import ChatRequest

# Потолок поправки: больше — скорее сбой счётчика провайдера, чем токенизатор. Без потолка
# одно неправдоподобное число навсегда сделало бы любой вход «больше лимита».
MAX_FACTOR = 2.0
# Служебные токены разметки чата на каждое сообщение и на запрос целиком.
MESSAGE_OVERHEAD = 8
REQUEST_OVERHEAD = 16


def text_tokens(value: str, costs: CharCosts) -> float:
    letters = digits = spaces = other = 0
    for char in value:
        if char.isalpha():
            letters += 1
        elif char.isdigit():
            digits += 1
        elif char.isspace():
            spaces += 1
        else:
            other += 1
    return (
        letters / costs.letters
        + digits / costs.digits
        + spaces / costs.spaces
        + other / costs.other
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Estimator:
    """Оценка входа одного вызова. `factor` — поправка модели, не меньше единицы."""

    costs: CharCosts
    factor: float = 1.0

    def raw(self, request: ChatRequest) -> float:
        tokens: float = REQUEST_OVERHEAD + MESSAGE_OVERHEAD * len(request.messages)
        tokens += text_tokens(_dump(list(request.messages)), self.costs)
        if request.tools:
            tokens += text_tokens(_dump(list(request.tools)), self.costs)
        if request.response_format is not None:
            tokens += text_tokens(_dump(request.response_format), self.costs)
        return tokens

    def request(self, request: ChatRequest) -> int:
        return math.ceil(self.raw(request) * self.factor)

    def text(self, value: str) -> int:
        """Оценка куска входа — результата инструмента — с той же поправкой. Строка
        считается так же, как во входе вызова: внутри JSON сообщения, с экранированием."""
        return math.ceil(text_tokens(_dump(value), self.costs) * self.factor)

    def learn(self, raw: float, actual: int) -> bool:
        """Сверка с фактом: поправка растёт, если оценка оказалась меньше. Вниз — никогда."""
        if raw <= 0 or actual <= raw * self.factor or self.factor >= MAX_FACTOR:
            return False
        # Параллельные ходы: поправка в памяти тоже только растёт.
        self.factor = max(self.factor, min(actual / raw, MAX_FACTOR))
        return True


_LOAD = text("select factor from app.token_calibration where model = :model")
_SAVE = text(
    """
    insert into app.token_calibration (model, factor) values (:model, :factor)
    on conflict (model) do update
    set factor = greatest(app.token_calibration.factor, excluded.factor), updated_at = now()
    """
)


def load_factor(connection: Connection, model: str) -> float:
    value = connection.scalar(_LOAD, {"model": model})
    return min(max(float(value), 1.0), MAX_FACTOR) if value is not None else 1.0


def save_factor(connection: Connection, model: str, factor: float) -> None:
    connection.execute(_SAVE, {"model": model, "factor": factor})
