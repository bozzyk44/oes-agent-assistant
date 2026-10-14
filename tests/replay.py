"""Заглушка модели, которая проигрывает записанные ходы прогона.

Из записи хода (`turn_record` в `results.jsonl`) восстанавливаются ответы модели
по порядку вызовов: текст и запрошенные инструменты с аргументами. Номер вызова — число
ответов модели после последнего вопроса во входе. Так прогон в CI идёт без модели и сети,
а проверки, отчёт и сам цикл хода получают настоящие ответы модели.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assistant.model import ChatRequest, ModelReply, ToolCall, UsageReport


def _reply(call: dict[str, Any], number: int) -> ModelReply:
    usage = call.get("usage") or {}
    tool_calls = tuple(
        ToolCall(f"r{number}-{index}", str(item["name"]), str(item.get("arguments", "")))
        for index, item in enumerate(call.get("tool_calls", []))
    )
    return ModelReply(
        content=call.get("content") or None,
        tool_calls=tool_calls,
        usage=UsageReport(
            usage.get("input_tokens"), usage.get("output_tokens"), usage.get("cached_input_tokens")
        ),
        truncated=bool(call.get("truncated")),
    )


@dataclass
class ReplayModel:
    # Вопрос → ответы модели по порядку вызовов, по одному списку на каждую попытку.
    turns: dict[str, list[list[ModelReply]]] = field(default_factory=dict)
    requests: list[ChatRequest] = field(default_factory=list)

    @classmethod
    def from_results(cls, path: Path) -> ReplayModel:
        model = cls()
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            record = item.get("turn_record")
            if item.get("type") != "attempt" or not record:
                continue
            calls = [call for call in record.get("calls", []) if "error" not in call]
            replies = [_reply(call, number) for number, call in enumerate(calls)]
            model.turns.setdefault(item["question"]["question"], []).append(replies)
        return model

    def complete(self, request: ChatRequest) -> ModelReply:
        self.requests.append(request)
        messages = list(request.messages)
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        question = str(messages[last_user]["content"]).rsplit("\n\n", 1)[-1]
        number = sum(1 for m in messages[last_user:] if m["role"] == "assistant")
        attempts = self.turns[question]
        replies = attempts[0]
        if number == len(replies) - 1 and len(attempts) > 1:
            # Последний вызов этой попытки: следующая попытка берёт свою запись.
            attempts.pop(0)
        return replies[min(number, len(replies) - 1)]
