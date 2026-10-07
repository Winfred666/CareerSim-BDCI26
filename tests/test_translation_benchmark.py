"""Tests for the deterministic observation translation benchmark."""

import asyncio
from dataclasses import replace
import json
import re
import sqlite3
from pathlib import Path

import pytest

from career_sim_runner.models import TokenUsage
from career_sim_runner.translation_benchmark import cases, dictionary, driver, report, rescore, scoring, storage


def _case() -> cases.TranslationCase:
    return cases.TranslationCase(
        case_id="event-1:node-root",
        shorttitle="测试事件·root",
        observation={
            "current_event": {"title": "测试事件"},
            "choices": [
                {"choice": 1, "action": "选择稳健方案"},
                {"choice": 2, "action": "选择冒险方案"},
            ],
        },
        expected={
            1: {"H": "+", "N": "++", "R": "-"},
            2: {},
        },
    )


def test_expected_metrics_keeps_hidden_risk_sign_from_observe_rules() -> None:
    expected = cases.expected_metrics({"Health": -2, "Network": 1, "HiddenRisk": 3, "Level": -1})
    assert expected == {"H": "--", "N": "+", "R": "+++"}


def test_batch_prompt_attaches_hints_to_the_correct_option() -> None:
    case = _case()
    prompt = driver.render_batch_prompt(
        [case], "职场黑话\t高效指标表达\n", "逐指标判断。",
        {case.case_id: ["L3 全案候选\tR+"]},
        {case.case_id: {1: ["L4 稳健候选\tR-"], 2: ["L5 冒险候选\tR++"]}},
    )
    assert '"dictionary_matches_by_choice":{"1":[4],"2":[5]}' in prompt
    assert '"dictionary_matches":[3]' in prompt
    assert prompt.count("逐指标判断。") == 1
    assert "重新检查" not in prompt
    assert "GT" not in prompt


def test_resume_restores_scored_prefix_and_exact_prompt(tmp_path: Path) -> None:
    case = _case()
    document = {"shorttitle": case.shorttitle, "options": [
        {"choice": 1, "metrics": {"H": "+", "D": "unknown", "S": "unknown", "N": "++",
                                  "O": "unknown", "W": "unknown", "R": "-"}},
        {"choice": 2, "metrics": {metric: "unknown" for metric in "HDSNOWR"}},
    ]}
    prompt = "逐指标查词：\n```tsv\n帮助同事\tN++\n```"
    response = json.dumps(document, ensure_ascii=False)
    payload = driver._result_payload(case, prompt, response, TokenUsage(total_tokens=12),
                                     scoring.score_case(case, document))
    payload["jiuwen_session_id"] = "resume-test-b001"
    output = tmp_path / "run"
    output.mkdir()
    metadata = {
        "run_id": "run", "seed": "fixed", "session_id": "resume-test",
        "dataset_root": "/dataset", "dictionary_sha256": "hash", "output_dir": str(output),
        "case_count": 1, "mode": "dev-full", "batch_size": 1, "session_count": 1,
        "option_count": 2, "compression_rate": 0.5, "scoring_mode": "deterministic_structural",
    }
    (output / "events.md").write_text(report.render_events(metadata, [payload]))
    ledger = storage.Ledger(tmp_path / "benchmark.sqlite3")
    ledger.begin(metadata)
    ledger.record("run", 1, payload)
    run_row, saved = ledger.resumable_run("run")
    restored = driver._restore_results(output, saved, [case])
    assert run_row["session_id"] == "resume-test"
    assert restored[0]["input_prompt"] == prompt
    assert restored[0]["response_text"] == response
    assert restored[0]["direction_scores"] == {str(k): v for k, v in payload["direction_scores"].items()}
    ledger.finish("run", {
        "metric_correct": payload["metric_correct"], "metric_total": payload["metric_total"],
        "option_correct": payload["option_correct"], "option_total": payload["option_total"],
        "direction_score": payload["direction_score"], "direction_option_total": 2,
        "false_positive_count": payload["false_positive_count"],
        "false_negative_count": payload["false_negative_count"],
    })
    with pytest.raises(ValueError, match="already complete"):
        ledger.resumable_run("run")


@pytest.mark.asyncio
async def test_empty_jiuwen_responses_retry_batch_instead_of_scoring_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def blank_chat(_socket, session, _prompt, _timeout, _output):
        calls.append(session)
        return "", TokenUsage()

    monkeypatch.setattr(driver, "_chat", blank_chat)
    with pytest.raises(TimeoutError, match="no translation text"):
        await driver._chat_with_retries(
            object(), "empty", "prompt", 1, tmp_path, scoring.parse_json_object, attempts=3,
        )
    assert len(calls) == 3


def test_dev_dictionary_attribution_counts_distinct_events_and_preserves_live_format() -> None:
    source = "职场黑话\t高效指标表达\n【通用】\n帮助同事\tN++\n减轻负荷\tH+\n"
    rows = dictionary.dictionary_rows(source)
    assert [row.line for row in rows] == [3, 4]
    assert "L3 帮助同事\tN++" in dictionary.numbered_dictionary(source, rows)
    claims, errors = dictionary.parse_activations(
        {"options": [
            {"choice": 1, "dictionary_lines": [3, 3]},
            {"choice": 2, "dictionary_lines": [3, 4]},
        ]},
        {1, 2}, rows,
    )
    assert errors == []
    assert claims == {1: [3], 2: [3, 4]}
    counts = dictionary.activation_counts([
        {"dictionary_activations": claims},
        {"dictionary_activations": {1: [3]}},
    ], rows)
    assert counts == {3: 2, 4: 1}
    assert dictionary.activation_tsv(source, counts) == (
        "职场黑话\t高效指标表达\tactivation time\n【通用】\n"
        "帮助同事\tN++\t2\n减轻负荷\tH+\t1\n"
    )
    assert source.splitlines()[2].count("\t") == 1
    assert dictionary.parse_activations(
        {"options": [{"choice": 1, "dictionary_lines": [99]}]}, {1}, rows
    )[1] == ["option 1 dictionary_lines contains an invalid line"]
    grouped = {metric: [] for metric in "HDSNOWR"}
    grouped["N"] = [3, 3]
    grouped["H"] = [4]
    assert dictionary.parse_activations(
        {"options": [{"choice": 1, "dictionary_lines": grouped}]}, {1}, rows
    ) == ({1: [3, 4]}, [])
    grouped["S"] = [3]
    assert dictionary.parse_activations(
        {"options": [{"choice": 1, "dictionary_lines": grouped}]}, {1}, rows
    ) == ({1: [3, 4]}, ["option 1 dictionary_lines contains an invalid metric line"])


def test_dictionary_accepts_single_and_bundled_metric_rows() -> None:
    assert len(dictionary.dictionary_rows(
        "职场黑话\t高效指标表达\n半年谈话技能不足\tS短板线索\n"
        "做成项目\tO++,S++\n技术进步\tS+2\n"
    )) == 3
    for invalid in ("O++,O+", "O++,", "O++,unknown", "O++,S短板线索", "O+-,S++"):
        with pytest.raises(ValueError, match="invalid metric effects"):
            dictionary.dictionary_rows(f"职场黑话\t高效指标表达\n做成项目\t{invalid}\n")
    with pytest.raises(ValueError, match="two columns"):
        dictionary.dictionary_rows("职场黑话\t高效指标表达\n做成项目\tO++\t3\n")


def test_bundled_row_attribution_accepts_applicable_components_and_counts_one_event() -> None:
    source = "职场黑话\t高效指标表达\n【特殊情境】\n深夜赶工\tH--,O+,R+\n"
    rows = dictionary.dictionary_rows(source)
    grouped = {metric: [] for metric in "HDSNOWR"}
    grouped["H"] = [3]
    grouped["R"] = [3]
    claims, errors = dictionary.parse_activations(
        {"options": [{"choice": 1, "dictionary_lines": grouped},
                     {"choice": 2, "dictionary_lines": [3]}]}, {1, 2}, rows,
    )
    assert errors == []
    assert claims == {1: [3], 2: [3]}
    counts = dictionary.activation_counts([{"dictionary_activations": claims}], rows)
    assert counts == {3: 1}
    assert "深夜赶工\tH--,O+,R+\t1" in dictionary.activation_tsv(source, counts)
    grouped["S"] = [3]
    assert dictionary.parse_activations(
        {"options": [{"choice": 1, "dictionary_lines": grouped}]}, {1}, rows,
    ) == ({1: [3]}, ["option 1 dictionary_lines contains an invalid metric line"])


@pytest.mark.parametrize("initial_r", ["unknown", "+"])
@pytest.mark.parametrize("empty_first_review", [False, True])
@pytest.mark.asyncio
async def test_batched_translation_never_rechecks_sparse_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_r: str, empty_first_review: bool,
) -> None:
    skill = tmp_path / "solution" / "skills" / "observe-decide-review"
    (skill / "notebooks").mkdir(parents=True)
    (skill / "stages").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: observe-decide-review\n---\n[观察翻译](stages/observe.md)\n"
    )
    (skill / "stages" / "observe.md").write_text(
        "# 观察\n\n5. 正式翻译\n\n读取 `notebooks/event-translator-dictionary.tsv`，逐指标查词。\n"
    )
    (skill / "notebooks" / "event-translator-dictionary.tsv").write_text(
        "职场黑话\t高效指标表达\n帮助同事\tN++\n"
    )
    case = replace(_case(), expected={1: _case().expected[1], 2: {"N": "+", "R": "+"}})

    async def fake_cases(_seed, _limit, _exclude=None):
        return [case], "/dataset"

    calls = []

    async def fake_chat(_socket, session, prompt, _timeout, output):
        calls.append((session, prompt))
        blank = {metric: "unknown" for metric in "HDSNOWR"}
        options = [
            {"choice": choice, "dictionary_lines": [], "metrics": blank.copy()}
            for choice in case.expected
        ]
        options[0]["dictionary_lines"] = [2]
        options[0]["metrics"].update({"H": "+", "N": "++", "R": "-"})
        options[1]["metrics"]["R"] = initial_r
        if "复核选项" in prompt:
            if empty_first_review and session.endswith("-a1"):
                (output / "events.jsonl").write_text("{}\n")
                return "", TokenUsage(total_tokens=1)
            options[1]["metrics"].update({"N": "+", "R": "+"})
            response = {"shorttitle": case.shorttitle, "options": options}
        else:
            response = {"cases": [{"case_id": case.case_id,
                                   "shorttitle": case.shorttitle, "options": options}]}
        (output / "events.jsonl").write_text("{}\n")
        return json.dumps(response, ensure_ascii=False), TokenUsage(total_tokens=1)

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def recv(self):
            return "{}"

    monkeypatch.setattr(driver, "load_cases", fake_cases)
    monkeypatch.setattr(driver, "_chat", fake_chat)
    monkeypatch.setattr(driver.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    result = await driver.run(
        solution=tmp_path / "solution", skill_id="observe-decide-review",
        seed="test", limit=1, mode="dev-batch", ws_url="ws://unused", timeout_s=1,
        output_root=tmp_path / "benchmark",
    )
    assert len(calls) == 1
    assert result["option_correct"] == 1
    assert result["effect_review_count"] == 0
    assert result["dictionary_event_activations"] == 1
    output = Path(result["output_dir"])
    assert not (output / "review-attempts.jsonl").exists()


@pytest.mark.parametrize(
    ("batch_size", "expected_peak", "expected_sessions", "offset"),
    [(4, 2, 2, 0), (1, 4, 8, 0), (2, 3, 3, 2)],
)
@pytest.mark.asyncio
async def test_batched_probe_runs_independent_sessions_and_records_fixed_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    batch_size: int, expected_peak: int, expected_sessions: int, offset: int,
) -> None:
    skill = tmp_path / "solution" / "skills" / "observe-decide-review"
    (skill / "notebooks").mkdir(parents=True)
    (skill / "stages").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: observe-decide-review\n---\n[观察翻译](stages/observe.md)\n"
    )
    (skill / "stages" / "observe.md").write_text(
        "# 观察\n\n5. 正式翻译\n\n读取 `notebooks/event-translator-dictionary.tsv`；逐项判断。\n"
    )
    (skill / "notebooks" / "event-translator-dictionary.tsv").write_text(
        "职场黑话\t高效指标表达\n帮助同事\tN++\n"
    )
    batch_cases = [
        replace(_case(), case_id=f"event-{i}:node-root", shorttitle=f"测试事件{i}·root")
        for i in range(8)
    ]
    by_id = {case.case_id: case for case in batch_cases}
    active = 0
    peak = 0

    async def fake_cases(_seed, _limit, _exclude=None):
        return batch_cases, "/dataset"

    async def fake_chat(_socket, session, prompt, _timeout, output):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02 if "-b001-" in session else 0.005)
        active -= 1
        ids = [
            case_id for case_id in re.findall(r'"case_id":"([^"]+)"', prompt)
            if case_id in by_id
        ]
        documents = []
        for case_id in ids:
            case = by_id[case_id]
            documents.append({
                "case_id": case_id, "shorttitle": case.shorttitle,
                "options": [
                    {"choice": choice, "metrics": {
                        metric: case.expected[choice].get(metric, "unknown")
                        for metric in cases.METRICS
                    }, "dictionary_lines": [2] if choice == 1 else []}
                    for choice in case.expected
                ],
            })
        (output / "events.jsonl").write_text(json.dumps({"session": session}) + "\n")
        return json.dumps({"cases": documents}, ensure_ascii=False), TokenUsage(total_tokens=1)

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def recv(self):
            return "{}"

    monkeypatch.setattr(driver, "load_cases", fake_cases)
    monkeypatch.setattr(driver, "_chat", fake_chat)
    monkeypatch.setattr(driver.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    root = tmp_path / "benchmark"
    result = await driver.run(
        solution=tmp_path / "solution", skill_id="observe-decide-review",
        seed="fixed", limit=8, mode="dev-batch", ws_url="ws://unused",
        timeout_s=1, batch_size=batch_size, offset=offset, output_root=root,
    )
    assert peak == expected_peak
    assert result["session_count"] == expected_sessions
    assert result["batch_size"] == batch_size
    assert result["offset"] == offset
    with sqlite3.connect(root / "benchmark.sqlite3") as database:
        assert [row[0] for row in database.execute(
            "SELECT case_id FROM results ORDER BY position"
        )] == [case.case_id for case in batch_cases[offset:]]
    streams = (Path(result["output_dir"]) / "events.jsonl").read_text().splitlines()
    assert len(streams) == expected_sessions
    assert all(f"-b{index:03d}-" in stream for index, stream in enumerate(streams, start=1))


@pytest.mark.parametrize("mode", ["dev-full", "dev-batch"])
@pytest.mark.asyncio
async def test_dev_run_records_compression_and_dictionary_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    solution = tmp_path / "solution"
    skill = solution / "skills" / "observe-decide-review"
    (skill / "notebooks").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: observe-decide-review\n---\n[观察翻译](stages/observe.md)\n", encoding="utf-8"
    )
    (skill / "stages").mkdir()
    (skill / "stages" / "observe.md").write_text(
        "# 观察\n\n5. 正式翻译\n\n读取 `notebooks/event-translator-dictionary.tsv`；逐项判断。\n",
        encoding="utf-8",
    )
    (skill / "notebooks" / "event-translator-dictionary.tsv").write_text(
        "职场黑话\t高效指标表达\n帮助同事\tN++\n", encoding="utf-8"
    )
    case = _case()

    async def fake_cases(_seed, _limit, _exclude=None):
        return [case], "/dataset"

    async def fake_chat(_socket, _session, prompt, _timeout, output):
        assert "L2 帮助同事\tN++" in prompt
        assert "dictionary_lines" in prompt
        (output / "events.jsonl").write_text("{}\n", encoding="utf-8")
        response = {"shorttitle": case.shorttitle, "options": [
            {"choice": 1, "metrics": {metric: case.expected[1].get(metric, "unknown")
                                       for metric in cases.METRICS}, "dictionary_lines": [2]},
            {"choice": 2, "metrics": {metric: "unknown" for metric in cases.METRICS},
             "dictionary_lines": [2]},
        ]}
        return json.dumps({"cases": [{"case_id": case.case_id, **response}]}, ensure_ascii=False), TokenUsage(total_tokens=8)

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def recv(self):
            return "{}"

    monkeypatch.setattr(driver, "load_cases", fake_cases)
    monkeypatch.setattr(driver, "_chat", fake_chat)
    monkeypatch.setattr(driver.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())
    root = tmp_path / "benchmark"
    result = await driver.run(
        solution=solution, skill_id="observe-decide-review", seed="fixed", limit=0,
        mode=mode, ws_url="ws://unused", timeout_s=1, output_root=root,
    )
    assert result["compression_rate"] == 0.5
    assert result["dictionary_event_activations"] == 1
    assert result["mean_activations_per_line"] == 1
    assert result["active_dictionary_lines"] == 1
    assert "帮助同事\tN++\t1" in Path(result["dictionary_activation_report"]).read_text()
    assert "1/2 = 0.5000" in Path(result["report"]).read_text()
    with sqlite3.connect(root / "benchmark.sqlite3") as database:
        assert database.execute("SELECT mode, compression_rate, dictionary_event_activations FROM runs").fetchone() == (
            mode, 0.5, 1
        )
        assert json.loads(database.execute("SELECT dictionary_activations_json FROM results").fetchone()[0]) == {
            "1": [2], "2": [2]
        }
    rescored = rescore.rescore_existing(root, result["run_id"])
    assert rescored["runs_updated"] == 1
    assert rescored["runs"][0]["direction_score"] == pytest.approx(result["direction_score"])
    with pytest.raises(ValueError, match="run not found"):
        rescore.rescore_existing(root, "missing-run")


@pytest.mark.asyncio
async def test_case_order_is_fixed_by_seed() -> None:
    first, first_root = await cases.load_cases("fixed-seed", 5)
    second, second_root = await cases.load_cases("fixed-seed", 5)
    other, _ = await cases.load_cases("other-seed", 5)

    assert [case.case_id for case in first] == [case.case_id for case in second]
    assert [case.case_id for case in first] != [case.case_id for case in other]
    assert first_root == second_root
    assert all(case.expected for case in first)

    excluded = {case.case_id for case in first}
    unseen, _ = await cases.load_cases("fixed-seed", 5, excluded)
    assert not excluded.intersection(case.case_id for case in unseen)


def _history_scenario():
    from career_emulator.server.models import ScenarioChoice, ScenarioDefinition, ScenarioNode

    def node(name, description, choices):
        return ScenarioNode(name, description, [
            ScenarioChoice(action, {"Network": 999, "HiddenCanary": 987654}, target)
            for action, target in choices
        ])

    return ScenarioDefinition("history-test", "协商安排", "root", {
        "root": node("root", "先前请求", [("提出边界", "middle"), ("未选的动作", "other")]),
        "middle": node("middle", "对方原则同意", [("商定轮值", "target")]),
        "target": node("target", "当前节点", [("本次待选动作", "future")]),
        "other": node("other", "未发生支线", []),
        "future": node("future", "尚未发生的结局", []),
    })


@pytest.mark.asyncio
async def test_nonroot_history_reaches_prompt_without_hidden_or_future_data(monkeypatch):
    scenario = _history_scenario()

    async def load(_self):
        return {scenario.event_id: scenario}

    monkeypatch.setattr(cases.EventPoolLoader, "load", load)
    loaded, _ = await cases.load_cases("history-test", 0)
    by_id = {case.case_id: case for case in loaded}
    root = by_id["history-test:root"]
    target = by_id["history-test:target"]
    assert "event_history" not in root.observation
    assert target.observation["event_history"]["steps"] == [
        {"description": "先前请求", "selected_choice": {"choice": 1, "action": "提出边界"}},
        {"description": "对方原则同意", "selected_choice": {"choice": 1, "action": "商定轮值"}},
    ]
    assert target.observation["current_event"]["description"] == "当前节点"
    assert target.observation["choices"] == [{"choice": 1, "action": "本次待选动作"}]
    # Expected effects still exist for local scoring; neither they nor sibling/
    # future text may appear in the actual prompt sent to the translator.
    assert target.expected[1]["N"] == "+" * 999
    prompt = driver.render_batch_prompt([target], "词典", "翻译规则", {}, {})
    for text in ("先前请求", "提出边界", "对方原则同意", "商定轮值", "当前节点"):
        assert text in prompt
    for text in ("HiddenCanary", "987654", "status_updates", "未选的动作", "未发生支线", "尚未发生的结局"):
        assert text not in prompt


@pytest.mark.parametrize("problem", ["ambiguous", "unreachable", "cycle"])
def test_history_does_not_invent_a_path(problem):
    from career_emulator.server.models import ScenarioChoice

    scenario = _history_scenario()
    if problem == "ambiguous":
        scenario.nodes["other"].choices = [ScenarioChoice("另一条前情", {}, "target")]
    elif problem == "unreachable":
        scenario.nodes["middle"].choices = []
    else:
        scenario.nodes["other"].choices = [ScenarioChoice("循环", {}, "root")]
    with pytest.raises(ValueError, match="event history"):
        cases.public_event_history(scenario, "target")


def test_score_reports_wrong_option_and_ignores_uncertainty_marker() -> None:
    case = _case()
    document = {
        "shorttitle": case.shorttitle,
        "options": [
            {
                "choice": 1,
                "metrics": {
                    "H": "+?",
                    "D": "unknown",
                    "S": "unknown",
                    "N": "+",
                    "O": "unknown",
                    "W": "unknown",
                    "R": "-?",
                },
            },
            {"choice": 2, "metrics": {metric: "unknown" for metric in cases.METRICS}},
        ],
    }

    score = scoring.score_case(case, document)

    assert score.wrong_options == [1]
    assert score.mismatches == {1: {"N": {"expected": "++", "actual": "+"}}}
    assert score.predicted == {1: {"H": "+?", "N": "+", "R": "-?"}, 2: {}}
    assert score.false_positives == {}
    assert score.false_positive_count == 0
    assert score.false_negative_count == 1
    assert score.metric_correct == 13
    assert score.metric_total == 14
    assert score.option_correct == 1
    assert score.direction_scores == {1: pytest.approx(1.0), 2: 1.0}
    assert score.direction_score == pytest.approx(1.0)


def test_direction_vector_weights_and_signed_cosine() -> None:
    assert scoring.effect_value("H", "unknown") == 0
    assert scoring.effect_value("H", "+?") == 0.5
    assert scoring.effect_value("H", "+") == 1
    assert scoring.effect_value("H", "++?") == 1.5
    assert scoring.effect_value("H", "++") == 2
    assert scoring.effect_value("H", "---?") == -2.5
    assert scoring.direction_cosine({"H": "++"}, {"H": "---?"}) == pytest.approx(-1)
    assert scoring.direction_cosine({}, {}) == 1
    assert scoring.direction_cosine({"H": "+"}, {}) == 0
    assert scoring.direction_cosine({"D": "+++"}, {}) == 1
    assert scoring.direction_cosine({"H": "+", "R": "+"}, {"R": "+"}) == pytest.approx(
        5 / (30**0.5)
    )


def test_numbered_metric_strings_are_canonicalized_without_changing_direction() -> None:
    assert scoring.normalize_metric("S", "+3?") == "+++"
    assert scoring.display_metric("S", "S+3?") == "+++?"
    assert scoring.effect_value("R", "-2?") == -1.5


def test_classification_errors_count_excess_missing_and_wrong_direction() -> None:
    false_positives, false_negatives = scoring.classification_errors(
        {
            1: {"H": "+", "N": "++", "O": "+", "R": "-"},
            2: {"D": "++"},
            3: {"H": "-"},
            4: {"H": "-"},
        },
        {
            1: {"H": "++", "D": "+", "N": "+", "R": "+"},
            2: {},
            3: {"H": "-?"},
            4: {"H": "--?"},
        },
    )

    assert false_positives == {
        1: {
            "H": {"expected": "+", "predicted": "++"},
            "D": {"expected": "unknown", "predicted": "+"},
            "R": {"expected": "-", "predicted": "+"},
        }
    }
    assert false_negatives == {
        1: {
            "N": {"expected": "++", "predicted": "+"},
            "O": {"expected": "+", "predicted": "unknown"},
            "R": {"expected": "-", "predicted": "+"},
        }
    }


def test_parse_json_object_accepts_fenced_response() -> None:
    assert scoring.parse_json_object('result:\n```json\n{"options": []}\n```') == {"options": []}


def test_parse_json_object_uses_last_complete_streamed_response() -> None:
    text = '{"shorttitle":"中间段","options":[]}\n{"shorttitle":"最终段","options":[]}'

    assert scoring.parse_json_object(text)["shorttitle"] == "最终段"


def test_parse_json_object_rejects_nested_object_from_malformed_envelope() -> None:
    with pytest.raises(ValueError, match="does not contain"):
        scoring.parse_json_object('{"shorttitle":"截断","options":[{"choice":1,"metrics":{"H":"+"}}]')


def test_parse_batch_response_uses_final_complete_envelope() -> None:
    response = ('{"cases":[{"case_id":"old","options":[]}]}\n'
                '```json\n{"cases":[{"case_id":"new","options":[]}]}\n```')
    assert list(driver.parse_batch_response(response)) == ["new"]


@pytest.mark.asyncio
async def test_chat_retry_keeps_full_run_alive_after_timeout_and_empty_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions: list[str] = []

    async def fake_chat(_socket, session, _prompt, _timeout, _output):
        sessions.append(session)
        if len(sessions) == 1:
            raise TimeoutError("model timed out")
        if len(sessions) == 2:
            return "", TokenUsage(total_tokens=3)
        return '{"shorttitle":"测试","options":[]}', TokenUsage(total_tokens=5)

    monkeypatch.setattr(driver, "_chat", fake_chat)
    response, usage, session = await driver._chat_with_retries(
        object(), "batch", "prompt", 1, tmp_path, scoring.parse_json_object, attempts=3
    )
    assert len(set(sessions)) == 3
    assert all(re.fullmatch(r"batch-r[0-9a-f]{8}-a[123]", value) for value in sessions)
    assert session == sessions[-1]
    await driver._chat_with_retries(
        object(), "batch", "prompt", 1, tmp_path, scoring.parse_json_object, attempts=1
    )
    assert sessions[-1].rsplit("-a", 1)[0] != sessions[0].rsplit("-a", 1)[0]
    assert response == '{"shorttitle":"测试","options":[]}'
    assert usage.total_tokens == 8


def test_missing_metric_is_a_structural_mismatch() -> None:
    case = _case()
    first_prediction = {metric: case.expected[1].get(metric, "unknown") for metric in cases.METRICS}
    second_prediction = {metric: "unknown" for metric in cases.METRICS}
    score = scoring.score_case(
        case,
        {
            "shorttitle": case.shorttitle,
            "options": [
                {"choice": 1, "metrics": {key: value for key, value in first_prediction.items() if key != "D"}},
                {"choice": 2, "metrics": second_prediction},
            ],
        },
    )

    assert score.actual[1]["D"] == "missing"
    assert score.wrong_options == []
    assert score.metric_correct == 13
    assert "option 1 missing metric D" in score.errors


def test_report_omits_pred_group_when_translator_predicts_no_effect() -> None:
    rendered = report.render_report(
        {
            "seed": "fixed",
            "session_id": "session",
            "dataset_root": "/dataset",
            "dictionary_sha256": "hash",
            "scoring_mode": "deterministic_structural",
            "case_count": 1,
        },
        [
            {
                "shorttitle": "测试事件·root",
                "choice_texts": {1: "选择稳健方案"},
                "expected": {1: {"H": "+"}},
                "actual": {1: {metric: "unknown" for metric in cases.METRICS}},
                "predicted": {1: {}},
                "wrong_options": [1],
                "mismatches": {1: {"H": {"expected": "+", "actual": "unknown"}}},
                "errors": [],
                "usage": {},
                "metric_correct": 6,
                "metric_total": 7,
                "option_correct": 0,
                "option_total": 1,
            }
        ],
    )

    assert "1. 选择稳健方案 (GT:H+)" in rendered
    assert "(Pred:" not in rendered
    assert "综合分（主方向余弦）: 0.00%" in rendered
    assert "FP（多报/夸大）: 0" in rendered
    assert "FN（漏报/低估；排除 D）: 1" in rendered


@pytest.mark.asyncio
async def test_chat_ignores_stale_completion_by_request_id(tmp_path: Path) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.request_id = ""
            self.frames: list[dict[str, object]] = []

        async def send(self, payload: str) -> None:
            self.request_id = json.loads(payload)["request_id"]
            self.frames = [
                {
                    "request_id": "preceding-request",
                    "response_kind": "e2a.complete",
                    "status": "succeeded",
                    "is_final": True,
                },
                {
                    "request_id": self.request_id,
                    "response_kind": "e2a.complete",
                    "status": "succeeded",
                    "is_final": True,
                },
            ]

        async def recv(self) -> str:
            return json.dumps(self.frames.pop(0))

    response, usage = await driver._chat(FakeSocket(), "session", "prompt", 1, tmp_path)

    assert response == ""
    assert usage.total_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("translation_step", [6, 7, 11])
@pytest.mark.parametrize("layout", ["plain", "with_setup", "substeps"])
@pytest.mark.parametrize("role_location", ["top_level", "metadata", "links"])
async def test_run_writes_database_and_shorttitle_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, translation_step: int, layout: str, role_location: str
) -> None:
    solution = tmp_path / "solution"
    dictionary_path = solution / "skills" / "observe-decide-review" / "notebooks" / "event-translator-dictionary.tsv"
    dictionary_path.parent.mkdir(parents=True)
    dictionary_path.write_text("职场黑话\t高效指标表达\n帮忙\tN++\n", encoding="utf-8")
    skill_path = solution / "skills" / "observe-decide-review" / "SKILL.md"
    roles_yaml = (
        "roles:\n"
        "  - id: observation-translator\n"
        "    path: roles/observation-translator.md\n"
    )
    if role_location == "metadata":
        roles_yaml = "metadata:\n" + "".join(f"  {line}\n" for line in roles_yaml.splitlines())
    if role_location == "links":
        translator_relative = "stages/observe.md"
        skill_document = (
            "---\nname: observe-decide-review\n---\n"
            "[观察翻译](stages/observe.md)\n[翻译规则](stages/observe.md#rules)\n"
            "[复核](stages/review.md)\n"
        )
    else:
        translator_relative = "roles/observation-translator.md"
        skill_document = f"---\nname: observe-decide-review\n{roles_yaml}---\n"
    skill_path.write_text(skill_document, encoding="utf-8")
    translator_path = skill_path.parent / translator_relative
    translator_path.parent.mkdir(parents=True)
    if role_location == "links":
        (translator_path.parent / "review.md").write_text(
            "# 复核\n\n1. 根据日志维护 event-translator-dictionary.tsv。\n",
            encoding="utf-8",
        )
    opening = (
        f"### {translation_step}. 正式事件翻译\n\n先调用路径脚本。\n\n```bash\nnext-translation-path.sh\n```\n\n"
        if layout == "with_setup"
        else f"{translation_step}. "
    )
    closing = f"{translation_step + 1}. 写入翻译。\n"
    if layout == "substeps":
        opening = f"### {translation_step}.2 翻译事件\n\n"
        closing = f"#### {translation_step}.3 写入翻译。\n"
    translator_path.write_text(
        f"# Translator\n\n{translation_step - 1}. 封顶查验与红线校准。\n\n"
        f"{opening}只读 `<notebooks_directory>/event-translator-dictionary.tsv`。"
        "当前 solution 同步规则：先判断主要影响，再做语义匹配。\n\n"
        f"{closing}",
        encoding="utf-8",
    )
    case = _case()

    async def fake_cases(_seed: str, _limit: int, _exclude_case_ids=None):
        return [case], "/dataset"

    response = {
        "shorttitle": case.shorttitle,
        "options": [
            {
                "choice": 1,
                "metrics": {
                    **{metric: case.expected[1].get(metric, "unknown") for metric in cases.METRICS},
                    "S": "+",
                    "N": "+?",
                },
            },
            {"choice": 2, "metrics": {**{metric: "unknown" for metric in cases.METRICS}, "H": "-?"}},
        ],
    }

    async def fake_chat(_url, _session, _prompt, _timeout, output):
        (output / "events.jsonl").write_text('{"kind":"frame_final"}\n', encoding="utf-8")
        return json.dumps(response, ensure_ascii=False), TokenUsage(total_tokens=42)

    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def recv(self):
            return "{}"

    monkeypatch.setattr(driver.websockets, "connect", lambda *_args, **_kwargs: FakeConnection())

    monkeypatch.setattr(driver, "load_cases", fake_cases)
    monkeypatch.setattr(driver, "_chat", fake_chat)
    output_root = tmp_path / "translation_benchmark"

    result = await driver.run(
        solution=solution,
        skill_id="observe-decide-review",
        seed="fixed",
        limit=1,
        ws_url="ws://unused",
        timeout_s=1,
        output_root=output_root,
    )

    assert result["session_id"].startswith("translation-benchmark-")
    assert result["metric_correct"] == 11
    assert result["false_positive_count"] == 2
    assert result["false_negative_count"] == 1
    assert result["direction_score"] == pytest.approx(7 / (72.5**0.5) / 2)
    report = Path(result["report"]).read_text(encoding="utf-8")
    assert "测试事件·root" in report
    assert "direct structural comparison; no LLM judge" in report
    assert "综合分（主方向余弦）" in report
    assert "D×0, R×5" in report
    assert "`R+` = hidden risk increases" in report
    assert "FP（多报/夸大）: 2" in report
    assert "FN（漏报/低估；排除 D）: 1" in report
    assert "| 1 |" in report
    assert "1. 选择稳健方案 (GT:H+ N++ R-) (Pred:H+ S+ N+? R-)" in report
    assert "#### Option 1" not in report
    assert "| Source |" not in report
    assert "Option 2" not in report
    assert "unknown" not in report.split("## Wrong option details", 1)[1]
    events = Path(result["events"]).read_text(encoding="utf-8")
    assert "### Input observation" in events
    assert '"action": "选择稳健方案"' in events
    assert "### Input prompt sent to Jiuwen" in events
    assert "帮忙\tN++" in events
    assert "当前 solution 同步规则：先判断主要影响，再做语义匹配" in events
    assert "封顶查验与红线校准" not in events
    assert "写入翻译。" not in events
    assert "next-translation-path.sh" not in events
    assert str(solution.resolve()) == result["solution_path"]
    assert result["translator_path"] == f"skills/observe-decide-review/{translator_relative}"
    assert result["solution_skill_sha256"] in events
    assert result["translator_sha256"] in events
    assert result["translation_rules_sha256"] in events
    assert "direction_cosine_by_option" in events
    assert "主方向余弦（选项）" in events
    assert "FP（多报/夸大）: 2" in events
    assert "FN（漏报/低估；排除 D）: 1" in events
    assert "### Raw Jiuwen output" in events
    assert '"shorttitle": "测试事件·root"' in events
    assert "### Parsed deterministic comparison" in events
    assert '"Pred"' in events
    output_dir = Path(result["output_dir"])
    assert {path.name for path in output_dir.iterdir()} == {"benchmark.md", "events.jsonl", "events.md"}
    assert "summary" not in result
    assert "results" not in result
    with sqlite3.connect(output_root / "benchmark.sqlite3") as database:
        assert database.execute("SELECT count(*) FROM runs").fetchone() == (1,)
        assert database.execute("SELECT wrong_options_json FROM results").fetchone() == ("[1]",)
        assert database.execute("SELECT direction_score FROM runs").fetchone()[0] == pytest.approx(
            result["direction_score"]
        )
        assert database.execute("SELECT false_positive_count, false_negative_count FROM runs").fetchone() == (2, 1)
        assert database.execute("SELECT false_positive_count, false_negative_count FROM results").fetchone() == (2, 1)

    rescored = rescore.rescore_existing(output_root)
    assert rescored["mode"] == "offline_rescore"
    assert rescored["runs_updated"] == 1
    assert rescored["results_updated"] == 1
    assert rescored["runs"][0]["false_positive_count"] == 2
    assert rescored["runs"][0]["false_negative_count"] == 1
    assert "综合分（主方向余弦）" in Path(result["report"]).read_text(encoding="utf-8")


def test_omissions_and_reversals_ignore_magnitude_and_exclude_d_w() -> None:
    summary = scoring.directional_error_summary(
        {1: {"O": "+", "S": "---", "N": "++", "H": "-", "R": "+", "D": "+", "W": "-"},
         2: {"O": "-", "S": "+", "N": "+", "H": "+", "R": "-"}},
        {"1": {"O": "unknown", "S": "+?", "N": "+?", "H": "-?", "R": "-2?", "D": "-", "W": "+"},
         "2": {"O": 0, "S": "invalid", "N": None, "H": "", "R": "unknown"},
         "99": {"O": "+"}},
    )
    assert summary["omission_count"] == 6
    assert summary["reversal_count"] == 2
    assert set(summary["omission_metrics"][1]) == {"O"}
    assert set(summary["reversal_metrics"][1]) == {"S", "R"}
    assert scoring.directional_error_summary({1: {}}, {1: {"O": "+"}})["omission_count"] == 0


def test_directional_error_ledger_resume_and_rescore(tmp_path: Path) -> None:
    case = _case()
    document = {"shorttitle": case.shorttitle, "options": [
        {"choice": 1, "metrics": {**dict.fromkeys(cases.METRICS, "unknown"), "R": "+?"}},
        {"choice": 2, "metrics": dict.fromkeys(cases.METRICS, "unknown")},
    ]}
    response = json.dumps(document)
    payload = driver._result_payload(case, "original prompt", response, TokenUsage(), scoring.score_case(case, document))
    payload["jiuwen_session_id"] = "test-session"
    output = tmp_path / "run"
    output.mkdir()
    metadata = {"run_id": "run", "seed": "fixed", "session_id": "test-session", "dataset_root": "/dataset",
                "dictionary_sha256": "hash", "output_dir": str(output), "case_count": 1,
                "scoring_mode": "deterministic_structural"}
    (output / "events.md").write_text(report.render_events(metadata, [payload]))
    (output / "benchmark.md").write_text(report.render_report(metadata, [payload]))
    ledger = storage.Ledger(tmp_path / "benchmark.sqlite3")
    ledger.begin(metadata)
    ledger.record("run", 1, payload)
    _, saved = ledger.resumable_run("run")
    restored = driver._restore_results(output, saved, [case])[0]
    assert (restored["omission_count"], restored["reversal_count"]) == (2, 1)
    for _ in range(2):
        summary = rescore.rescore_existing(tmp_path)["runs"][0]
        assert (summary["omission_count"], summary["reversal_count"]) == (2, 1)
    with sqlite3.connect(ledger.path) as db:
        for table in ("runs", "results"):
            assert db.execute(f"SELECT omission_count, reversal_count FROM {table}").fetchone() == (2, 1)
        assert db.execute("SELECT response_text FROM results").fetchone()[0] == response
    for name in ("events.md", "benchmark.md"):
        text = (output / name).read_text()
        assert text.count("- 遗漏次数（O/S/N/H/R）: 2") == 1
        assert text.count("- 反向次数（O/S/N/H/R）: 1") == 1
    assert "original prompt" in (output / "events.md").read_text()


def test_explicit_unchanged_dictionary_effects_and_predictions() -> None:
    rows = dictionary.dictionary_rows("职场黑话\t高效指标表达\n自研\tS++,O-,R+?,H=\n合规原型\tR=\n")
    assert [row.effects for row in rows] == ["S++,O-,R+?,H=", "R="]
    for value in ("=", "H=", "H=0", "=?"):
        assert scoring.normalize_metric("H", value) == "unknown"
        assert scoring.effect_value("H", value) == 0
    assert scoring.directional_error_summary({1: {"H": "+"}}, {1: {"H": "="}})["omission_count"] == 1


@pytest.mark.asyncio
async def test_first_batch_failure_preserves_resumable_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_cases(*_args):
        return [_case()], "/dataset"

    class FailedConnection:
        async def __aenter__(self):
            raise RuntimeError("fixture connection failure")

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(driver, "load_cases", fake_cases)
    monkeypatch.setattr(driver.websockets, "connect", lambda *_args, **_kwargs: FailedConnection())
    monkeypatch.setattr(driver, "resolve_dictionary", lambda *_: driver.REPO_ROOT / "archive/translation-dictionary/event-translator-dictionary.tsv")
    fixture_skill = driver.REPO_ROOT / "solution/skills/observe-decide-review/SKILL.md"
    monkeypatch.setattr(driver, "resolve_translation_rules", lambda *_: (fixture_skill, fixture_skill, "fixture translation rules"))
    kwargs = dict(solution=driver.REPO_ROOT / "solution", skill_id="observe-decide-review", seed="fixed",
                  limit=0, mode="dev-full", ws_url="ws://unused", timeout_s=1, output_root=tmp_path)
    with pytest.raises(RuntimeError, match="fixture connection failure"):
        await driver.run(**kwargs)
    with sqlite3.connect(tmp_path / "benchmark.sqlite3") as db:
        run_id, output = db.execute("SELECT run_id, output_dir FROM runs").fetchone()
        assert db.execute("SELECT count(*) FROM results").fetchone()[0] == 0
    assert "Cases completed: 0/1" in (Path(output) / "benchmark.md").read_text()
    # Hash validation and empty-prefix restoration succeed before the same fixture failure.
    with pytest.raises(RuntimeError, match="fixture connection failure"):
        await driver.run(**kwargs, resume_run_id=run_id)


@pytest.mark.asyncio
async def test_minimal_translation_retries_only_missing_and_never_reviews(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from career_sim_runner.translation_benchmark import minimal
    first = _case()
    second = replace(first, case_id="second", shorttitle="第二案")
    calls = []
    def document(case):
        return {"case_id": case.case_id, "shorttitle": case.shorttitle, "options": [
            {"choice": c["choice"], "metrics": dict.fromkeys("HDSNOWR", "unknown"), "dictionary_lines": []}
            for c in case.observation["choices"]]}
    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def create(self, **kwargs):
            calls.append(kwargs)
            docs = [document(second)] if len(calls) == 1 else [document(first)]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"cases": docs})))],
                                   usage=SimpleNamespace(prompt_tokens=50, completion_tokens=30, total_tokens=80,
                                                         completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))
    monkeypatch.setattr(minimal, "AsyncOpenAI", Client)
    monkeypatch.setattr(minimal, "dotenv_values", lambda _: {"API_KEY": "test", "API_BASE": "http://unused"})
    records = []
    kwargs = dict(cases=[first, second], start=0, batch_size=2, output=tmp_path,
                  render=lambda batch: json.dumps([c.case_id for c in batch]), parse=driver.parse_batch_response,
                  record=lambda *args: records.append(args), flush=lambda: None, timeout_s=1)
    await minimal.translate_remaining(**kwargs)
    assert len(calls) == 2
    assert json.loads(calls[1]["messages"][1]["content"]) == [first.case_id]
    assert [r[1].case_id for r in records] == [first.case_id, second.case_id]
    assert sum(r[4].total_tokens for r in records) == 160
    # Already cached successes, including all-zero/wrong translations, require no calls.
    await minimal.translate_remaining(**kwargs)
    assert len(calls) == 2


def test_minimal_recovers_valid_cases_from_malformed_neighbor():
    from career_sim_runner.translation_benchmark.minimal import recover_documents
    raw = '{"cases":[{"case_id":"good","options":[]},{"case_id":"bad","options":[L8]}]}'
    assert recover_documents(raw, driver.parse_batch_response) == {"good": {"case_id": "good", "options": []}}


def test_audit_combines_all_versions_in_one_metric_table(tmp_path):
    from career_sim_runner.translation_benchmark.audit import audit_run
    case = _case()
    doc = {"shorttitle": case.shorttitle, "options": [
        {"choice": 1, "metrics": dict.fromkeys("HDSNOWR", "unknown")},
        {"choice": 2, "metrics": dict.fromkeys("HDSNOWR", "unknown")},
    ]}
    payload = driver._result_payload(case, "prompt", json.dumps(doc), TokenUsage(), scoring.score_case(case, doc))
    metadata = {"seed": "fixed", "session_id": "test", "dictionary_sha256": "hash", "case_count": 2,
                "dataset_root": "/dataset", "scoring_mode": "deterministic_structural"}
    (tmp_path / "events.md").write_text(report.render_events(metadata, [payload, payload]))
    (tmp_path / "prompt-cohorts.json").write_text(json.dumps([
        {"start": 1, "prompt_version": "old"}, {"start": 2, "prompt_version": "new"}]))
    audit = audit_run(tmp_path)
    assert list(audit["cohorts"]) == ["all"]
    assert audit["cohorts"]["all"]["H"]["omission"] == 2
    assert (tmp_path / "automated-audit.md").read_text().count("| Metric |") == 1
