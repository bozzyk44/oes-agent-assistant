"""Интерфейс модели: один метод, вход — запрос целиком, выход — ответ с расходом.

Тесты подставляют заглушку с тем же интерфейсом: она отдаёт заданную последовательность
ответов и записывает каждый полученный вход.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    # Аргументы — как прислала модель: строка JSON, возможно неверная.
    arguments: str


@dataclass(frozen=True)
class UsageReport:
    """Фактический расход из ответа провайдера; нет поля — `None`."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: UsageReport = field(default_factory=UsageReport)
    # Ответ оборван по пределу длины: итогом он не считается.
    truncated: bool = False


@dataclass(frozen=True)
class ChatRequest:
    """Вход одного вызова. Неизменная часть — первое сообщение и схемы инструментов."""

    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    # Запрет инструментов — на последнем вызове хода; схемы при этом остаются во входе.
    # `None` — параметр не передаётся: запасной путь для сервера, который его отверг.
    tool_choice: Literal["auto", "none"] | None = "auto"
    # Требование к формату ответа; не входит в неизменную часть и в `prefix_hash`.
    response_format: dict[str, Any] | None = None
    # Предел времени этого вызова, секунды: не больше остатка срока хода. `None` — предел
    # клиента из настроек.
    timeout: float | None = None


class ModelError(Exception):
    """Ответа нет; ход завершается с `error`, расход уже сделанных вызовов сохранён."""


class TransientModelError(ModelError):
    """Превышение частоты, ошибка сервера, обрыв: запрос повторяется один раз."""


class FormatRejected(ModelError):
    """Сервер отверг запрос со строгими параметрами — форматом ответа или запретом
    инструментов — до запуска модели: вход не потрачен. Причина может быть и другой;
    это выясняет повтор без них."""


class ModelClient(Protocol):
    def complete(self, request: ChatRequest) -> ModelReply: ...
