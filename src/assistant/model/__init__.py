"""Клиент модели за интерфейсом. Он отправляет готовый вход и возвращает ответ
с фактическим расходом; о правах и бюджете не знает — всё проверено до него.
"""

from assistant.model.client import (
    ChatRequest,
    FormatRejected,
    ModelClient,
    ModelError,
    ModelReply,
    ToolCall,
    TransientModelError,
    UsageReport,
)

__all__ = [
    "ChatRequest",
    "FormatRejected",
    "ModelClient",
    "ModelError",
    "ModelReply",
    "ToolCall",
    "TransientModelError",
    "UsageReport",
]
