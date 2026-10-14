"""Разбор итога хода: основной путь — JSON, запасной — текст; размышления вырезаются."""

import pytest

from assistant.turn.parse import parse_final


@pytest.mark.parametrize(
    ("content", "path", "answer", "outcome", "citations"),
    [
        (
            '{"outcome": "answer", "answer": "Да", "citations": ["D1#2"]}',
            "json",
            "Да",
            "answer",
            ("D1#2",),
        ),
        (
            '```json\n{"outcome": "no_data", "answer": "Нет", "citations": []}\n```',
            "json",
            "Нет",
            "no_data",
            (),
        ),
        (
            'Вот ответ: {"outcome": "refusal", "answer": "Нельзя", "citations": []} — всё.',
            "json",
            "Нельзя",
            "refusal",
            (),
        ),
        (
            '{"answer": "Только текст [D2#1]"}',
            "json_partial",
            "Только текст [D2#1]",
            None,
            ("D2#1",),
        ),
        (
            '{"outcome": "action", "answer": "Сам решил", "citations": []}',
            "json_partial",
            "Сам решил",
            None,
            (),
        ),
        (
            "Простой текст со ссылкой [D3#4.1] и без",
            "text",
            "Простой текст со ссылкой [D3#4.1] и без",
            None,
            ("D3#4.1",),
        ),
        (
            '{"outcome": "answer", "answer": "обрыв',
            "text",
            '{"outcome": "answer", "answer": "обрыв',
            None,
            (),
        ),
        (
            '<think>посчитаю {"x": 1}</think>'
            '{"outcome": "answer", "answer": "Итог", "citations": []}',
            "json",
            "Итог",
            "answer",
            (),
        ),
        (
            '{"outcome": "answer", "answer": "Кавычки \\" и\\nперенос", "citations": []}',
            "json",
            'Кавычки " и\nперенос',
            "answer",
            (),
        ),
    ],
)
def test_parse_final(
    content: str, path: str, answer: str, outcome: str | None, citations: tuple[str, ...]
) -> None:
    parsed = parse_final(content)
    assert (parsed.path, parsed.answer, parsed.outcome, parsed.citations) == (
        path,
        answer,
        outcome,
        citations,
    )


def test_object_without_answer_is_not_final() -> None:
    # Похоже на вызов инструмента текстом: итогом не считается и не выполняется.
    parsed = parse_final('{"name": "query_kpi", "arguments": {"table": "production"}}')
    assert (parsed.is_final, parsed.answer) == (False, "")


def test_unclosed_thinking_is_dropped() -> None:
    assert parse_final("<think>рассуждения без конца").answer == ""
    assert parse_final(None).answer == ""
