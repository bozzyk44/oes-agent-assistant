"""Маленький набор для тестов базы: собран здесь, с содержимым `data/` не связан."""

import json
from pathlib import Path

USERS = [
    {
        "user_id": "u-eng",
        "name": "Инженер Главный",
        "position": "Главный инженер",
        "groups": ["engineering"],
        "kpi": {"production": ["K1", "K2"], "downtime": ["K1", "K2"], "equipment": ["K1", "K2"]},
        "repair_requests": ["K1", "K2"],
    },
    {
        "user_id": "u-k1",
        "name": "Начальник Первый",
        "position": "Начальник участка",
        "groups": ["site-k1"],
        "kpi": {"production": ["K1"], "downtime": ["K1"], "equipment": ["K1"]},
        "repair_requests": ["K1"],
    },
    {
        "user_id": "u-fin",
        "name": "Экономист Один",
        "position": "Экономист",
        "groups": ["finance"],
        "kpi": {"production": ["K1", "K2"]},
        "repair_requests": [],
    },
    {
        "user_id": "u-hr",
        "name": "Кадровик Один",
        "position": "Специалист по персоналу",
        "groups": ["hr"],
        "kpi": {},
        "repair_requests": [],
    },
]

# Документ → группы доступа.
DOCS = {
    "A1": ["all"],
    "H1": ["hr"],
    "R1": ["engineering", "site-k1"],
    # Действующая редакция открыта шире прежней: финансисту прежняя не видна.
    "R2": ["engineering", "site-k1", "finance"],
}

PRODUCTION = [
    "2026-01-01,1,K1,100",
    "2026-01-01,2,K1,110",
    "2026-01-01,1,K2,200",
    "2026-01-01,2,K2,210",
    "2026-01-02,1,K2,220",
]
DOWNTIME = [
    "EX-1,K1,2026-01-01T22:00,2026-01-02T03:30,ремонт",
    "EX-2,K2,2026-01-01T09:00,2026-01-01T10:00,погода",
    "T-1,K1,2026-01-02T09:00,2026-01-02T09:40,нет водителя",
]
EQUIPMENT = [
    "EX-1,экскаватор,ЭКГ-10,K1,5000,4500",
    "EX-2,экскаватор,ЭКГ-12,K2,7000,6900",
    "T-1,самосвал,БелАЗ-7555,K1,3000,2800",
]


def _doc(doc_id: str, title: str, status: str, body: str, extra: str = "") -> str:
    acl = ", ".join(DOCS[doc_id])
    head = f"---\nid: {doc_id}\ntitle: {title}\ndate: 2026-01-10\nstatus: {status}\nacl: [{acl}]\n"
    return f"{head}{extra}---\n\n# {title}\n\n{body}\n"


def write_small_dataset(root: Path) -> Path:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "A1.md").write_text(
        _doc(
            "A1",
            "Памятка для всех",
            "active",
            "Вводный текст памятки.\n\n## 1. Ёмкости\n\nЁмкость экскаватора EX-1 проверяется"
            " ежесменно.\n\n## 2. Связь\n\nДиспетчер отвечает по рации.",
        ),
        encoding="utf-8",
    )
    (docs / "H1.md").write_text(
        _doc(
            "H1",
            "Положение об окладах",
            "active",
            "## 1. Оклады\n\nОклад горного мастера составляет 123450 рублей."
            " Премия горного мастера начисляется ежемесячно.",
        ),
        encoding="utf-8",
    )
    (docs / "R1.md").write_text(
        _doc(
            "R1",
            "Регламент ремонта",
            "superseded",
            "## 1. Периодичность\n\nРемонт через каждые 500 моточасов.",
            "superseded_by: R2\n",
        ),
        encoding="utf-8",
    )
    (docs / "R2.md").write_text(
        _doc(
            "R2",
            "Регламент ремонта",
            "active",
            "## 1. Периодичность\n\nРемонт через каждые 700 моточасов.",
            "supersedes: R1\n",
        ),
        encoding="utf-8",
    )
    (root / "users.json").write_text(json.dumps(USERS, ensure_ascii=False), encoding="utf-8")
    for name, header, rows in (
        ("production.csv", "date,shift,site,tonnes", PRODUCTION),
        ("downtime.csv", "equipment_id,site,start,end,reason", DOWNTIME),
        (
            "equipment.csv",
            "equipment_id,type,model,site,motor_hours,last_ppr_motor_hours",
            EQUIPMENT,
        ),
    ):
        (root / name).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return root
