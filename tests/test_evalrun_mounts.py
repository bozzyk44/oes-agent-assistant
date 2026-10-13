from pathlib import Path

from evalrun.mounts import check_mounts


def test_all_present(tmp_path: Path) -> None:
    questions = tmp_path / "q" / "questions.jsonl"
    questions.parent.mkdir()
    questions.write_text("", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    out_dir = tmp_path / "eval"
    out_dir.mkdir()
    assert check_mounts(questions, data_dir, out_dir) == []


def test_each_missing_part_is_reported(tmp_path: Path) -> None:
    problems = check_mounts(tmp_path / "нет.jsonl", tmp_path / "нет-набора", tmp_path / "нет-eval")
    assert len(problems) == 3
    assert any("вопросов" in problem for problem in problems)
    assert any("набора" in problem for problem in problems)
    assert any("результатов" in problem for problem in problems)
