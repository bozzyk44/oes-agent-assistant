import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from assistant.db.grants import grant_service
from assistant.db.migrate import upgrade
from assistant.ingest.bootstrap import Accounts, ensure_roles_and_databases, read_accounts
from assistant.ingest.dataset import read_dataset
from assistant.ingest.load import LOADER_VERSION, load_dataset
from assistant.ingest.startup import connect_admin
from datagen.render import write_dataset
from datagen.world import DEFAULT_SEED, SECOND_SEED, World, build_world
from tests.smallset import write_small_dataset


@pytest.fixture(scope="session", params=[DEFAULT_SEED, SECOND_SEED], ids=["data", "second"])
def dataset(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, World]:
    """Набор на диске и его параметры. Тест с этой фикстурой идёт дважды: на наборе
    с зерном `data/` и на втором, с другими числами, идентификаторами и расстановкой ловушек.
    Каталог общий на запуск: тест, которому нужно его менять, берёт копию.
    """
    seed: int = request.param
    root = tmp_path_factory.mktemp(f"dataset-{seed}")
    world = build_world(seed)
    write_dataset(world, root)
    return root, world


# Пароли ролей — метки для тестов: база живёт в памяти до конца запуска.
OWNER_PASSWORD = "test-owner-password-marker-91ab"
SERVICE_PASSWORD = "test-service-password-marker-3e7d"
TOIR_PASSWORD = "test-toir-password-marker-c240"


@dataclass(frozen=True)
class Database:
    """Сервер тестов с ролями, базами и схемой — как после загрузчика, но без набора."""

    host: str
    port: int
    admin_password: str
    accounts: Accounts
    owner_url: str
    service_url: str
    toir_url: str
    owner: Engine
    # Одно соединение в пуле: тесты проверяют, что остаётся на соединении между транзакциями.
    service: Engine

    def url(self, role: str, password: str, database: str) -> str:
        return f"postgresql+psycopg://{role}:{password}@{self.host}:{self.port}/{database}"

    def load(self, root: Path, *, fresh: bool = False) -> bool:
        """Загружает набор. `fresh` — как на новой базе: сессии и действия прежних тестов
        очищаются, даже если набор тот же.
        """
        dataset = read_dataset(root, LOADER_VERSION)
        with self.owner.begin() as connection:
            if fresh:
                connection.execute(text("delete from app.dataset_version"))
            return load_dataset(connection, dataset)


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    host = os.environ["POSTGRES_HOST"]
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    admin_password = os.environ["POSTGRES_PASSWORD"]

    def url(role: str, password: str, name: str) -> str:
        return f"postgresql+psycopg://{role}:{password}@{host}:{port}/{name}"

    owner_url = url("test_owner", OWNER_PASSWORD, "assistant_test")
    service_url = url("test_app", SERVICE_PASSWORD, "assistant_test")
    toir_url = url("test_toir", TOIR_PASSWORD, "toir_test")
    accounts = read_accounts(owner_url, service_url, toir_url)
    with connect_admin(host, port, admin_password) as admin:
        ensure_roles_and_databases(admin, accounts)
    owner = create_engine(owner_url)
    with owner.begin() as connection:
        upgrade(connection)
        grant_service(connection, accounts.service.role)
    service = create_engine(service_url, pool_size=1, max_overflow=0)
    yield Database(
        host, port, admin_password, accounts, owner_url, service_url, toir_url, owner, service
    )
    service.dispose()
    owner.dispose()


@pytest.fixture
def small(database: Database, tmp_path: Path) -> Path:
    """Маленький набор в базе. Если он там уже лежит, загрузка ничего не меняет:
    сессии и действия прежних тестов остаются, поэтому тесты берут свои идентификаторы.
    """
    root = write_small_dataset(tmp_path / "small")
    database.load(root)
    return root
