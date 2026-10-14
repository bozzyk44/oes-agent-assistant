"""Приведение регистра и похожих букв явными таблицами — одинаково в коде и в базе.

Модель переносит идентификаторы из русского вопроса и пишет кириллические «Е», «Х», «К»
вместо латинских. Таблицы заданы явно, а не функциями `upper`/`lower`: их поведение в базе
зависит от локали сервера, а ключ должен совпадать в коде и в базе. Те же таблицы стоят
в миграции 0004.
"""

import re

LOWER = "abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
# После перевода в верхний регистр: кириллические буквы, неотличимые от латинских.
LOOKALIKE_FROM = "АВЕКМНОРСТУХ"
LOOKALIKE_TO = "ABEKMHOPCTYX"
# Разделители, которые ключ идентификатора убирает: пробелы, включая неразрывные,
# дефис и тире разных видов.
SEPARATORS = " \t\n\r\f\v  -‐‑‒–—―−"

_TO_UPPER = str.maketrans(LOWER, UPPER)
_TO_LOWER = str.maketrans(UPPER, LOWER)
_TO_LATIN = str.maketrans(LOOKALIKE_FROM, LOOKALIKE_TO)
_SEPARATORS = re.compile("[" + re.escape(SEPARATORS) + "]")


def fold(text: str) -> str:
    """Верхний регистр, похожие кириллические буквы — латинскими."""
    return text.translate(_TO_UPPER).translate(_TO_LATIN)


def fold_lower(text: str) -> str:
    """То же, но в нижнем регистре: для терминов поиска, которые хранятся строчными."""
    return fold(text).translate(_TO_LOWER)


def equipment_key(equipment_id: str) -> str:
    """Ключ сравнения идентификатора оборудования: без регистра и разделителей, похожие
    кириллические буквы приведены к латинским. Совпадает с функцией ключа в базе.
    """
    return fold(_SEPARATORS.sub("", equipment_id))
