"""Числа в тексте: запись, сравнение строк по правилу задания и нижняя оценка длины в токенах."""

import re

# Наибольшее измеренное число символов на токен для русского текста — 4,7 (DECISIONS.md,
# замеры моделей). Делитель взят выше: оценка занижает длину, и документ, прошедший порог
# по ней, длиннее порога у любой из измеренных моделей.
CHARS_PER_TOKEN_UPPER = 5

# Только пробелы — обычный, неразрывный, узкий неразрывный и тонкий, которыми разделяют
# разряды: перевод строки числа не склеивает.
_SPACE_BETWEEN_DIGITS = re.compile(r"(?<=\d)[ \u00a0\u202f\u2009]+(?=\d)")
_COMMA_BETWEEN_DIGITS = re.compile(r"(?<=\d),(?=\d)")


def normalize(text: str) -> str:
    """Сравнение строк из задания: без регистра, `58 075` = `58075`, `38,1` = `38.1`."""
    text = _SPACE_BETWEEN_DIGITS.sub("", text.lower())
    return _COMMA_BETWEEN_DIGITS.sub(".", text)


def min_tokens(text: str) -> int:
    """Нижняя оценка числа токенов."""
    return len(text) // CHARS_PER_TOKEN_UPPER


def fmt_int(value: int) -> str:
    """Целое с пробелом между разрядами: 58 075."""
    return f"{value:,}".replace(",", " ")


def tenths_of_hours(minutes: int) -> int:
    """Минуты в десятых долях часа, с округлением до ближайшей."""
    return (minutes * 10 + 30) // 60


def fmt_tenths(tenths: int) -> str:
    """Десятые доли в записи с запятой: 381 → 38,1."""
    return f"{tenths // 10},{tenths % 10}"
