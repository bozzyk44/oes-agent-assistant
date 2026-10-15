"""Заглушка модели по HTTP в формате OpenAI — для сервиса в контейнере.

Сервис ходит в модель только через OpenAI-совместимый адрес. Чтобы проверить связку
целиком — сервис, его клиент модели и сеть compose, — заглушка поднимается отдельным
контейнером. Внутри — те же объекты, что в тестах: сценарная модель (`ScriptedModel`)
для сценария сборки и послушная модель для мутантов.

    python -m tests.modelserver      сценарная модель на порту 8003

Сценарий задаёт тот, кто проверяет: `PUT /script` со списком шагов. Шаг — вызовы
инструментов `{"tool_calls": [{"name": …, "arguments": {…}}]}` или итог
`{"final": {"answer": …, "outcome": …, "citations": […]}}`. Ключей и адресов заглушка
не знает; данных набора — тоже: шаги приходят снаружи.
"""

import os
from collections.abc import Mapping
from typing import Any, Protocol

from assistant.model import ChatRequest, ModelReply
from tests.modelstub import ScriptedModel, Step, call, final, tools

PORT = 8003


class Model(Protocol):
    def complete(self, request: ChatRequest) -> ModelReply: ...


def openai_reply(model: Model, body: Mapping[str, Any]) -> dict[str, Any]:
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
                "id": item.call_id,
                "type": "function",
                "function": {"name": item.name, "arguments": item.arguments},
            }
            for item in reply.tool_calls
        ]
    return {
        "id": "stub",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model", "stub"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def scripted_steps(raw: list[Mapping[str, Any]]) -> list[Step]:
    """Шаги сценария из JSON — теми же конструкторами, что в тестах."""
    steps: list[Step] = []
    for step in raw:
        if "tool_calls" in step:
            steps.append(
                tools(*(call(item["name"], **item["arguments"]) for item in step["tool_calls"]))
            )
        else:
            item = step["final"]
            steps.append(
                final(item["answer"], item.get("outcome", "answer"), item.get("citations"))
            )
    return steps


def http_app(model: Model) -> Any:
    from fastapi import FastAPI, HTTPException, Request

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def complete(request: Request) -> dict[str, Any]:
        try:
            return openai_reply(model, await request.json())
        except AssertionError as error:
            # Сценарий исчерпан: сервис получит ошибку сервера, как от сбоя модели.
            raise HTTPException(500, str(error)) from None

    if isinstance(model, ScriptedModel):

        @app.put("/script")
        async def script(request: Request) -> dict[str, int]:
            body = await request.json()
            model.steps = scripted_steps(body["steps"])
            model.requests.clear()
            return {"steps": len(model.steps)}

        @app.get("/requests")
        def requests() -> dict[str, int]:
            return {"requests": len(model.requests)}

    return app


def main() -> int:
    import uvicorn

    port = int(os.environ.get("MODEL_STUB_PORT", PORT))
    uvicorn.run(http_app(ScriptedModel([])), host="0.0.0.0", port=port)  # noqa: S104 — сеть compose
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
