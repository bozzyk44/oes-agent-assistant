"""Заглушка модели: заданная последовательность ответов и запись каждого входа.

Тесты прав ищут запрещённое в записанных входах: утечка — то, что модель увидела, а не то,
что она пересказала.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from assistant.model import ChatRequest, ModelReply, ToolCall, UsageReport

Step = ModelReply | Exception | Callable[[ChatRequest], ModelReply]
_ids = count(1)


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(f"c{next(_ids)}", name, json.dumps(arguments, ensure_ascii=False))


def tools(*calls: ToolCall, content: str | None = None) -> ModelReply:
    return ModelReply(content, calls, UsageReport(100, 10, None))


def final(answer: str, outcome: str = "answer", citations: list[str] | None = None) -> ModelReply:
    body = {"outcome": outcome, "answer": answer, "citations": citations or []}
    return ModelReply(json.dumps(body, ensure_ascii=False), (), UsageReport(120, 20, 50))


def text(content: str) -> ModelReply:
    return ModelReply(content, (), UsageReport(120, 20, None))


@dataclass
class ScriptedModel:
    steps: list[Step]
    requests: list[ChatRequest] = field(default_factory=list)
    # Ответ, когда сценарий кончился: модель «хочет» вызывать инструменты без конца.
    tail: Step | None = None

    def complete(self, request: ChatRequest) -> ModelReply:
        self.requests.append(request)
        step = self.steps.pop(0) if self.steps else self.tail
        if step is None:
            raise AssertionError("сценарий модели исчерпан")
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(request)
        return step

    def inputs(self) -> str:
        """Все входы целиком, одной строкой: для поиска запрещённого."""
        return "\n".join(
            json.dumps([list(request.messages), list(request.tools)], ensure_ascii=False)
            for request in self.requests
        )
