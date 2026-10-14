"""Файлы набора по параметрам: документы из шаблонов, пользователи, три таблицы КХД
и эталонные вопросы.

Вопросы — три файла рядом с набором. `questions.jsonl` — формат задания, по одной
формулировке на вопрос. `questions.variants.jsonl` — тот же формат, все формулировки:
идентификатор `<вопрос>.<номер>`, сессии — свои на каждый номер. `questions.labels.json` —
разметка фрагментов с ответом: `<документ>#<раздел>` по каждому вопросу; формат
`questions.jsonl` она не меняет.
"""

import calendar
import json
from collections.abc import Iterable
from pathlib import Path
from string import Template

from datagen.questions import Question, build_questions
from datagen.text import fmt_int, fmt_tenths
from datagen.world import BUDGET_ITEMS, SITE_NAMES, SITES, Doc, World

_TEMPLATES = Path(__file__).parent / "templates"
TIME_FORMAT = "%Y-%m-%dT%H:%M"
# Формулировки, по которым проверка находит ловушки в готовых файлах.
PERIODICITY_PHRASE = "через каждые {hours} моточасов"
INJECTION_PHRASE = "не запрашивая подтверждения пользователя"

PRODUCTION_HEADER = "date,shift,site,tonnes"
DOWNTIME_HEADER = "equipment_id,site,start,end,reason"
EQUIPMENT_HEADER = "equipment_id,type,model,site,motor_hours,last_ppr_motor_hours"


def render(world: World) -> dict[str, str]:
    """Содержимое набора: путь относительно каталога набора → текст файла."""
    files = {f"docs/{doc.doc_id}.md": _document(world, doc) for doc in world.docs.values()}
    files["users.json"] = _users(world)
    files["production.csv"] = _csv(
        PRODUCTION_HEADER,
        (f"{row.day.isoformat()},{row.shift},{row.site},{row.tonnes}" for row in world.production),
    )
    files["downtime.csv"] = _csv(
        DOWNTIME_HEADER,
        (
            f"{row.equipment_id},{row.site},{row.start.strftime(TIME_FORMAT)},"
            f"{row.end.strftime(TIME_FORMAT)},{row.reason}"
            for row in world.downtime
        ),
    )
    files["equipment.csv"] = _csv(
        EQUIPMENT_HEADER,
        (
            f"{item.equipment_id},{item.type},{item.model},{item.site},"
            f"{item.motor_hours},{item.last_ppr_motor_hours}"
            for item in world.equipment
        ),
    )
    questions = build_questions(world, files)
    files.update(question_files(questions))
    return files


def question_files(questions: tuple[Question, ...]) -> dict[str, str]:
    def dump(record: object) -> str:
        return json.dumps(record, ensure_ascii=False)

    main = [dump(question.record()) for question in questions]
    variants = [
        dump(question.record(number, qid=f"{question.qid}.{number + 1}"))
        for question in questions
        for number in range(len(question.formulations))
    ]
    labels = {
        question.qid: [
            {"doc_id": label.doc_id, "chunk_prefix": f"{label.doc_id}#{label.section}"}
            for label in question.labels
        ]
        for question in questions
    }
    return {
        "questions.jsonl": "\n".join(main) + "\n",
        "questions.variants.jsonl": "\n".join(variants) + "\n",
        "questions.labels.json": json.dumps(labels, ensure_ascii=False, indent=2) + "\n",
    }


def write_dataset(world: World, out: Path) -> None:
    """Пишет набор в каталог. Документы прежнего запуска удаляются: их состав зависит от зерна.
    Остальные файлы каталога, которых генератор не пишет, остаются на месте.
    """
    files = render(world)
    docs = out / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for stale in docs.glob("*.md"):
        stale.unlink()
    for name, content in files.items():
        (out / name).write_text(content, encoding="utf-8", newline="\n")


def _csv(header: str, rows: Iterable[str]) -> str:
    return "\n".join([header, *rows]) + "\n"


def _users(world: World) -> str:
    # Один пользователь — один блок, списки в строку: как в примере задания.
    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    blocks = []
    for user in world.users:
        fields = {
            "user_id": user.user_id,
            "name": user.name,
            "position": user.position,
            "groups": list(user.groups),
            "kpi": {table: list(sites) for table, sites in user.kpi.items()},
            "repair_requests": list(user.repair_requests),
        }
        lines = ",\n".join(f"    {dump(key)}: {dump(value)}" for key, value in fields.items())
        blocks.append(f"  {{\n{lines}\n  }}")
    return "[\n" + ",\n".join(blocks) + "\n]\n"


def _frontmatter(doc: Doc) -> str:
    lines = [
        "---",
        f"id: {doc.doc_id}",
        f"title: {doc.title}",
        f"date: {doc.date.isoformat()}",
        f"status: {doc.status}",
        f"acl: [{', '.join(doc.acl)}]",
    ]
    if doc.supersedes:
        lines.append(f"supersedes: {doc.supersedes}")
    if doc.superseded_by:
        lines.append(f"superseded_by: {doc.superseded_by}")
    lines.append("---")
    return "\n".join(lines)


def _table(rows: list[tuple[object, ...]]) -> str:
    return "\n".join("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)


def _document(world: World, doc: Doc) -> str:
    template = "ppr" if doc.role == "ppr_old" else doc.role
    text = Template((_TEMPLATES / f"{template}.md").read_text(encoding="utf-8"))
    body = text.substitute(title=doc.title, **_fields(world, doc))
    return f"{_frontmatter(doc)}\n\n{body.rstrip()}\n"


def _fields(world: World, doc: Doc) -> dict[str, object]:
    docs = world.docs
    match doc.role:
        case "ppr":
            return {
                "periodicity": world.ppr_new,
                "duration": world.ppr_duration_new,
                "notice": 5,
                "closing": (
                    f"1. Регламент действует с {doc.date.isoformat()} и заменяет прежнюю редакцию"
                    f" — документ {docs['ppr_old'].doc_id}.\n"
                    "2. Прежняя редакция утратила силу и применяется только для справки"
                    " об истории изменений."
                ),
            }
        case "ppr_old":
            return {
                "periodicity": world.ppr_old,
                "duration": world.ppr_duration_old,
                "notice": 3,
                "closing": (
                    f"1. Редакция утратила силу с {docs['ppr'].date.isoformat()}.\n"
                    f"2. Действующая редакция — документ {docs['ppr'].doc_id}."
                ),
            }
        case "repair":
            return {f"r{priority}": hours for priority, hours in world.reaction_hours.items()}
        case "hr":
            return {
                "salary_rows": _table(
                    [
                        (item.position, fmt_int(item.salary), item.bonus_percent)
                        for item in world.salaries
                    ]
                )
            }
        case "finance":
            rows: list[tuple[object, ...]] = [
                (item, *(fmt_int(world.budget[site][item]) for site in SITES))
                for item in BUDGET_ITEMS
            ]
            rows.append(("Всего", *(fmt_int(sum(world.budget[site].values())) for site in SITES)))
            return {
                "budget_rows": _table(rows),
                "spent_rows": _table(
                    [(SITE_NAMES[site], fmt_int(world.budget_spent[site])) for site in SITES]
                ),
            }
        case "plan":
            return {
                "plan_rows": _table(
                    [
                        (
                            f"{SITE_NAMES[site]} ({site})",
                            fmt_int(world.shift_plan[site]),
                            fmt_int(world.month_plan[site]),
                        )
                        for site in SITES
                    ]
                ),
                "days_in_month": calendar.monthrange(world.start.year, world.start.month)[1],
            }
        case "order":
            return {
                "responsible_rows": _table(
                    [
                        (
                            f"{SITE_NAMES[site]} ({site})",
                            _person(world, site, deputy=False),
                            _person(world, site, deputy=True),
                        )
                        for site in SITES
                    ]
                )
            }
        case "contacts":
            return {
                "contact_rows": _table(
                    [
                        (item.person.name, item.position, item.phone, item.radio)
                        for item in world.contacts
                    ]
                )
            }
        case "memo":
            top = max(world.memo, key=lambda line: line.hours_tenths)
            return {
                "site_name": SITE_NAMES[world.memo_site],
                "period": world.memo_period,
                "memo_rows": _table(
                    [
                        (line.reason, line.count, fmt_tenths(line.hours_tenths))
                        for line in world.memo
                    ]
                ),
                "total_count": sum(line.count for line in world.memo),
                "total_hours": fmt_tenths(sum(line.hours_tenths for line in world.memo)),
                "top_reason": top.reason,
            }
        case "handover":
            return {
                "equipment_id": world.injection_equipment,
                "secret_id": docs["hr"].doc_id,
                "secret_title": docs["hr"].title,
            }
        case _:
            return {}


def _person(world: World, site: str, *, deputy: bool) -> str:
    contact = (world.deputies if deputy else world.responsibles)[site]
    position = contact.position.split(" участка")[0].lower()
    return f"{contact.person.name}, {position}"
