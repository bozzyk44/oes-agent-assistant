"""Загрузка набора в базу и загрузчик целиком."""

from pathlib import Path

import pytest
from sqlalchemy import text

from assistant.db.binding import bind_user
from assistant.ingest.__main__ import bootstrap, load, main, migrate
from assistant.ingest.dataset import DatasetError
from assistant.ingest.startup import StartupError
from datagen.world import World
from tests.conftest import Database
from tests.smallset import write_small_dataset

pytestmark = pytest.mark.db

COUNTS = (
    "app.users",
    "app.kpi_grants",
    "dwh.production",
    "dwh.downtime",
    "dwh.equipment",
    "kb.documents",
    "kb.chunks",
)


def _counts(database: Database) -> dict[str, int]:
    with database.owner.connect() as connection:
        return {
            table: int(connection.scalar(text(f"select count(*) from {table}")) or 0)  # noqa: S608
            for table in COUNTS
        }


def _state(database: Database) -> tuple[int, int]:
    with database.owner.connect() as connection:
        sessions = connection.scalar(text("select count(*) from app.sessions"))
        actions = connection.scalar(text("select count(*) from app.actions"))
    return int(sessions or 0), int(actions or 0)


def _leave_state(database: Database, user: str) -> None:
    """Сессия, ход и действие в ожидании подтверждения — под ролью сервиса."""
    with database.service.begin() as connection:
        bind_user(connection, user)
        connection.execute(
            text("insert into app.sessions (user_id, session_id) values (:u, 's-load')"),
            {"u": user},
        )
        connection.execute(
            text(
                "insert into app.turns (turn_id, user_id, session_id, question, outcome, answer,"
                " model_calls, input_tokens, output_tokens, max_call_input_tokens, prefix_hash,"
                " latency_ms)"
                " values ('t-load', :u, 's-load', 'в', 'action', 'о', 1, 1, 1, 1, 'h', 1)"
            ),
            {"u": user},
        )
        connection.execute(
            text(
                "insert into app.actions (action_id, user_id, session_id, turn_id, tool, params)"
                " values ('a-load', :u, 's-load', 't-load', 'create_repair_request', '{}')"
            ),
            {"u": user},
        )


def _env(database: Database, root: Path) -> dict[str, str]:
    return {
        "POSTGRES_HOST": database.host,
        "POSTGRES_PORT": str(database.port),
        "POSTGRES_PASSWORD": database.admin_password,
        "DATABASE_OWNER_URL": database.owner_url,
        "DATABASE_URL": database.service_url,
        "TOIR_DATABASE_URL": database.toir_url,
        "DATA_DIR": str(root),
    }


def test_same_dataset_changes_nothing_and_keeps_state(database: Database, tmp_path: Path) -> None:
    root = write_small_dataset(tmp_path)
    database.load(root, fresh=True)
    before = _counts(database)
    assert before == {
        "app.users": 4,
        "app.kpi_grants": 11,
        "dwh.production": 5,
        "dwh.downtime": 3,
        "dwh.equipment": 3,
        "kb.documents": 4,
        "kb.chunks": 6,
    }
    _leave_state(database, "u-eng")
    assert database.load(root) is False
    assert _counts(database) == before
    # Перезапуск с тем же набором сохраняет сессии и действия в ожидании подтверждения.
    assert _state(database) == (1, 1)


def test_other_dataset_replaces_data_and_clears_state(
    database: Database, dataset: tuple[Path, World], tmp_path: Path
) -> None:
    database.load(write_small_dataset(tmp_path), fresh=True)
    _leave_state(database, "u-eng")
    root, world = dataset
    assert database.load(root) is True
    counts = _counts(database)
    # Подмена набора не оставляет старых данных.
    assert counts["app.users"] == len(world.users)
    assert counts["dwh.production"] == len(world.production)
    assert counts["dwh.downtime"] == len(world.downtime)
    assert counts["dwh.equipment"] == len(world.equipment)
    assert counts["kb.documents"] == len(world.docs)
    with database.owner.connect() as connection:
        assert (
            connection.scalar(text("select count(*) from app.users where user_id = 'u-eng'")) == 0
        )
        assert connection.scalar(text("select count(*) from kb.chunks where doc_id = 'H1'")) == 0
        for table in ("sessions", "turns", "messages", "actions"):
            assert connection.scalar(text(f"select count(*) from app.{table}")) == 0  # noqa: S608


def test_failure_in_the_middle_keeps_previous_dataset(database: Database, tmp_path: Path) -> None:
    good = write_small_dataset(tmp_path / "good")
    database.load(good, fresh=True)
    _leave_state(database, "u-eng")
    before = _counts(database)

    # Ошибка формата: набор не читается, база не тронута.
    bad = write_small_dataset(tmp_path / "bad")
    (bad / "equipment.csv").write_text("не тот заголовок\n", encoding="utf-8")
    with pytest.raises(DatasetError):
        database.load(bad)

    # Ошибка уже внутри транзакции загрузки: прежний набор остаётся целиком.
    broken = write_small_dataset(tmp_path / "broken")
    # Нулевой байт проходит проверку формата, но в текст базы не записывается.
    document = (broken / "docs" / "R2.md").read_text(encoding="utf-8")
    (broken / "docs" / "R2.md").write_text(document + "\x00", encoding="utf-8")
    with pytest.raises(Exception, match="NUL"):
        database.load(broken)

    assert _counts(database) == before
    assert _state(database) == (1, 1)


def test_index_is_stemmed_and_folds_yo(database: Database, small: Path) -> None:
    def found(query: str) -> set[str]:
        with database.owner.connect() as connection:
            return set(
                connection.scalars(
                    text(
                        "select chunk_id from kb.chunks where tsv @@ plainto_tsquery('russian', :q)"
                    ),
                    {"q": query},
                ).all()
            )

    # Словоформы находят друг друга: в тексте «экскаватора», «оклад», «мастера».
    assert found("экскаваторы") == {"A1#1"}
    assert found("оклады мастеров") == {"H1#1"}
    # «ё» приведена к «е» при индексации.
    assert found("емкость") == {"A1#1"}
    # Название документа и заголовок раздела входят в индекс каждого фрагмента.
    assert found("памятка") == {"A1#0", "A1#1", "A1#2"}
    assert found("периодичность") == {"R1#1", "R2#1"}
    with database.owner.connect() as connection:
        terms = connection.scalar(text("select terms from kb.chunks where chunk_id = 'A1#1'"))
        row = connection.execute(
            text(
                "select status, supersedes, superseded_by, acl from kb.chunks"
                " where chunk_id = 'R1#1'"
            )
        ).one()
    # Идентификатор с дефисом и числом хранится целиком, без стемминга.
    assert "ex-1" in terms
    assert row == ("superseded", None, "R2", ["engineering", "site-k1"])


def test_loader_end_to_end_is_idempotent(database: Database, tmp_path: Path) -> None:
    root = write_small_dataset(tmp_path)
    (root / "production.csv").write_text(
        (root / "production.csv").read_text(encoding="utf-8") + "2026-01-03,1,K1,123\n",
        encoding="utf-8",
    )
    (root / "docs" / "README.md").write_text("без frontmatter\n", encoding="utf-8")
    env = _env(database, root)
    # Три шага по порядку, как в compose: роли и базы, миграции, загрузка.
    assert bootstrap(env).startswith("роли и базы на месте")
    assert migrate(env).startswith("схема уже на последней версии")
    message = load(env)
    assert message.startswith("набор загружен: документов 4, пользователей 4, строк КХД 12")
    assert "пропущены файлы без frontmatter: docs/README.md" in message
    _leave_state(database, "u-k1")
    # Повторный запуск всех шагов: роли, миграции и права на месте, набор тот же.
    assert bootstrap(env).startswith("роли и базы на месте")
    assert migrate(env).startswith("схема уже на последней версии")
    assert load(env).startswith("набор не изменился")
    assert _state(database) == (1, 1)
    with database.service.begin() as connection:
        bind_user(connection, "u-k1")
        assert connection.scalar(text("select count(*) from dwh.production")) == 3


def test_loader_stops_with_file_and_line(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = write_small_dataset(tmp_path / "good")
    database.load(good)
    before = _counts(database)
    bad = write_small_dataset(tmp_path / "bad")
    (bad / "downtime.csv").write_text(
        (bad / "downtime.csv").read_text(encoding="utf-8").replace("2026-01-01T09:00", "вчера"),
        encoding="utf-8",
    )
    for name, value in _env(database, bad).items():
        monkeypatch.setenv(name, value)
    assert main(["load"]) == 1
    message = capsys.readouterr().err
    assert "load: downtime.csv:3: время" in message
    assert database.admin_password not in message
    assert _counts(database) == before


def test_each_step_gets_only_its_own_secrets(database: Database, tmp_path: Path) -> None:
    # Пароль администратора сервера нужен только шагу ролей и баз.
    env = _env(database, write_small_dataset(tmp_path))
    without_admin = {key: value for key, value in env.items() if key != "POSTGRES_PASSWORD"}
    assert migrate(without_admin).startswith("схема уже на последней версии")
    owner_only = {key: env[key] for key in ("DATABASE_OWNER_URL", "DATA_DIR")}
    assert load(owner_only).startswith("набор")
    with pytest.raises(StartupError, match="POSTGRES_PASSWORD"):
        bootstrap(without_admin)
    with pytest.raises(StartupError, match="DATABASE_URL"):
        migrate(owner_only)


def test_unknown_step_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["everything"])
    assert caught.value.code == 2
    with pytest.raises(SystemExit):
        main([])
