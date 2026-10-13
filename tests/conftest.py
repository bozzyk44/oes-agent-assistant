from pathlib import Path

import pytest

from datagen.render import write_dataset
from datagen.world import DEFAULT_SEED, SECOND_SEED, World, build_world


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
