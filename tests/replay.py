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


def extract(path: Path) -> list[dict[str, Any]]:
    """Из `results.jsonl` — только то, что нужно для проигрывания: ответы модели по вызовам
    и записанные вердикты. Результатов инструментов нет: их заново считает сервис."""
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("type") == "config":
            keys = ("run_id", "model", "prefix_hash", "dataset_hash", "questions_hash")
            out.insert(0, {"type": "config", **{key: item.get(key) for key in keys}})
            continue
        record = item.get("turn_record")
        if item.get("type") != "attempt" or not record:
            continue
        calls = [
            {
                "content": call.get("content"),
                "tool_calls": [
                    {"name": t["name"], "arguments": t.get("arguments", "")}
                    for t in call.get("tool_calls", [])
                ],
                "usage": call.get("usage"),
                "truncated": call.get("truncated", False),
            }
            for call in record.get("calls", [])
            if "error" not in call
        ]
        response = item.get("response") or {}
        out.append(
            {
                "type": "turn",
                "question": item["question"],
                "attempt": item["attempt"],
                "calls": calls,
                "passed": item["passed"],
                "outcome": response.get("outcome"),
            }
        )
    return out


@dataclass
class ReplayModel:
    # Вопрос → ответы модели по порядку вызовов, по одному списку на каждую попытку.
    turns: dict[str, list[list[ModelReply]]] = field(default_factory=dict)
    requests: list[ChatRequest] = field(default_factory=list)
    _current: dict[str, tuple[list[ModelReply], int]] = field(default_factory=dict)

    @classmethod
    def from_results(cls, path: Path) -> ReplayModel:
        return cls.from_turns(extract(path)[1:])

    @classmethod
    def from_turns(cls, turns: list[dict[str, Any]]) -> ReplayModel:
        model = cls()
        owners: dict[str, str] = {}
        for turn in turns:
            text, qid = turn["question"]["question"], turn["question"]["id"]
            if owners.setdefault(text, qid) != qid:
                raise ValueError(f"один текст у вопросов {owners[text]} и {qid}: не различить")
            replies = [_reply(call, number) for number, call in enumerate(turn["calls"])]
            model.turns.setdefault(text, []).append(replies)
        return model

    def complete(self, request: ChatRequest) -> ModelReply:
        """Первый вызов хода берёт следующую записанную попытку этого вопроса; вызов сверх
        записанного или записанная попытка, проигранная не до конца, — расхождение, тест
        падает, а не повторяет последний ответ."""
        self.requests.append(request)
        messages = list(request.messages)
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        question = str(messages[last_user]["content"]).rsplit("\n\n", 1)[-1]
        number = sum(1 for m in messages[last_user:] if m["role"] == "assistant")
        if number == 0:
            current = self._current.get(question)
            if current is not None and current[1] < len(current[0]):
                raise AssertionError(f"ход «{question}» кончился раньше записанного")
            if not self.turns.get(question):
                raise AssertionError(f"нет записанной попытки для «{question}»")
            self._current[question] = (self.turns[question].pop(0), 0)
        replies, used = self._current[question]
        if number >= len(replies):
            raise AssertionError(f"ход «{question}» просит вызов {number + 1} сверх записанных")
        self._current[question] = (replies, max(used, number + 1))
        return replies[number]

    def assert_done(self) -> None:
        """Все записанные попытки проиграны до конца."""
        left = {question: len(items) for question, items in self.turns.items() if items}
        unfinished = [q for q, (replies, used) in self._current.items() if used < len(replies)]
        assert not left, f"не проиграны попытки: {left}"
        assert not unfinished, f"ходы кончились раньше записанного: {unfinished}"


if __name__ == "__main__":
    import sys

    lines = extract(Path(sys.argv[1]))
    target = Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8"
    )
