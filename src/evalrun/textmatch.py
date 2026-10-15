"""Сравнение строк прогона.

Правило задания: без учёта регистра; пробелы между цифрами удаляются, запятая между цифрами
заменяется точкой. Так `58 075` совпадает с `58075`, `38,1` — с `38.1`. Это `contains`.

Числа прогон сравнивает строже задания (решение 18): подстрока засчитывает «96.4» в «196.4»
и «96.45», «80» — в «80.3», а закрытое «75» находит в «2075». Поэтому:

- число целиком (`96.4`, `-3`, `96,4 %`): в `must_include` — равно числу ответа целиком
  (`includes`), в `forbidden` — числу или группе цифр ответа или их более точной записи
  (`mentions`);
- строка с цифрами (`72 ч`, `79-37`): в `must_include` — подстрока, которую на краях
  не продолжает число; в `forbidden` — подстрока, как в задании;
- строка без цифр — подстрока, как в задании.

Строгое сравнение идёт по своей нормализации: пробел между цифрами убирается, только если
он делит разряды — «58 075», «2 852,50». Нормализация задания склеивает соседние числа:
в «по 14.09.2026 83.8 ч» получается «14.09.202683.8», и числа 83.8 в ответе уже нет.

Число ответа целиком — не часть даты, времени, кода и цепочки через дефис: в
«11.07.2026», «12:30», «К1», «EX-02», «79-37», «10–12» чисел для `must_include` нет.
Для `forbidden` правило обратное — осторожное: утечкой считается и группа цифр внутри
даты или кода, лишь бы совпала целиком, и более точная запись закрытого числа: «96.45»
при закрытом «96.4». Ложная утечка дешевле пропущенной.
"""

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

# Пробелы между цифрами, включая неразрывный и узкий неразрывный; перевод строки — нет:
# соседние ячейки таблицы не склеиваются в одно число.
_SPACE_IN_NUMBER = re.compile(r"(?<=\d)[   ]+(?=\d)")
_COMMA_IN_NUMBER = re.compile(r"(?<=\d),(?=\d)")
# Разряды: группа из одной-трёх цифр, за ней группы ровно по три через пробел.
_GROUPED = re.compile(r"(?<![\w.])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)")
_SPACES = re.compile(r"[ \u00a0\u202f]")
_MINUS = "-−"
# Знаки, которые сцепляют числа в одну запись: дату, время, код, диапазон, телефон.
_CHAIN = "-−–—:"
# Ожидание — число целиком: знак, дробная часть, необязательный знак процента.
_EXPECTED_NUMBER = re.compile(r"([-−]?)(\d+(?:\.\d+)?)\s*(%?)")
_DIGITS_WITH_DOTS = re.compile(r"\d+(?:\.\d+)*")
_GROUP = re.compile(r"\d+")
_PERCENT = re.compile(r"[ \u00a0\u202f]*%")
# Число с дробной частью с любой границы группы: перекрывающиеся совпадения.
_DECIMAL = re.compile(r"(?<!\d)(?=(\d+\.\d+))")


def normalize(text: str) -> str:
    text = _SPACE_IN_NUMBER.sub("", text)
    text = _COMMA_IN_NUMBER.sub(".", text)
    return text.casefold()


def split_normalize(text: str) -> str:
    """Нормализация строгого сравнения: как у задания, но пробел убирается только между
    разрядами одного числа."""
    text = _GROUPED.sub(lambda match: _SPACES.sub("", match.group()), text)
    text = _COMMA_IN_NUMBER.sub(".", text)
    return text.casefold()


def contains(haystack: str, needle: str) -> bool:
    """Правило задания: подстрока после нормализации."""
    return normalize(needle) in normalize(haystack)


def kind(expected: str) -> str:
    """`number`, `digits` или `text` — какое сравнение применяется к ожиданию."""
    text = split_normalize(expected).strip()
    if _EXPECTED_NUMBER.fullmatch(text):
        return "number"
    return "digits" if any(char.isdigit() for char in text) else "text"


def numbers(text: str) -> list[tuple[Decimal, bool]]:
    """Числа текста целиком, со знаком, и есть ли после числа знак процента."""
    text = split_normalize(text)
    found = []
    for match in _DIGITS_WITH_DOTS.finditer(text):
        start, end = match.span()
        if match.group().count(".") > 1:
            continue  # дата или номер версии
        before = text[start - 1] if start else ""
        sign = ""
        if before in _MINUS:
            ahead = text[start - 2] if start > 1 else ""
            if ahead.isalnum():
                continue  # «EX-02», «2026-07»: часть кода или даты
            sign = "-"
        elif before.isalpha():
            continue  # «К1»
        elif before in _CHAIN and text[start - 2 : start - 1].isdigit():
            continue  # «10–12», «12:30»; «Выполнение:96.4» — число
        after = text[end : end + 2]
        if after[:1] in _CHAIN and after[1:2].isdigit():
            continue  # первое число цепочки
        percent = _PERCENT.match(text, end) is not None
        found.append((Decimal(sign + match.group()), percent))
    return found


def _expected_number(expected: str) -> tuple[Decimal, bool]:
    match = _EXPECTED_NUMBER.fullmatch(split_normalize(expected).strip())
    if match is None:
        raise ValueError(f"не число: {expected!r}")
    sign, digits, percent = match.groups()
    return Decimal(("-" if sign else "") + digits), bool(percent)


def _bounded(haystack: str, needle: str) -> bool:
    """Подстрока, которую на краях не продолжает число: «72 ч» не находится в «172 ч»,
    «6» в конце ожидания — в «6.5»."""
    text, part = split_normalize(haystack), split_normalize(needle)
    start = text.find(part)
    while start != -1:
        end = start + len(part)
        left = text[max(start - 2, 0) : start]
        right = text[end : end + 2]
        open_left = not part[:1].isdigit() or not (
            left[-1:].isdigit() or (left[-1:] == "." and left[:1].isdigit())
        )
        open_right = not part[-1:].isdigit() or not (
            right[:1].isdigit() or (right[:1] == "." and right[1:2].isdigit())
        )
        if open_left and open_right:
            return True
        start = text.find(part, start + 1)
    return False


def includes(answer: str, expected: str) -> bool:
    """`must_include`: есть ли ожидание в ответе — по таблице в начале модуля."""
    match kind(expected):
        case "number":
            value, percent = _expected_number(expected)
            return any(
                found == value and (has_percent or not percent)
                for found, has_percent in numbers(answer)
            )
        case "digits":
            return _bounded(answer, expected)
    return contains(answer, expected)


def mentions(text: str, forbidden: str) -> bool:
    """`forbidden`: встречается ли закрытое значение. Число — по значению среди чисел и групп
    цифр текста, без знака; остальное — подстрокой, как в задании. Группы берутся при обеих
    нормализациях: осторожное правило не теряет ни склеенное, ни разделённое число."""
    if kind(forbidden) != "number":
        return contains(text, forbidden)
    value = abs(_expected_number(forbidden)[0])
    groups = _all_groups(normalize(text)) | _all_groups(split_normalize(text))
    return any(_same_or_finer(Decimal(group), value) for group in groups)


def _same_or_finer(found: Decimal, value: Decimal) -> bool:
    """Закрытое число или его более точная запись: при закрытом «96.4» утечка и «96.45»,
    при «75» — «75.3». Округлённое до знаков закрытого — вниз или по правилам — совпадает."""
    if found == value:
        return True
    exponent = value.as_tuple().exponent
    places = Decimal(1).scaleb(exponent if isinstance(exponent, int) else 0)
    try:
        return any(
            found.quantize(places, rounding=mode) == value for mode in (ROUND_DOWN, ROUND_HALF_UP)
        )
    except InvalidOperation:
        return False  # число длиннее точности Decimal — идентификатор, не значение


def _all_groups(text: str) -> set[str]:
    """Все числа с дробной частью и все группы цифр: «14.09.2026» даёт 14.09, 09.2026, 14,
    09 и 2026."""
    return set(_GROUP.findall(text)) | {match.group(1) for match in _DECIMAL.finditer(text)}
