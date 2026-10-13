"""Схемы запросов и ответов API — контракт задания, раздел «API»."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Outcome = Literal["answer", "refusal", "no_data", "action", "budget_exceeded", "error"]


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1)


class Citation(BaseModel):
    doc_id: str
    chunk_id: str


class PendingAction(BaseModel):
    action_id: str
    tool: str
    params: dict[str, object]


class Usage(BaseModel):
    """Сумма по всем вызовам модели в ходе."""

    model_calls: int = 0
    input_tokens: int = 0
    # Если провайдер не отдаёт — null.
    cached_input_tokens: int | None = None
    output_tokens: int = 0
    max_call_input_tokens: int = 0
    prefix_hash: str
    latency_ms: int = 0


class TurnResponse(BaseModel):
    turn_id: str
    outcome: Outcome
    answer: str
    citations: list[Citation] = []
    pending_action: PendingAction | None = None
    usage: Usage


class ActionDone(BaseModel):
    status: Literal["done"] = "done"
    request_id: str


class ActionRejected(BaseModel):
    status: Literal["rejected"] = "rejected"
