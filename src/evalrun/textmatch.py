"""Сравнение строк по правилам задания: без учёта регистра; пробелы между цифрами
удаляются, запятая между цифрами заменяется точкой. Так `58 075` совпадает с `58075`,
`38,1` — с `38.1`.
"""

import re

# Пробельные символы между цифрами, включая неразрывный и узкий неразрывный пробел.
_SPACE_IN_NUMBER = re.compile(r"(?<=\d)\s+(?=\d)")
_COMMA_IN_NUMBER = re.compile(r"(?<=\d),(?=\d)")


def normalize(text: str) -> str:
    text = _SPACE_IN_NUMBER.sub("", text)
    text = _COMMA_IN_NUMBER.sub(".", text)
    return text.casefold()


def contains(haystack: str, needle: str) -> bool:
    return normalize(needle) in normalize(haystack)
