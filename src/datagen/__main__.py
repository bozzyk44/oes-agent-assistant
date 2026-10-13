"""Генератор набора: `make data`. Каркас — сам генератор появляется отдельной задачей."""

import sys


def main() -> int:
    print("генератор набора ещё не реализован: набор в data/ не записан", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
