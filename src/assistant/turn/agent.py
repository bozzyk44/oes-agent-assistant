"""Цикл хода: модель предлагает, код решает.

Не больше `MAX_CALLS` вызовов модели; последний — с запретом инструментов и, по умолчанию,
с принуждением к формату ответа. Каждый отправленный запрос, включая повтор после
временной ошибки, считается в лимит. Отказ сервера из-за параметра формата лимит не тратит:
сервер отверг параметр до запуска модели.

Токены, обрезка результата инструмента и проверка бюджета перед отправкой — отдельный
слой (бюджет хода); здесь — лимит числа вызовов и порядок шагов.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from assistant.access import Status, data_periods, load_grants
from assistant.actions.draft import TOOL as REPAIR_TOOL
from assistant.config import ModelSettings
from assistant.db.session import transaction
from assistant.model import (
    ChatRequest,
    FormatRejected,
    ModelClient,
    ModelError,
    ModelReply,
    TransientModelError,
)
from assistant.tools.box import ToolBox, ToolResult, envelope
from assistant.turn.calls import NOT_EXECUTED, plan_calls
from assistant.turn.context import actions_block, build_messages, load_history, user_info
from assistant.turn.outcome import ToolStatus, resolve_outcome
from assistant.turn.parse import parse_final, strip_thinking
from assistant.turn.prefix import PREFIX, Prefix, canonical
from assistant.turn.runner import (
    CitationRef,
    StoredMessage,
    TurnContext,
    TurnResult,
    draft_result,
)

log = logging.getLogger(__name__)

MAX_CALLS = 4
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "turn_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["answer", "refusal", "no_data"]},
                "answer": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["outcome", "answer", "citations"],
            "additionalProperties": False,
        },
    },
}
REPEATED = "Результат этого вызова с теми же аргументами уже есть выше; повтор не выполнен."
EMPTY_ANSWER = "Не удалось получить ответ модели."
BUDGET_ANSWER = "Ход остановлен на лимите вызовов модели, ответа нет."


@dataclass
class _State:
    """То, что накапливает ход: вход, статусы, фрагменты во входе, запись."""

    messages: list[dict[str, Any]]
    seen_chunks: set[str]
    statuses: list[ToolStatus] = field(default_factory=list)
    calls: dict[str, ToolResult] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=lambda: {"calls": []})


class AgentHandler:
    """Тело хода с настоящей моделью; модель подставляется — в тестах заглушка."""

    def __init__(
        self, engine: Engine, model: ModelClient, settings: ModelSettings, prefix: Prefix = PREFIX
    ) -> None:
        self._engine = engine
        self._model = model
        self._prefix = prefix
        self._format_mode = settings.response_format
        self._timeout = settings.timeout_seconds
        # Сервер отверг строгие параметры, а повтор без них прошёл — они выключены до
        # перезапуска процесса.
        self._format_disabled = False
        self.prefix_hash = prefix.hash

    def __call__(self, context: TurnContext) -> TurnResult:
        user, session = context.user_id, context.session_id
        with transaction(self._engine, user) as connection:
            grants = load_grants(connection, user)
            if grants is None:
                raise ModelError("пользователь не найден")
            info = user_info(grants, data_periods(connection, grants))
            history = load_history(connection, user, session)
            actions = actions_block(connection, user, session)
        box = ToolBox(lambda: transaction(self._engine, user), grants, context.message)
        state = _State(
            messages=[
                self._prefix.system_message(),
                *build_messages(info, history, actions, context.message),
            ],
            seen_chunks={chunk for item in history for chunk in item.chunk_ids},
        )
        state.record["history_chunks"] = sorted(state.seen_chunks)

        sent = 0
        transient = 0
        while sent < MAX_CALLS:
            if context.deadline.expired():
                raise TimeoutError("предел времени хода")
            last = sent == MAX_CALLS - 1
            request = self._request(state.messages, last, context.deadline.remaining())
            try:
                reply = self._complete(request, state)
            except TransientModelError as error:
                # Повтор — один, и он тоже считается в лимит вызовов.
                sent += 1
                transient += 1
                context.usage.record(0, 0)
                state.record["calls"].append({"error": str(error), "last": last})
                if transient > 1:
                    raise ModelError("временная ошибка модели повторилась") from None
                continue
            sent += 1
            usage = reply.usage
            context.usage.record(
                usage.input_tokens or 0, usage.output_tokens or 0, usage.cached_input_tokens
            )
            call_record: dict[str, Any] = {
                "last": last,
                "tool_choice": request.tool_choice,
                "response_format": request.response_format is not None,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cached_input_tokens": usage.cached_input_tokens,
                },
                "content": strip_thinking(reply.content or ""),
                "truncated": reply.truncated,
                "tool_calls": [],
            }
            state.record["calls"].append(call_record)

            if reply.tool_calls and not last:
                drafted = self._run_tools(reply, box, state, call_record)
                if drafted is not None:
                    return self._draft(context, state, drafted)
                continue
            if reply.tool_calls:
                # Запрет инструментов нарушен: вызов не выполняется.
                state.record["tool_calls_after_ban"] = [call.name for call in reply.tool_calls]
            # Оборванный ответ итогом не считается: его текст — обрубок.
            return self._finish(context, state, None if reply.truncated else reply, at_limit=last)
        return self._finish(context, state, None, at_limit=True)

    def _request(self, messages: list[dict[str, Any]], last: bool, remaining: float) -> ChatRequest:
        strict = not self._format_disabled
        enforce = strict and (self._format_mode == "all" or (self._format_mode == "last" and last))
        return ChatRequest(
            messages=tuple(messages),
            tools=self._prefix.tools,
            tool_choice=("none" if strict else None) if last else "auto",
            response_format=ANSWER_SCHEMA if enforce else None,
            # Вызов не переживает срок хода: иначе ход пережил бы и отметку занятости сессии.
            timeout=min(self._timeout, remaining),
        )

    def _complete(self, request: ChatRequest, state: _State) -> ModelReply:
        try:
            return self._model.complete(request)
        except FormatRejected:
            # Повтор без строгих параметров. Выключаются они, только если повтор прошёл:
            # иначе отказ был по другой причине — длина входа, шаблон чата.
            state.record["format_rejected"] = True
            plain = ChatRequest(request.messages, request.tools, None, None, request.timeout)
            reply = self._model.complete(plain)
            if not self._format_disabled:
                log.warning("сервер модели отверг строгие параметры; принуждение выключено")
            self._format_disabled = True
            return reply

    def _run_tools(
        self, reply: ModelReply, box: ToolBox, state: _State, call_record: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Выполняет вызовы ответа; возвращает параметры черновика, если он создан."""
        plan = plan_calls(reply.tool_calls)
        state.messages.append(
            {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in reply.tool_calls
                ],
            }
        )
        results: dict[str, str] = {}
        for call in plan.run:
            key = canonical([call.name, _arguments_key(call.arguments)])
            if key in state.calls:
                results[call.call_id] = envelope(Status.OK, note=REPEATED)
                call_record["tool_calls"].append({"name": call.name, "repeated": True})
                continue
            result = box.run(call.name, call.arguments)
            state.calls[key] = result
            state.statuses.append(
                ToolStatus(
                    call.name,
                    result.status,
                    best_match_closed=result.best_match_closed,
                    partially_denied=result.partially_denied,
                )
            )
            state.seen_chunks.update(result.chunk_ids)
            call_record["tool_calls"].append(
                {
                    "name": call.name,
                    "arguments": call.arguments,
                    "status": result.status.value,
                    "result": result.content,
                    **result.record,
                }
            )
            if call.name == REPAIR_TOOL and result.draft is not None:
                return result.draft
            results[call.call_id] = result.content
        for call in plan.skipped:
            results[call.call_id] = envelope(Status.INVALID, note=NOT_EXECUTED)
            call_record["tool_calls"].append({"name": call.name, "skipped": True})
        for call in reply.tool_calls:
            state.messages.append(
                {"role": "tool", "tool_call_id": call.call_id, "content": results[call.call_id]}
            )
        return None

    def _draft(self, context: TurnContext, state: _State, params: dict[str, Any]) -> TurnResult:
        state.record["parse_path"] = "draft"
        result = draft_result(self.prefix_hash, REPAIR_TOOL, params, state.record)
        return TurnResult(
            outcome=result.outcome,
            answer=result.answer,
            prefix_hash=result.prefix_hash,
            record=state.record,
            messages=(
                StoredMessage("user", {"text": context.message}),
                StoredMessage("assistant", {"text": result.answer}),
            ),
            action=result.action,
        )

    def _finish(
        self, context: TurnContext, state: _State, reply: ModelReply | None, *, at_limit: bool
    ) -> TurnResult:
        parsed = parse_final(reply.content if reply else None)
        state.record["parse_path"] = parsed.path
        if not parsed.is_final or not parsed.answer:
            outcome, answer = (
                ("budget_exceeded", BUDGET_ANSWER) if at_limit else ("error", EMPTY_ANSWER)
            )
            state.record["outcome_row"] = None
            return TurnResult(outcome, answer, self.prefix_hash, record=state.record)

        accepted, dropped = _citations(parsed.citations, state.seen_chunks)
        if dropped:
            state.record["dropped_citations"] = list(dropped)
        verdict = resolve_outcome(
            state.statuses, proposed=parsed.outcome, cited=bool(accepted), action_drafted=False
        )
        state.record["outcome_row"] = verdict.row
        if parsed.outcome != verdict.outcome:
            state.record["outcome_replaced"] = {"proposed": parsed.outcome, "set": verdict.outcome}
        citations = tuple(CitationRef(chunk.split("#", 1)[0], chunk) for chunk in accepted)
        return TurnResult(
            outcome=verdict.outcome,
            answer=parsed.answer,
            prefix_hash=self.prefix_hash,
            citations=citations,
            record=state.record,
            messages=(
                StoredMessage("user", {"text": context.message}),
                StoredMessage("assistant", {"text": parsed.answer}, tuple(accepted)),
            ),
        )


def _arguments_key(raw: str) -> Any:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return raw


def _citations(cited: tuple[str, ...], seen: set[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Цитата принимается, только если фрагмент был во входе этого хода или в истории,
    вошедшей в него. Ссылка на раздел принимает и его части: `<док>#2` — для `<док>#2.1`.
    """
    accepted: list[str] = []
    dropped: list[str] = []
    for chunk in cited:
        exact = [item for item in seen if item == chunk or item.startswith(f"{chunk}.")]
        if exact:
            accepted.append(chunk)
        else:
            dropped.append(chunk)
    return tuple(dict.fromkeys(accepted)), tuple(dropped)
