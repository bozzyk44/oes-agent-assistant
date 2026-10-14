"""Сравнение двух прогонов: попарно по вопросам.

Честное сравнение меняет один фактор: конфигурации обязаны отличаться ровно в одном поле —
это проверяет команда, а не глаз. Коммит фактором не считается: правка промпта или
описаний инструментов — новый коммит и новый `prefix_hash`, это одна правка. Цены тоже
не фактор: у каждой модели свои, на ответы они не влияют. Победитель по разнице внутри
шума не объявляется: отчёт перечисляет вопросы, на которых прогоны расходятся.
"""

from pathlib import Path
from typing import Any

from evalrun.report import STABLE, base_id, read, statuses

FIELDS = (
    "model",
    "model_base_url",
    "prefix_hash",
    "dataset_hash",
    "questions_hash",
    "sampling",
    "attempts",
)
# Показываются рядом, но фактором не считаются.
CONTEXT = ("commit", "prices")


class NotComparable(ValueError):
    pass


def differing(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    return [name for name in FIELDS if first.get(name) != second.get(name)]


def compare(first_path: Path, second_path: Path) -> str:
    first_config, first = read(first_path)
    second_config, second = read(second_path)
    fields = differing(first_config, second_config)
    code_only = not fields and first_config.get("commit") != second_config.get("commit")
    if len(fields) != 1 and not code_only:
        named = ", ".join(fields) if fields else "ни в одном"
        raise NotComparable(
            f"конфигурации должны отличаться ровно в одном поле, отличаются: {named}"
        )
    field = fields[0] if fields else "commit"
    a, b = statuses(first), statuses(second)
    leaks_a = {base_id(line) for line in first if line.get("leak")}
    leaks_b = {base_id(line) for line in second if line.get("leak")}
    only_a = [q for q in a if a[q] == STABLE and b.get(q) != STABLE]
    only_b = [q for q in b if b[q] == STABLE and a.get(q) != STABLE]
    lines = [
        "# Сравнение прогонов",
        "",
        f"Отличается поле `{field}`: А — `{first_config.get(field)}`,"
        f" Б — `{second_config.get(field)}`.",
        "",
        "| | А | Б |",
        "|---|---|---|",
        f"| Запуск | {first_config.get('run_id')} | {second_config.get('run_id')} |",
        *(f"| {name} | {first_config.get(name)} | {second_config.get(name)} |" for name in CONTEXT),
        f"| Проходят стабильно | {sum(v == STABLE for v in a.values())} из {len(a)}"
        f" | {sum(v == STABLE for v in b.values())} из {len(b)} |",
        f"| Вопросов с утечкой | {len(leaks_a)} | {len(leaks_b)} |",
        "",
        f"Стабильно проходят только в А: {', '.join(only_a) or 'нет'}.",
        f"Стабильно проходят только в Б: {', '.join(only_b) or 'нет'}.",
        "",
        "| Вопрос | А | Б |",
        "|---|---|---|",
    ]
    for qid in sorted(set(a) | set(b)):
        if a.get(qid) != b.get(qid):
            lines.append(f"| {qid} | {a.get(qid, '—')} | {b.get(qid, '—')} |")
    return "\n".join(lines) + "\n"
