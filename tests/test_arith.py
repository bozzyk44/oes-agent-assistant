"""Выражение инструмента расчёта: только числа, четыре действия и скобки."""

from decimal import Decimal

import pytest

from assistant.tools.arith import MAX_DEPTH, BadExpression, evaluate


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.1 + 0.2", "0.3"),
        ("61777 / (2290 * 28) * 100", None),
        ("2 290 × 2 × 14", "64120"),
        ("2,5 * 4", "10.0"),
        ("-(3 - 5) ÷ 4", "0.5"),
        ("+7 − 2", "5"),
    ],
)
def test_arithmetic(text: str, expected: str | None) -> None:
    value = evaluate(text)
    if expected is None:
        assert round(value, 1) == Decimal("96.3")
    else:
        assert value == Decimal(expected)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "__import__('os').system('true')",
        "x + 1",
        "2 ** 10",
        "abs(-1)",
        "(1).real",
        "True + 1",
        "[1, 2]",
        "1; 2",
        "'1' + '2'",
        "1 % 2",
        "1" + " + 1" * 200,
    ],
)
def test_anything_but_arithmetic_is_refused(text: str) -> None:
    with pytest.raises(BadExpression):
        evaluate(text)


def test_nesting_is_bounded() -> None:
    depth = MAX_DEPTH + 5
    with pytest.raises(BadExpression):
        evaluate("(" * depth + "1" + ")" * depth + "+" + "-" * depth + "1")


def test_division_by_zero_is_an_arithmetic_error() -> None:
    with pytest.raises(ZeroDivisionError):
        evaluate("1 / 0")
