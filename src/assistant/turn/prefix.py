"""Неизменная часть входа: системное сообщение и схемы инструментов.

Собирается один раз при старте; в ней нет времени, идентификаторов хода и сессии, имени
и прав пользователя. Её канонический вид с фиксированным порядком — основа `prefix_hash`:
хэш не меняется между вызовами и ходами.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant.tools.schemas import TOOL_SCHEMAS

SYSTEM_PROMPT = (Path(__file__).parent / "system.md").read_text(encoding="utf-8").strip()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Prefix:
    system: str
    tools: tuple[dict[str, Any], ...]

    @property
    def hash(self) -> str:
        # Порядок инструментов — часть входа и хэшируется как есть; порядок полей внутри
        # схем — нет: он канонизируется.
        return hashlib.sha256(
            canonical({"system": self.system, "tools": list(self.tools)}).encode()
        ).hexdigest()

    def system_message(self) -> dict[str, Any]:
        return {"role": "system", "content": self.system}


PREFIX = Prefix(SYSTEM_PROMPT, TOOL_SCHEMAS)
