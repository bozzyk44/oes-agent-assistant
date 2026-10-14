"""Клиент OpenAI-совместимого API: адрес, ключ и имя модели — из окружения.

Ключ модели живёт только в объекте SDK: в журнал, ответы и запись хода он не попадает.
"""

from collections.abc import Mapping
from typing import Any

import openai

from assistant.model.client import (
    ChatRequest,
    FormatRejected,
    ModelError,
    ModelReply,
    ToolCall,
    TransientModelError,
    UsageReport,
)

# Поля кэшированного входа у разных провайдеров: стандартное и известные свои.
_CACHED_FIELDS = (("prompt_tokens_details", "cached_tokens"), (None, "prompt_cache_hit_tokens"))


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int,
        timeout: float,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        # Повторы делает цикл хода и считает их в лимит вызовов: у SDK свои выключены.
        self._client = openai.OpenAI(
            base_url=base_url, api_key=api_key or "none", max_retries=0, timeout=timeout
        )
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._extra_body = dict(extra_body or {})

    def complete(self, request: ChatRequest) -> ModelReply:
        arguments: dict[str, Any] = {
            "model": self._model,
            "messages": list(request.messages),
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
        }
        if request.tools:
            arguments["tools"] = list(request.tools)
            if request.tool_choice is not None:
                arguments["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            arguments["response_format"] = request.response_format
        if request.timeout is not None:
            arguments["timeout"] = request.timeout
        if self._extra_body:
            arguments["extra_body"] = self._extra_body
        try:
            response = self._client.chat.completions.create(**arguments)
        except (openai.BadRequestError, openai.UnprocessableEntityError) as error:
            if request.response_format is not None or request.tool_choice == "none":
                # Возможно, отказ из-за строгого параметра: сервер не запускал модель.
                raise FormatRejected(type(error).__name__) from None
            raise ModelError(f"сервер модели отклонил запрос: {error.status_code}") from None
        except (
            openai.RateLimitError,
            openai.InternalServerError,
            openai.APIConnectionError,
        ) as error:
            raise TransientModelError(type(error).__name__) from None
        except openai.APIStatusError as error:
            raise ModelError(f"сервер модели ответил {error.status_code}") from None
        except openai.OpenAIError as error:
            raise ModelError(type(error).__name__) from None
        return _reply(response)


def _reply(response: Any) -> ModelReply:
    if not response.choices:
        raise ModelError("в ответе модели нет вариантов")
    choice = response.choices[0]
    message = choice.message
    calls = tuple(
        ToolCall(call.id or f"call-{number}", call.function.name, call.function.arguments or "")
        for number, call in enumerate(message.tool_calls or [])
        if getattr(call, "function", None) is not None
    )
    return ModelReply(
        content=message.content,
        tool_calls=calls,
        usage=_usage(response.usage),
        truncated=getattr(choice, "finish_reason", None) == "length",
    )


def _usage(usage: Any) -> UsageReport:
    if usage is None:
        return UsageReport()
    cached: int | None = None
    for parent, name in _CACHED_FIELDS:
        holder = getattr(usage, parent, None) if parent else usage
        value = getattr(holder, name, None) if holder is not None else None
        if isinstance(value, int):
            cached = value
            break
    return UsageReport(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        cached_input_tokens=cached,
    )
