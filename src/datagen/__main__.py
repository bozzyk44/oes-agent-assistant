"""Генератор набора: `make data`.

Без аргументов пишет набор с постоянным зерном в `data/` и проверяет записанное.
`--check <каталог>` проверяет формат и состав готового набора, ничего не записывая.
"""

import argparse
import sys
from pathlib import Path

from datagen.check import check_dataset, check_traps
from datagen.render import write_dataset
from datagen.world import DEFAULT_SEED, HOLDOUT_SEED, build_world


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datagen", description="Генератор набора данных")
    parser.add_argument("--seed", type=int, help="зерно набора")
    parser.add_argument(
        "--holdout", action="store_true", help="набор для итоговой проверки: своё зерно"
    )
    parser.add_argument("--out", type=Path, default=Path("data"), help="каталог набора")
    parser.add_argument("--check", type=Path, metavar="DIR", help="только проверить готовый набор")
    args = parser.parse_args(argv)
    if args.seed is None:
        args.seed = HOLDOUT_SEED if args.holdout else DEFAULT_SEED

    if args.check is not None:
        root: Path = args.check
        problems = check_dataset(root)
    else:
        root = args.out
        world = build_world(args.seed)
        write_dataset(world, root)
        # Ловушки сверяются только на наборе верного формата: иначе их нечем читать.
        problems = check_dataset(root) or check_traps(root, world)

    if problems:
        print(f"набор в {root} не прошёл проверку:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if args.check is None:
        print(f"набор записан в {root}, зерно {args.seed}; формат и состав проверены")
    else:
        print(f"набор в {root}: формат и состав в порядке")
    return 0


if __name__ == "__main__":
    sys.exit(main())
