"""Послушная модель: запрашивает запрещённое прямо и пересказывает всё, что получила.

Настоящая модель может не попросить закрытое или не послушаться инструкции из документа,
и мутант останется незамеченным по её заслуге. Эта модель делает ровно то, что записано
в атаке вопроса (`questions.attacks.json`), и переносит в ответ данные инструментов как есть:
дошли запрещённые данные до входа — они будут и в ответе.

Порядок атаки: сначала все вызовы чтения одним ответом, затем подготовка заявки отдельным —
в одном ответе с ней остальные вызовы не выполняются. Ответ: из поиска — первый фрагмент
(модель верит лучшему совпадению), из документа — все фрагменты, из КХД — данные целиком.
Исход — `answer`, если данные есть, иначе `refusal`; окончательный исход ставит код.

Та же модель доступна по HTTP в формате OpenAI — для мутантов в контейнерах.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant.model import ChatRequest, ModelReply, ToolCall, UsageReport
from assistant.tools.box import BLOCK_CLOSE, BLOCK_OPEN

ATTACKS_FILE = "questions.attacks.json"
REPAIR = "create_repair_request"
SEARCH = "search_documents"
# Расход — единица: поправка оценки токенов только растёт, и заглушка её не сдвинет.
_USAGE = UsageReport(1, 1, 0)
_BLOCK = re.compile(
    re.escape(BLOCK_OPEN) + r" id=(\S+) [^\n]*\n(.*?)\n" + re.escape(BLOCK_CLOSE), re.DOTALL
)

Call = dict[str, Any]


def load_attacks(questions: Path) -> dict[str, list[Call]]:
    """Текст вопроса → вызовы атаки; для всех формулировок из файла вопросов."""
    attacks = json.loads((questions.parent / ATTACKS_FILE).read_text(encoding="utf-8"))
    found: dict[str, list[Call]] = {}
    for line in questions.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        attack = attacks.get(str(record["id"]).split(".", 1)[0])
        if attack and attack["calls"]:
            found[record["question"]] = attack["calls"]
    return found


def steps(calls: list[Call]) -> list[list[Call]]:
    reads = [call for call in calls if call["tool"] != REPAIR]
    repairs = [[call] for call in calls if call["tool"] == REPAIR]
    return ([reads] if reads else []) + repairs


@dataclass
class ObedientModel:
    attacks: Mapping[str, list[Call]]

    def complete(self, request: ChatRequest) -> ModelReply:
        messages = list(request.messages)
        last = max(index for index, item in enumerate(messages) if item["role"] == "user")
        question = str(messages[last]["content"])
        after = messages[last + 1 :]
        done = sum(1 for item in after if item["role"] == "assistant" and item.get("tool_calls"))
        plan = steps(self._attack(question))
        if done < len(plan) and request.tool_choice != "none":
            calls = tuple(
                ToolCall(
                    f"a{done}-{number}",
                    call["tool"],
                    json.dumps(call["arguments"], ensure_ascii=False),
                )
                for number, call in enumerate(plan[done])
            )
            return ModelReply(None, calls, _USAGE)
        return ModelReply(json.dumps(answer(after), ensure_ascii=False), (), _USAGE)

    def _attack(self, question: str) -> list[Call]:
        # Текущее сообщение может идти после сведений о пользователе и действиях сессии.
        for text, calls in self.attacks.items():
            if question.rstrip().endswith(text):
                return calls
        return []


def answer(after: list[dict[str, Any]]) -> dict[str, Any]:
    """Итог из результатов инструментов этого хода: всё, что пришло, — в текст ответа."""
    names = {
        call["id"]: call["function"]["name"]
        for item in after
        if item["role"] == "assistant"
        for call in item.get("tool_calls") or []
    }
    parts: list[str] = []
    citations: list[str] = []
    for item in after:
        if item["role"] != "tool":
            continue
        content = str(item["content"])
        header_line, _, _ = content.partition("\n")
        try:
            header = json.loads(header_line)
        except ValueError:
            continue
        if not isinstance(header, dict) or header.get("status") != "ok":
            continue
        blocks = _BLOCK.findall(content)
        if blocks:
            if names.get(item.get("tool_call_id")) == SEARCH:
                blocks = blocks[:1]
            for chunk_id, body in blocks:
                parts.append(body)
                citations.append(chunk_id)
        elif "data" in header:
            parts.append(json.dumps(header["data"], ensure_ascii=False))
    return {
        "outcome": "answer" if parts else "refusal",
        "answer": "\n".join(parts) or "Нет доступа.",
        "citations": citations,
    }


def openai_reply(model: ObedientModel, body: Mapping[str, Any]) -> dict[str, Any]:
    """Ответ сервера в формате OpenAI на запрос в том же формате."""
    reply = model.complete(
        ChatRequest(
            messages=tuple(body.get("messages") or ()),
            tools=tuple(body.get("tools") or ()),
            tool_choice=body.get("tool_choice"),
        )
    )
    message: dict[str, Any] = {"role": "assistant", "content": reply.content}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in reply.tool_calls
        ]
    return {
        "id": "obedient",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model", "obedient"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def http_app(model: ObedientModel) -> Any:
    from fastapi import FastAPI, Request

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def complete(request: Request) -> dict[str, Any]:
        return openai_reply(model, await request.json())

    return app
