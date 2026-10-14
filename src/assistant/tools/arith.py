"""Арифметическое выражение для инструмента расчёта: числа, четыре действия и скобки.

Разбор — синтаксическим деревом Python с белым списком узлов; ничего не исполняется, имён
и вызовов нет. Числа — `Decimal` из записи модели, а не результат арифметики над `float`:
`0.1 + 0.2` даёт `0.3`. Литерал длиннее 17 значащих цифр проходит через `float` разбора
и теряет хвост — для показателей рудника такой точности нет.
"""

import ast
import operator
import re
from collections.abc import Callable
from decimal import Decimal

MAX_LENGTH = 300
# Глубина дерева: выражение из ответа модели не должно разворачиваться без предела. Сумма
# из многих слагаемых — тоже глубина: `a + b + c` — цепочка вложенных действий.
MAX_DEPTH = 100
_SPACE = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d)")
_COMMA = re.compile(r"(?<=\d),(?=\d)")
NUMBER = re.compile(r"\d+(?:\.\d+)?")

_OPERATORS: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class BadExpression(ValueError):
    """Выражение не разобрано: в тексте причины — что не так, без исполнения."""


def evaluate(text: str) -> Decimal:
    """Значение выражения. Деление на ноль — `ZeroDivisionError` или `decimal`-исключение."""
    return _value(_parse(text), 0)


def literals(text: str) -> list[Decimal]:
    """Числа выражения — те же, что идут в расчёт: `1e5` и `114_300` — одно число каждое,
    а не несколько групп цифр."""
    return [
        _constant(node)
        for node in ast.walk(_parse(text))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ]


def _parse(text: str) -> ast.expr:
    if not text.strip() or len(text) > MAX_LENGTH:
        raise BadExpression(f"ожидалось непустое выражение до {MAX_LENGTH} символов")
    # Модели пишут знаки по-разному. Пробел между цифрами — разряды, запятая — десятичная:
    # как в сравнении строк прогона.
    prepared = normalize_digits(text.replace("×", "*").replace("÷", "/").replace("−", "-"))
    try:
        tree = ast.parse(prepared.strip(), mode="eval")
    except SyntaxError:
        raise BadExpression("выражение не разобрано: только числа, + - * / и скобки") from None
    return tree.body


def _constant(node: ast.Constant) -> Decimal:
    value = node.value
    return Decimal(value) if isinstance(value, int) else Decimal(ast.unparse(node))


def normalize_digits(text: str) -> str:
    """Пробел между цифрами убирается, запятая между цифрами — точка: как в сравнении строк
    прогона."""
    return _COMMA.sub(".", _SPACE.sub("", text))


def _value(node: ast.AST, depth: int) -> Decimal:
    if depth > MAX_DEPTH:
        raise BadExpression("слишком глубокая вложенность")
    match node:
        case ast.Constant(value=bool()):
            raise BadExpression("в выражении — не число")
        case ast.Constant(value=int() | float()):
            return _constant(node)
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_value(operand, depth + 1)
        case ast.UnaryOp(op=ast.UAdd(), operand=operand):
            return _value(operand, depth + 1)
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _OPERATORS:
            return _OPERATORS[type(op)](_value(left, depth + 1), _value(right, depth + 1))
    raise BadExpression("в выражении допустимы только числа, + - * / и скобки")
