"""Заглушка модели по HTTP для сценария сборки: тот же `ScriptedModel`, что в тестах,
сценарий задаётся снаружи, ответы — в формате OpenAI."""

import json
from typing import Any

from fastapi.testclient import TestClient

from tests.modelserver import http_app
from tests.modelstub import ScriptedModel


def test_scripted_model_speaks_openai_format() -> None:
    model = ScriptedModel([])
    client = TestClient(http_app(model))
    steps = [
        {"tool_calls": [{"name": "create_repair_request", "arguments": {"equipment_id": "E-1"}}]},
        {"final": {"answer": "готово", "outcome": "action"}},
    ]
    assert client.put("/script", json={"steps": steps}).json() == {"steps": 2}

    def ask() -> dict[str, Any]:
        body = {"model": "m", "messages": [{"role": "user", "content": "E-1"}]}
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        choice: dict[str, Any] = response.json()["choices"][0]
        return choice

    first = ask()
    assert first["finish_reason"] == "tool_calls"
    (tool_call,) = first["message"]["tool_calls"]
    assert tool_call["function"]["name"] == "create_repair_request"
    assert json.loads(tool_call["function"]["arguments"]) == {"equipment_id": "E-1"}
    second = ask()
    assert json.loads(second["message"]["content"])["outcome"] == "action"
    assert client.get("/requests").json() == {"requests": 2}
    # Сценарий исчерпан — ошибка сервера, как от сбоя модели, а не выдуманный ответ.
    exhausted = client.post("/v1/chat/completions", json={"messages": []})
    assert exhausted.status_code == 500
    # Новый сценарий сбрасывает и шаги, и счёт запросов.
    client.put("/script", json={"steps": []})
    assert client.get("/requests").json() == {"requests": 0}
