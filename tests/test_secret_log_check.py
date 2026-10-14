"""Самопроверка общей проверки журнала: она видит записи самого теста, а не только
фазы завершения. Без этой проверки её пустота однажды уже осталась незамеченной.
"""

import logging
import os
from pathlib import Path

import pytest

from tests.conftest import leaked_markers

pytest_plugins = ["pytester"]


ROOT = Path(__file__).resolve().parents[1]


def test_leak_in_test_body_fails_the_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Отдельный процесс: записи вложенного прогона не попадают в журнал этого теста.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(ROOT), str(ROOT / "src")]))
    pytester.makeconftest("from tests.conftest import secrets_stay_out_of_log  # noqa: F401\n")
    pytester.makepyfile(
        """
        import logging

        from tests.servicekit import TOIR_TOKEN


        def test_leak():
            logging.getLogger("assistant.probe").warning("leak %s", TOIR_TOKEN)


        def test_clean():
            logging.getLogger("assistant.probe").warning("ничего секретного")
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_leak*"])


def test_markers_are_found_in_messages_and_tracebacks() -> None:
    try:
        raise RuntimeError("в исключении: secret-marker")
    except RuntimeError:
        import sys

        failure = logging.LogRecord("x", logging.ERROR, "f", 1, "сбой", None, sys.exc_info())
    plain = logging.LogRecord("x", logging.INFO, "f", 1, "текст %s", ("secret-marker",), None)
    assert leaked_markers([plain], ("secret-marker",)) == ["secret-marker"]
    assert leaked_markers([failure], ("secret-marker",)) == ["secret-marker"]
    assert leaked_markers([plain], ("другое",)) == []
