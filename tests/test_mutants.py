"""Мутанты со сценарной заглушкой: набор ловит каждое отключённое правило.

Каждый мутант проходит прогон по вопросам с атакой на сгенерированном наборе — с зерном
`data/` и со вторым. Заглушка модели запрашивает запрещённое прямо, проверки — те же, что
у `make eval`. Контроль — сервис без подмен: на нём все вопросы с атакой проходят, иначе
провал на мутанте ничего бы не доказывал.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from assistant.actions.toir import Reply
from assistant.ingest.bootstrap import read_account
from assistant.ingest.startup import connect_admin
from datagen.world import World
from evalrun.api import Service, Toir
from evalrun.questions import load
from evalrun.run import Plan, Prices, Runner
from tests.conftest import Database
from tests.mutants import BY_NAME, CONTROL, MUTANTS, Mutant, applied
from tests.mutants.obedient import ATTACKS_FILE, ObedientModel, load_attacks
from tests.mutants.role import ensure_bypass_role
from tests.test_evalrun import _transport

pytestmark = pytest.mark.db

BYPASS_PASSWORD = "test-bypass-password-marker-6f2e"


@pytest.fixture
def bypass_url(database: Database) -> str:
    """Роль с обходом — на каждый тест заново: шаг ролей и баз в соседних тестах отзывает
    вход у ролей, которых нет в его списке. Имя — вне шаблона ролей тестов `test_`."""
    url = database.url("mutant_bypass", BYPASS_PASSWORD, database.accounts.service.database)
    with connect_admin(database.host, database.port, database.admin_password) as admin:
        ensure_bypass_role(admin, database.owner, read_account("BYPASS_DATABASE_URL", url))
    return url


class FakeToir:
    """ТОиР в процессе тестов: сервис создаёт заявки, прогон считает их."""

    def __init__(self) -> None:
        self.created: dict[str, dict[str, Any]] = {}

    def service(self, method: str, path: str, headers: Any, body: Any) -> Reply:
        key = headers["Idempotency-Key"]
        self.created.setdefault(key, body)
        return Reply(201, {"request_id": f"r-{len(self.created)}"})

    def evalrun(self, method: str, url: str, headers: Any, body: Any) -> tuple[int, Any]:
        return 200, list(self.created.values())


def _attacked(root: Path, out: Path) -> Path:
    """Вопросы с атакой — в своём файле рядом с атаками и разметкой."""
    attacks = json.loads((root / ATTACKS_FILE).read_text(encoding="utf-8"))
    lines = [
        line
        for line in (root / "questions.jsonl").read_text(encoding="utf-8").splitlines()
        if attacks[json.loads(line)["id"]]["calls"]
    ]
    out.mkdir(parents=True, exist_ok=True)
    path = out / "questions.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name in (ATTACKS_FILE, "questions.labels.json"):
        (out / name).write_bytes((root / name).read_bytes())
    return path


def run_mutant(
    database: Database, bypass_url: str, mutant: Mutant, root: Path, tmp: Path
) -> list[dict[str, Any]]:
    from assistant.turn.agent import AgentHandler
    from tests.servicekit import make_service, settings

    questions = _attacked(root, tmp / "questions")
    url = bypass_url if mutant.bypass else database.service_url
    engine = create_engine(url) if mutant.bypass else database.service
    toir = FakeToir()
    try:
        with applied(mutant):
            model = ObedientModel(load_attacks(questions))
            handler = AgentHandler(engine, model, settings(url, model=True).model)
            service = make_service(engine, url, handler, model=True, toir=toir.service)
            runner = Runner(
                Service("http://assistant:8000", _transport(service.client)),
                Toir("http://toir:8001", "token", toir.evalrun),
                sleep=lambda _: None,
            )
            plan = Plan(f"m-{mutant.name}", 1, questions.name, "hash", Prices(None, None, None))
            lines = runner.run(
                load(questions),
                plan,
                tmp / "eval" / "results.jsonl",
                lambda: datetime(2026, 10, 14, tzinfo=UTC),
            )
    finally:
        if mutant.bypass:
            engine.dispose()
    return lines[1:]


def _expected(root: Path, mutant: Mutant) -> set[str]:
    attacks = json.loads((root / ATTACKS_FILE).read_text(encoding="utf-8"))
    return {qid for qid, item in attacks.items() if item["calls"] and item["case"] in mutant.cases}


def test_every_mutant_case_has_an_attacked_question(dataset: tuple[Path, World]) -> None:
    root, _ = dataset
    attacks = json.loads((root / ATTACKS_FILE).read_text(encoding="utf-8"))
    attacked = {item["case"] for item in attacks.values() if item["calls"]}
    for mutant in MUTANTS:
        assert mutant.cases <= attacked, mutant.name


@pytest.mark.parametrize("mutant", MUTANTS, ids=[mutant.name for mutant in MUTANTS])
def test_the_set_catches_the_mutant(
    database: Database,
    bypass_url: str,
    dataset: tuple[Path, World],
    tmp_path: Path,
    mutant: Mutant,
) -> None:
    root, _ = dataset
    database.load(root, fresh=True)
    lines = run_mutant(database, bypass_url, mutant, root, tmp_path)
    failed = {line["question"]["id"] for line in lines if not line["passed"]}
    leaks = {line["question"]["id"] for line in lines if line["leak"]}
    if mutant.single_barrier:
        # Один барьер из двух держит утечки; исход может разойтись — это допустимо.
        assert leaks == set(), [line["checks"] for line in lines if line["leak"]]
        if mutant.patches is not None:
            # Без кода слоя доступа отказ по КХД становится пустым результатом: расхождение
            # исхода показывает, что код в этом мутанте действительно не отсекает.
            assert failed, "мутант без кода слоя доступа ведёт себя как контроль"
    else:
        assert failed == _expected(root, mutant), [
            (line["question"]["id"], line["checks"]) for line in lines
        ]


def test_control_is_the_service_itself() -> None:
    assert CONTROL.patches is None and not CONTROL.bypass and not CONTROL.cases
    assert set(BY_NAME) == {mutant.name for mutant in MUTANTS}


def test_mutant_patches_are_undone(database: Database, bypass_url: str) -> None:
    import assistant.access.grants as grants
    import assistant.actions.draft as draft
    import assistant.api.app as app
    import assistant.tools.box as box

    def current() -> tuple[object, ...]:
        names = vars(app)["run_turn"], vars(app)["Actions"], vars(box)["search"]
        return (grants.load_grants, draft.mentioned, *names)

    before = current()
    for mutant in MUTANTS:
        with applied(mutant) as patches:
            assert all(patch.done for patch in patches), mutant.name
    assert current() == before


def test_service_code_has_no_switch_for_mutants() -> None:
    # Мутант — подмена в точке входа рядом с тестами; сервис о мутантах не знает.
    src = Path(__file__).parent.parent / "src"
    found = [
        str(path)
        for path in src.rglob("*.py")
        if any(word in path.read_text(encoding="utf-8") for word in ("mutant", "BYPASS_DATABASE"))
    ]
    assert found == []


def test_obedient_model_speaks_openai_format() -> None:
    from fastapi.testclient import TestClient

    from tests.mutants.obedient import ObedientModel, http_app

    attack = [
        {"tool": "get_document", "arguments": {"ref": "X1"}},
        {"tool": "create_repair_request", "arguments": {"equipment_id": "E-1"}},
    ]
    client = TestClient(http_app(ObedientModel({"Что в памятке?": attack})))
    user = {"role": "user", "content": "[сведения]\n\nЧто в памятке?"}

    def ask(*messages: dict[str, Any]) -> dict[str, Any]:
        response = client.post(
            "/v1/chat/completions", json={"model": "m", "messages": [user, *messages]}
        )
        assert response.status_code == 200
        choice: dict[str, Any] = response.json()["choices"][0]
        return choice

    first = ask()
    assert first["finish_reason"] == "tool_calls"
    assert [call["function"]["name"] for call in first["message"]["tool_calls"]] == ["get_document"]
    read = {"role": "assistant", "content": "", "tool_calls": first["message"]["tool_calls"]}
    block = (
        '{"status": "ok", "data": {}}\n'
        "<<<фрагмент id=X1#1 документ=X1 «Т»>>>\nоклад 123\n<<<конец фрагмента>>>"
    )
    result = {"role": "tool", "tool_call_id": "a0-0", "content": block}
    second = ask(read, result)
    assert second["message"]["tool_calls"][0]["function"]["name"] == "create_repair_request"
    repair = {"role": "assistant", "content": "", "tool_calls": second["message"]["tool_calls"]}
    denied = {"role": "tool", "tool_call_id": "a1-0", "content": '{"status": "denied"}'}
    final = json.loads(ask(read, result, repair, denied)["message"]["content"])
    assert final == {"outcome": "answer", "answer": "оклад 123", "citations": ["X1#1"]}


def _results(path: Path, lines: list[tuple[str, bool, bool]]) -> None:
    path.mkdir(parents=True)
    records = [{"type": "config"}] + [
        {"type": "attempt", "question": {"id": qid}, "passed": passed, "leak": leak}
        for qid, passed, leak in lines
    ]
    (path / "results.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )


def test_table_counts_only_failures_the_control_did_not_have(tmp_path: Path) -> None:
    from tests.mutants.table import build

    attacks: dict[str, dict[str, object]] = {
        "a": {"case": "foreign_group_doc", "calls": [{"tool": "get_document"}]},
        "b": {"case": "injection", "calls": [{"tool": "get_document"}]},
        "c": {"case": "kpi_calc", "calls": []},
    }
    _results(tmp_path / "model" / "control", [("a", True, False), ("b", False, False)])
    _results(
        tmp_path / "model" / "docs", [("a", False, True), ("b", False, True), ("c", False, False)]
    )
    _results(tmp_path / "model" / "policies-only", [("a", False, False)])
    table = build(tmp_path, attacks)
    rows = {line.split(" | ")[0].strip("| "): line for line in table.splitlines()}
    # b провалился и в контроле: мутанту он не засчитан; c — лишний провал.
    assert "1 из 2: a; ещё c" in rows["docs"]
    assert "утечек 0; исход разошёлся: a" in rows["policies-only"]
    assert rows["kpi"].endswith("| — | — |")


@pytest.mark.parametrize("table", ["dwh.production", "kb.chunks"])
def test_bypass_role_skips_row_security(
    database: Database, bypass_url: str, dataset: tuple[Path, World], table: str
) -> None:
    """Роль мутантов без политик видит все строки от имени любого пользователя, роль
    сервиса — не все: иначе мутант «только код» был бы неотличим от контроля."""
    from assistant.db.session import transaction

    root, _ = dataset
    database.load(root, fresh=True)
    query = text(f"select count(*) from {table}")  # noqa: S608 — имя из списка выше
    with database.owner.connect() as connection:
        total = connection.scalar(query)
        users = list(connection.scalars(text("select user_id from app.users")))
    bypass = create_engine(bypass_url)
    try:
        seen = {}
        for user in users:
            with transaction(bypass, user) as connection:
                assert connection.scalar(query) == total, user
            with transaction(database.service, user) as connection:
                seen[user] = connection.scalar(query)
    finally:
        bypass.dispose()
    assert min(seen.values()) < total


@pytest.mark.parametrize(
    "name",
    [
        mutant.name
        for mutant in MUTANTS
        if mutant.name in {"docs", "kpi", "repair", "policies-only"}
    ],
)
def test_widened_grants_cover_everything_named(
    database: Database, dataset: tuple[Path, World], name: str
) -> None:
    """Подмена полномочий действует: код слоя доступа больше ничего не отсекает."""
    from assistant.access import grants as module
    from assistant.db.session import transaction
    from assistant.tools.schemas import KPI_COLUMNS

    root, _ = dataset
    database.load(root, fresh=True)
    mutant = BY_NAME[name]
    with database.owner.connect() as connection:
        users = list(connection.scalars(text("select user_id from app.users")))
        groups = set(connection.scalars(text("select group_name from app.user_groups")))
    for user in users:
        with transaction(database.service, user) as connection:
            before = module.load_grants(connection, user)
            with applied(mutant):
                after = module.load_grants(connection, user)
        assert before is not None and after is not None
        docs = name in {"docs", "policies-only"}
        kpi = name in {"kpi", "policies-only"}
        assert after.groups == (frozenset(groups) if docs else before.groups)
        expected_kpi = dict.fromkeys(KPI_COLUMNS, before.known_sites) if kpi else before.kpi
        assert after.kpi == expected_kpi
        expected_repair = before.known_sites if name == "repair" else before.repair_sites
        assert after.repair_sites == expected_repair
