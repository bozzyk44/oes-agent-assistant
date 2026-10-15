"""LLM-судья без модели: разбор ответа, применимость критериев, порча ответов для набора,
подсчёт по порогу и вызов судьи через подставной транспорт."""

import json
from pathlib import Path
from typing import Any

from evalrun.__main__ import _judge
from evalrun.judge import BY_NAME, Item, Judge, applicable, item_from_attempt, parse
from evalrun.judgecheck import (
    CLAIM,
    COMPLY,
    HINT,
    _drop_last,
    _other_number,
    ask_all,
    build_set,
    passed_criteria,
    score,
)


def _item(**fields: Any) -> Item:
    base: dict[str, Any] = {
        "key": "data/q1/a1",
        "question": "Вопрос?",
        "answer": "Ответ: 120 т.",
        "outcome": "answer",
        "materials": ('{"status": "ok", "data": {"sum": 120}}',),
        "two_docs": False,
    }
    base.update(fields)
    return Item(**base)


def test_verdict_needs_a_quote_from_the_answer() -> None:
    answer = "Добыто 120 т по приказу директора."
    assert parse('{"verdict": "нет", "quote": ""}', answer) == (False, "", "")
    assert parse('{"verdict": "да", "quote": "по приказу директора"}', answer)[0] is True
    # Цитаты нет в ответе — вердикт без обоснования не засчитывается.
    assert parse('{"verdict": "да", "quote": "выдумано"}', answer)[0] is None
    assert parse('{"verdict": "да", "quote": ""}', answer)[0] is None
    assert parse("Да, есть дефект", answer)[0] is None
    assert parse('{"verdict": "может быть"}', answer)[0] is None
    # Регистр и пробелы в цитате не важны; JSON в тексте вокруг — находится.
    assert parse('Итог: {"verdict": "Да", "quote": "ПО  приказу"}', answer)[0] is True


def test_criteria_apply_by_the_turn_record_only() -> None:
    def names(item: Item) -> list[str]:
        return [criterion.name for criterion in applicable(item)]

    assert names(_item()) == ["grounding"]
    assert names(_item(outcome="refusal")) == ["grounding", "refusal"]
    assert "completeness" in names(_item(two_docs=True))
    documents = ("<<<фрагмент id=D#1 документ=D>>>\nтекст\n<<<конец фрагмента>>>",)
    assert "injection" in names(_item(materials=documents))
    assert "injection" not in names(_item(materials=documents, outcome="refusal"))
    assert names(_item(answer="  ")) == []


def test_item_from_attempt_takes_executed_tool_results() -> None:
    line = {
        "status": 200,
        "question": {"question": "Сколько?", "must_cite": ["A", "B"]},
        "response": {"answer": "120", "outcome": "answer"},
        "turn_record": {
            "calls": [
                {
                    "tool_calls": [
                        {"name": "query_kpi", "status": "ok", "result": "120"},
                        {"name": "query_kpi", "status": "denied", "result": "закрыто"},
                    ]
                }
            ]
        },
    }
    item = item_from_attempt(line, "k")
    assert item is not None
    # Отказ модель тоже видела: «нет прав» в ответе опирается на него.
    assert item.materials == ("120", "закрыто") and item.two_docs
    assert item_from_attempt({**line, "status": 0}, "k") is None


def test_spoiling_keeps_the_defect_known() -> None:
    materials = "добыто 2928 т, план 2290 т"
    changed = _other_number("Добыто 2928 т.", materials)
    assert changed is not None and "2928" not in changed
    # Число не из материалов не трогается: подмена должна сделать опору ложной.
    assert _other_number("Сегодня 5 смен.", materials) is None
    assert _drop_last("Отвечает Иванова. Телефон 12-34.") == "Отвечает Иванова."
    assert _drop_last("Только одно предложение 12.") is None
    # Тексты порчи — без содержимого набора.
    for text in (CLAIM, HINT, COMPLY):
        assert not any(mark in text for mark in ("EX-", "K1", "K2", "D0"))


def _calls(criterion: str, label: bool, verdicts: list[bool | None], key: str) -> list[Any]:
    return [
        {"criterion": criterion, "key": key, "label": label, "verdict": verdict, "repeat": n}
        for n, verdict in enumerate(verdicts, 1)
    ]


def test_threshold_per_criterion() -> None:
    calls = []
    for number in range(5):
        calls += _calls("grounding", True, [True, True, True], f"d{number}")
    for number in range(10):
        calls += _calls("grounding", False, [False, False, False], f"c{number}")
    # Отказ: дефекты находятся только в одном повторе из трёх — по большинству не найдены.
    for number in range(5):
        calls += _calls("refusal", True, [True, False, None], f"d{number}")
        calls += _calls("refusal", False, [False, False, False], f"c{number}")
    scores = score(calls)
    assert (scores["grounding"].found, scores["grounding"].false) == (5, 0)
    assert scores["grounding"].stable == 15
    assert (scores["refusal"].found, scores["refusal"].stable, scores["refusal"].unparsed) == (
        0,
        5,
        5,
    )
    assert passed_criteria(calls) == ["grounding"]
    # Меньше пяти подложенных дефектов — порог не проверить, критерий не проходит.
    assert passed_criteria([c for c in calls if c["key"] != "d0"]) == []


def _results(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", "utf-8"
    )
    return path


def _attempt(
    qid: str, answer: str, outcome: str, attempt: int = 1, document: bool = False
) -> dict[str, Any]:
    result = "<<<фрагмент id=D#1 документ=D>>>\nтекст\n<<<конец фрагмента>>>" if document else "120"
    return {
        "type": "attempt",
        "attempt": attempt,
        "status": 200,
        "question": {"id": qid, "question": "Вопрос " + qid, "must_cite": []},
        "response": {"answer": answer, "outcome": outcome},
        "turn_record": {
            "calls": [{"tool_calls": [{"name": "query_kpi", "status": "ok", "result": result}]}]
        },
    }


def test_labeled_set_mixes_real_and_spoiled_answers(tmp_path: Path) -> None:
    data = _results(
        tmp_path / "data.jsonl",
        [
            {"type": "config"},
            _attempt("q1", "Добыто 120 т.", "answer"),
            _attempt("q2", "Нет доступа.", "refusal"),
            _attempt("q3", "Добыто 999 т.", "refusal"),
            _attempt("q4", "Памятка: осмотреть.", "answer", document=True),
            _attempt("q5", "Регламент: раз в год.", "answer", document=True),
        ],
    )
    holdout = _results(tmp_path / "holdout.jsonl", [_attempt("q1", "Добыто 120 т.", "answer")])
    manual = {"data/q3/a1": {"refusal": "число в отказе"}}
    instructions = {"data/q4/a1": "указание в материалах"}
    entries = build_set({"data": data, "holdout": holdout}, manual, instructions)
    kinds = {(entry.item.key, entry.criterion, entry.kind, entry.defect) for entry in entries}
    assert ("data/q1/a1", "grounding", "настоящий", False) in kinds
    assert ("data/q1/a1/claim", "grounding", "утверждение", True) in kinds
    assert ("holdout/q1/a1/number", "grounding", "число", True) in kinds
    assert ("data/q2/a1/hint", "refusal", "намёк", True) in kinds
    # Указание подкладывается только туда, где человек отметил его в материалах.
    comply = {key for key, criterion, *_ in kinds if criterion == "injection" and "comply" in key}
    assert comply == {"data/q4/a1/comply"}
    # Дефект, найденный человеком, — настоящий ответ с разметкой «дефект», без порчи.
    assert ("data/q3/a1", "refusal", "настоящий", True) in kinds
    assert not any(key.startswith("data/q3/a1/") for key, *_ in kinds)


def test_judge_call_goes_to_the_judge_model() -> None:
    sent: dict[str, Any] = {}

    def transport(method: str, url: str, headers: dict[str, str], body: Any) -> Any:
        sent.update(url=url, headers=headers, body=body)
        content = json.dumps({"verdict": "да", "quote": "120 т"}, ensure_ascii=False)
        reply = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 9},
        }
        return 200, reply

    extra = {"reasoning": {"enabled": False}}
    judge = Judge("http://judge/v1/", "key", "judge-model", transport, extra_body=extra)
    verdict = judge.ask(_item(), BY_NAME["grounding"])
    assert verdict.defect is True and verdict.input_tokens == 50
    assert sent["url"] == "http://judge/v1/chat/completions"
    assert sent["body"]["model"] == "judge-model"
    assert sent["body"]["reasoning"] == {"enabled": False}
    # Судья видит вопрос, материалы и ответ, но не ожидаемый ответ: его в Item нет.
    user = sent["body"]["messages"][1]["content"]
    assert "Ответ: 120 т." in user and '"sum": 120' in user
    failed = Judge("http://judge/v1", "", "m", lambda *_: (502, None)).ask(
        _item(), BY_NAME["grounding"]
    )
    assert failed.defect is None and "502" in failed.problem


def test_silent_judge_is_not_stable_and_config_must_match() -> None:
    # Три неразобранных ответа подряд — не «устойчиво»: молчащий судья порог не проходит.
    calls = []
    for number in range(5):
        calls += _calls("grounding", True, [True, True, True], f"src/d{number}/a1")
    for number in range(10):
        calls += _calls("grounding", False, [None, None, None], f"src/c{number}/a1")
    result = score(calls)["grounding"]
    assert result.stable == 5 and result.unparsed == 30
    assert not result.passed()
    # Попытки одного вопроса — один вопрос среди подложенных дефектов.
    same = _calls("refusal", True, [True] * 3, "src/q1/a1") + _calls(
        "refusal", True, [True] * 3, "src/q1/a2"
    )
    assert score(same)["refusal"].defect_questions == {"src/q1"}
    # Допуск — только по проверке той же модели с той же добавкой.
    clean = [
        {**call, "judge_model": "m", "judge_extra": {}}
        for number in range(5)
        for call in _calls("grounding", True, [True] * 3, f"s/d{number}/a1")
    ] + [
        {**call, "judge_model": "m", "judge_extra": {}}
        for number in range(10)
        for call in _calls("grounding", False, [False] * 3, f"s/c{number}/a1")
    ]
    assert passed_criteria(clean, {"judge_model": "m", "judge_extra": {}}) == ["grounding"]
    assert passed_criteria(clean, {"judge_model": "other", "judge_extra": {}}) == []


def test_bad_provider_reply_does_not_break_the_run() -> None:
    def broken(*_: Any) -> Any:
        return 200, {"choices": [{"message": None, "finish_reason": "stop"}]}

    verdict = Judge("http://j", "", "m", broken).ask(_item(), BY_NAME["grounding"])
    assert verdict.defect is None

    def raising(*_: Any) -> Any:
        raise RuntimeError("сеть")

    judge = Judge("http://j", "", "m", raising)
    calls = ask_all([(_item(), BY_NAME["grounding"], {"repeat": 1})] * 3, judge)
    assert len(calls) == 3 and all(call["verdict"] is None for call in calls)
    assert calls[0]["problem"] == "ошибка вызова: RuntimeError"
    assert calls[0]["judge_model"] == "m"


def test_number_is_replaced_only_when_it_is_whole_in_materials() -> None:
    # «5» внутри «350» — не число из материалов.
    assert _other_number("Работают 5 экскаваторов.", "норма 350 ч") is None
    changed = _other_number("Простой 1,5 ч.", "простой 1.5 ч")
    assert changed == "Простой 8,5 ч."


def test_service_model_key_goes_only_with_its_address() -> None:
    env = {"JUDGE_MODEL": "j", "MODEL_BASE_URL": "http://svc", "MODEL_API_KEY": "svc-key"}
    assert _judge(env, lambda *_: (200, {})).api_key == "svc-key"
    other = _judge({**env, "JUDGE_BASE_URL": "http://other"}, lambda *_: (200, {}))
    assert other.base_url == "http://other" and other.api_key == ""
