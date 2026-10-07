"""Exercise the real translator helper with standard public values and isolated notebooks."""

import json
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys

import pytest


SKILL = Path(__file__).resolve().parents[1] / "solution/skills/observe-decide-review"
SCRIPT = SKILL / "scripts/refresh-context.py"
REFRESH_HELPER = (
    "import pathlib, runpy, sys\n"
    "try:\n"
    "    runpy.run_path(sys.argv[1])['refresh_observation'](pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))\n"
    "except (OSError, ValueError, KeyError, TypeError) as error:\n"
    "    print(f'redline_update_error: {error}', file=sys.stderr)\n"
    "    sys.exit(3)\n"
)
REDLINE_HEADER = "event_key\t当前状态\t触发红线状态（可无或多个）\n"
BASE_STATE = "L=3;O12;S5;N4;H5;D5;W4;R0"


@pytest.fixture
def runtime(tmp_path):
    shutil.copytree(SKILL / "scripts", tmp_path / "scripts")
    notebooks = tmp_path / "notebooks"
    notebooks.mkdir()
    for name in ("redline-guardian-keeps.tsv",):
        (notebooks / name).write_bytes((SKILL / "notebooks" / name).read_bytes())
    observation = tmp_path / "observe.json"
    write_observation(observation)
    return notebooks, observation


def write_observation(path, *, events="", **status):
    values = dict(level="L3", output=10, skill=5, network=4, health=5, dignity=5, wealth=4)
    values.update(status)
    path.write_text(json.dumps({"current_state": {"status": values, "time": {"current_month": 1}},
                                "events": events}), encoding="utf-8")


def test_monthly_hr_feedback_is_written_before_redline_and_deduplicated(runtime):
    notebooks, observation = runtime
    refresh_observation = runpy.run_path(str(SCRIPT))["refresh_observation"]
    data = json.loads(observation.read_text())
    data["current_state"]["time"] = {"current_month": 7}
    data["events"] = "本月基本工资到账\n\n关系好的HR偷偷跟你说，你近期有点风险，不过问题还不大。"
    observation.write_text(json.dumps(data))
    refresh_observation(notebooks, observation)
    risk_file = notebooks / "redline-guardian-keeps.tsv"
    saved = risk_file.read_bytes()
    assert "隐患1" in last_row(notebooks)[1]
    refresh_observation(notebooks, observation)
    assert risk_file.read_bytes() == saved
    data["current_state"]["time"]["current_month"] = 8
    observation.write_text(json.dumps(data))
    refresh_observation(notebooks, observation)
    assert risk_file.read_bytes() == saved
    data["events"] = ""
    observation.write_text(json.dumps(data))
    saved = risk_file.read_bytes()
    refresh_observation(notebooks, observation)
    assert risk_file.read_bytes() == saved


def test_risk_feedback_ignores_event_title_and_rejects_wrong_session(runtime):
    notebooks, observation = runtime
    refresh_observation = runpy.run_path(str(SCRIPT))["refresh_observation"]
    data = json.loads(observation.read_text())
    data["current_event"] = {"title": "HR有点风险"}
    data["current_state"]["time"] = {"current_month": 1}
    observation.write_text(json.dumps(data))
    risk_file = notebooks / "redline-guardian-keeps.tsv"
    saved = risk_file.read_bytes()
    refresh_observation(notebooks, observation)
    assert "隐患0" in last_row(notebooks)[1]
    (notebooks / "workflow-state.json").write_text('{"session_id":"correct"}')
    with pytest.raises(ValueError, match="session mismatch"):
        refresh_observation(notebooks, observation)


def test_failed_review_context_save_preserves_snapshot_and_reports_no_success(runtime, monkeypatch, capsys):
    notebooks, observation = runtime
    helper = runpy.run_path(str(SCRIPT))
    refresh_observation = helper["refresh_observation"]
    refresh_observation(notebooks, observation)
    capsys.readouterr()
    snapshot = notebooks / "review-context.json"
    original = snapshot.read_bytes()
    write_observation(observation, health=6)
    real_replace = helper["os"].replace

    def fail_snapshot_replace(source, target):
        if target == snapshot:
            raise OSError("injected snapshot save failure")
        return real_replace(source, target)

    monkeypatch.setattr(helper["os"], "replace", fail_snapshot_replace)
    with pytest.raises(OSError, match="snapshot save failure"):
        refresh_observation(notebooks, observation)
    assert capsys.readouterr().out == ""
    assert snapshot.read_bytes() == original
    assert not list(notebooks.glob(".notebook-*"))




def pending(notebooks, state=BASE_STATE, *, key="00001", labels="待判定", append=False):
    path = notebooks / "redline-guardian-keeps.tsv"
    prefix = path.read_text(encoding="utf-8") if append else REDLINE_HEADER
    path.write_text(prefix + f"{key}\t{state}\t{labels}\n", encoding="utf-8")


def last_row(notebooks):
    lines = (notebooks / "redline-guardian-keeps.tsv").read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip()][-1].split("\t")


def run_refresh(runtime, *, success=True):
    notebooks, observation = runtime
    result = subprocess.run(
        [sys.executable, '-c', REFRESH_HELPER,
         str(notebooks.parent / "scripts/refresh-context.py"), str(notebooks), str(observation)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == success, result.stderr
    if success:
        assert result.stdout.strip() == "redline_updated"
    else:
        assert "redline_update_error" in result.stderr
    return result


def test_bootstrap_replaces_placeholder_without_cap_notebook_or_adding_a_row(runtime):
    notebooks, observation = runtime
    write_observation(observation, health=2, dignity=8, wealth=1)
    before_lines = (notebooks / "redline-guardian-keeps.tsv").read_bytes().count(b"\n")
    run_refresh(runtime)
    assert last_row(notebooks) == ["00000", "L=3;绩效产出10;专业技能5;人脉4;身心健康2;尊严8;个人财富1;隐患0", "身心健康过低且个人财富过低"]
    assert not (notebooks / "stat-cap.tsv").exists()
    assert (notebooks / "redline-guardian-keeps.tsv").read_bytes().count(b"\n") == before_lines


def test_pending_overestimate_does_not_create_false_caps(runtime):
    notebooks, _ = runtime
    pending(notebooks, "L=L3;O12;S6;N5;H5;D5;W4;R0")
    run_refresh(runtime)
    assert last_row(notebooks) == ["00001", "L=3;绩效产出10;专业技能5;人脉4;身心健康5;尊严5;个人财富4;隐患0", "无"]
    assert not (notebooks / "stat-cap.tsv").exists()


@pytest.mark.parametrize("output", [12, 13, None, "unknown"])
def test_below_cap_or_unknown_observation_has_no_cap_label(runtime, output):
    notebooks, observation = runtime
    pending(notebooks)
    write_observation(observation, output=output)
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "无"


@pytest.mark.parametrize("level", ["L2", "L4"])
def test_level_change_uses_current_level_caps(runtime, level):
    notebooks, observation = runtime
    pending(notebooks)
    cap = runpy.run_path(str(SCRIPT))["STAT_CAPS"][int(level[1:])]
    write_observation(observation, level=level, output=cap["O"], skill=cap["S"], network=cap["N"])
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "绩效产出封顶且专业技能封顶且人脉封顶"


def test_current_level_cap_is_usable_when_revisiting_that_level(runtime):
    notebooks, observation = runtime
    pending(notebooks, BASE_STATE.replace("L=3", "L=4"))
    write_observation(observation, level="L3", output=16, events="职级发生变化")
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "绩效产出封顶"


@pytest.mark.parametrize("events", ["工资到账", "之前的隐患炸了，工作打水漂", "未识别的额外结算通知"])
def test_settlement_does_not_change_fixed_caps(runtime, events):
    notebooks, observation = runtime
    pending(notebooks, "L=3;O12;S8;N5;H5;D5;W4;R0")
    write_observation(observation, events=events, output=16, health=2)
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "身心健康过低且绩效产出封顶"


def test_fixed_cap_survives_repeated_decreases_and_missing_labels(runtime):
    notebooks, observation = runtime
    for index, (value, label) in enumerate(
        [(16, "绩效产出封顶"), (15, "无"), (14, "无"), (13, "无"), (12, "无"), (15, "无"), (16, "绩效产出封顶")],
        start=1,
    ):
        pending(notebooks, key=f"{index:05}", append=True)
        write_observation(observation, output=value)
        run_refresh(runtime)
        assert last_row(notebooks)[2] == label


def test_older_tag_cannot_override_fixed_caps(runtime):
    notebooks, _ = runtime
    pending(notebooks, "L=3;O10;S5;N4;H5;D5;W4;R0", labels="O封顶且S快封顶")
    pending(notebooks, "L=3;O10;S5;N4;H5;D5;W4;R0", key="00002", append=True)
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "无"


def test_already_calibrated_row_cannot_override_fixed_caps(runtime):
    notebooks, _ = runtime
    pending(notebooks, labels="O封顶")
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "无"


def test_observed_overflow_does_not_invalidate_fixed_cap(runtime):
    notebooks, observation = runtime
    pending(notebooks)
    write_observation(observation, output=17)
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "无"
    write_observation(observation, output=16)
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "绩效产出封顶"


def test_l4_resume_overflow_then_real_clamp_reproduces_rows_00112_00113(runtime):
    notebooks, observation = runtime
    pending(notebooks,'L=4;O27;S84;N38;H5;D10;W13;R0',key='00111')
    write_observation(observation,level='L4',output=27,skill=84,network=38,health=5,dignity=10,wealth=13,
                      events='本月工资到账')
    run_refresh(runtime)
    assert last_row(notebooks)[2] == '无'
    pending(notebooks,'L=4;O27;S86;N39;H5;D10;W13;R0',key='00112',append=True)
    write_observation(observation,level='L4',output=26,skill=86,network=36,health=5,dignity=10,wealth=13)
    run_refresh(runtime)
    assert last_row(notebooks) == ['00112','L=4;绩效产出26;专业技能86;人脉36;身心健康5;尊严10;个人财富13;隐患0','绩效产出封顶且人脉封顶']
    pending(notebooks,'L=4;O25;S86;N36;H7;D10;W13;R0',key='00113',append=True)
    write_observation(observation,level='L4',output=25,skill=86,network=36,health=7,dignity=10,wealth=13)
    run_refresh(runtime)
    assert last_row(notebooks) == ['00113','L=4;绩效产出25;专业技能86;人脉36;身心健康7;尊严10;个人财富13;隐患0','人脉封顶']
    # A repeated observation leaves the exact labels unchanged.
    run_refresh(runtime)
    assert last_row(notebooks)[2] == '人脉封顶'
    # A later overestimated action cannot replace the fixed L3 cap.
    pending(notebooks, key="00002", append=True)
    write_observation(observation, output=11)
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "无"


@pytest.mark.parametrize("level", ["L1", "L10"])
@pytest.mark.parametrize("value,label", [(2, "过低"), (3, "过低"), (4, ""), (7, ""), (8, ""), (9, ""), (10, "封顶")])
def test_health_and_dignity_thresholds_do_not_depend_on_level(runtime, level, value, label):
    notebooks, observation = runtime
    write_observation(observation, level=level, health=value, dignity=value)
    run_refresh(runtime)
    expected = f"身心健康{label}且尊严{label}" if label == '过低' else f'身心健康{label}' if label else '无'
    assert last_row(notebooks)[2] == expected


def test_all_low_labels_use_observed_values_and_latest_r(runtime):
    notebooks, observation = runtime
    pending(notebooks, BASE_STATE.replace("R0", "R3"))
    write_observation(observation, health=0, dignity=2, wealth=-1)
    run_refresh(runtime)
    assert last_row(notebooks)[1] == "L=3;绩效产出10;专业技能5;人脉4;身心健康0;尊严2;个人财富-1;隐患3"
    assert last_row(notebooks)[2] == "身心健康过低且尊严过低且个人财富过低且隐患偏高"


def test_unknown_r_is_not_zero_and_r_wealth_never_get_cap_labels(runtime):
    notebooks, observation = runtime
    pending(notebooks, BASE_STATE.replace("R0", "Runknown"), labels="无")
    write_observation(observation, health=None, dignity="?", wealth=100)
    run_refresh(runtime)
    assert last_row(notebooks)[1].endswith("身心健康?;尊严?;个人财富100;隐患?")
    assert last_row(notebooks)[2] == "无"


@pytest.mark.parametrize('output,dignity', [('O12', 'D5'), ('工作产出12', 'Dignity（尊严）5'),
                                          ('绩效产出12', '尊严5')])
def test_dignity_display_round_trips_and_reads_legacy_rows(output, dignity):
    api = runpy.run_path(str(SCRIPT))
    legacy = api['parse_expected'](BASE_STATE.replace('O12', output).replace('D5', dignity))
    expanded = api['format_state'](legacy['L'], {m: legacy[m] for m in 'OSNHDWR'})
    assert '尊严5' in expanded and ';D5;' not in expanded
    assert api['parse_expected'](expanded) == legacy
    assert api['risk_from_state'](expanded) == legacy['R']
    with pytest.raises(ValueError, match='invalid pending review state'):
        api['parse_expected'](expanded + ';D5')


def test_pending_unknown_values_do_not_change_cap_labels(runtime):
    notebooks, _ = runtime
    pending(notebooks, "L=3;O?;Sunknown;N?;H5;D5;W4;R0")
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "无"


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
@pytest.mark.parametrize("trailing", [b"", b"\n\n"])
def test_refresh_preserves_history_eof_permissions_and_other_notebooks(runtime, ending, trailing):
    notebooks, _ = runtime
    path = notebooks / "redline-guardian-keeps.tsv"
    prefix = (REDLINE_HEADER + "00001\t历史原样保存\tO快封顶\n").encode()
    path.write_bytes(prefix + f"00002\t{BASE_STATE}\t待判定".encode() + ending + trailing)
    path.chmod(0o640)
    other = notebooks / "other.tsv"
    other.write_bytes(b"preserve this notebook\n")
    before = path.read_bytes()
    run_refresh(runtime)
    after = path.read_bytes()
    assert after.startswith(prefix) and after.endswith(ending + trailing)
    assert after.count(b"\n") == before.count(b"\n")
    assert path.stat().st_mode & 0o777 == 0o640
    assert other.read_bytes() == b"preserve this notebook\n"
    assert not list(notebooks.glob(".notebook-*"))
    assert not list(notebooks.glob(".replace-tsv-*"))
    # Repeating the exact snapshot is harmless and leaves the table unchanged.
    run_refresh(runtime)
    assert path.read_bytes() == after


@pytest.mark.parametrize(
    "failure",
    [
        "missing_month",
        "missing_metric",
        "bad_number",
        "bad_level",
        "bad_pending",
        "missing_redline",
        "bad_r",
    ],
)
def test_bad_inputs_fail_before_any_notebook_write(runtime, failure):
    notebooks, observation = runtime
    pending(notebooks)
    if failure in ("missing_month", "missing_metric", "bad_number", "bad_level"):
        payload = json.loads(observation.read_text())
        if failure == "missing_month":
            del payload["current_state"]["time"]["current_month"]
        elif failure == "missing_metric":
            del payload["current_state"]["status"]["health"]
        else:
            payload["current_state"]["status"]["health" if failure == "bad_number" else "level"] = True
        observation.write_text(json.dumps(payload))
    elif failure == "bad_pending":
        pending(notebooks, "L=3;O12")
    elif failure == "missing_redline":
        (notebooks / "redline-guardian-keeps.tsv").write_text(REDLINE_HEADER)
    elif failure == "bad_r":
        pending(notebooks, BASE_STATE.replace("R0", "Rbad"))
    before = {path.name: path.read_bytes() for path in notebooks.iterdir()}
    run_refresh(runtime, success=False)
    assert {path.name: path.read_bytes() for path in notebooks.iterdir()} == before


@pytest.mark.parametrize("notice", ["", " \n\t", "工资到账", "结算含引号 ' 与 $(touch should-not-exist)"])
def test_observation_in_relocated_solution_uses_sibling_notebooks(tmp_path, notice):
    """A saved public response works from any cwd with only the solution and stdlib."""
    from career_emulator.server.models import CareerState, ObserveResponse

    relocated = tmp_path / "参赛提交 with spaces" / "skills" / "observe-decide-review"
    shutil.copytree(SKILL, relocated)
    notebooks = relocated / "notebooks"
    pending(notebooks)
    response = ObserveResponse(
        current_state=CareerState(
            level="L3", output=10, skill=5, network=4, health=2, dignity=8, wealth=1
        ).to_visible_dict(),
        current_event=None,
        choices=[],
        warning=notice,
    ).to_mcp_dict()
    assert "observe_json_path" not in response
    observation = tmp_path / "public observation.json"
    observation.write_text(json.dumps(response), encoding="utf-8")
    args = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        REFRESH_HELPER,
        str(relocated / "scripts/refresh-context.py"),
        str(notebooks),
        str(observation),
    ]
    result = subprocess.run(["bash", "-c", shlex.join(args)], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    expected_labels = "身心健康过低且个人财富过低"
    assert last_row(notebooks)[2] == expected_labels
    assert not (tmp_path / "should-not-exist").exists()
    assert json.loads(observation.read_text()) == response


@pytest.mark.parametrize(
    "payload",
    ["{", "[]", "null", "{}", '{"current_state": {}}',
     '{"current_state": {"time": {"current_month": 0}}}',
     '{"current_state": {"time": {"current_month": 1}}, "events": false}'],
)
def test_invalid_observations_never_modify_notebooks(runtime, payload):
    notebooks, observation = runtime
    observation.write_text(payload, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in notebooks.iterdir()}
    result = subprocess.run(
        [sys.executable, '-c', REFRESH_HELPER,
         str(notebooks.parent / "scripts/refresh-context.py"), str(notebooks), str(observation)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert "redline_update_error" in result.stderr
    assert {path.name: path.read_bytes() for path in notebooks.iterdir()} == before


@pytest.mark.parametrize("level", range(1, 11))
def test_fixed_caps_at_every_level_boundary(runtime, level):
    notebooks, observation = runtime
    values = runpy.run_path(str(SCRIPT))["STAT_CAPS"][level]
    cap = dict(zip(("output", "skill", "network"),
                   (values[metric] if values[metric] is not None else 999 for metric in "OSN")))
    for gap in (0, 1):
        write_observation(observation, level=f"L{level}", **{metric: value - gap for metric, value in cap.items()})
        run_refresh(runtime)
        # L10 has no stat caps; other levels label only exact equality.
        expected = "绩效产出封顶且专业技能封顶且人脉封顶" if gap == 0 and level < 10 else "无"
        assert last_row(notebooks)[2] == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("output", -2),
        ("skill", -1),
        ("network", -1),
        ("health", -1),
        ("dignity", 11),
        ("wealth", True),
        ("output", 1.5),
        ("skill", float("nan")),
        ("network", float("inf")),
        ("health", []),
        ("dignity", {}),
    ],
)
def test_invalid_metric_values_leave_all_tables_unchanged(runtime, field, value):
    notebooks, observation = runtime
    pending(notebooks)
    write_observation(observation, **{field: value})
    before = {path.name: path.read_bytes() for path in notebooks.iterdir()}
    run_refresh(runtime, success=False)
    assert {path.name: path.read_bytes() for path in notebooks.iterdir()} == before


def test_failed_redline_write_does_not_report_success_or_destroy_prediction(runtime, monkeypatch, capsys):
    notebooks, observation = runtime
    pending(notebooks)
    original = (notebooks / "redline-guardian-keeps.tsv").read_bytes()
    helper = runpy.run_path(str(SCRIPT))
    status = json.loads(observation.read_text())["current_state"]["status"]

    namespace = helper["refresh"].__globals__
    real_update = namespace["atomic_update"]

    def fail_write(path, original, replacement):
        if path.name == "redline-guardian-keeps.tsv":
            raise ValueError("injected write failure")
        return real_update(path, original, replacement)

    with monkeypatch.context() as patch:
        patch.setitem(namespace, "atomic_update", fail_write)
        with pytest.raises(ValueError, match="injected write failure"):
            helper["refresh"](notebooks, status, "0")
    assert not capsys.readouterr().out
    assert (notebooks / "redline-guardian-keeps.tsv").read_bytes() == original
    assert not (notebooks / "stat-cap.tsv").exists()
    assert not list(notebooks.glob(".notebook-*"))
    # A retry uses the same fixed caps after an interrupted write.
    run_refresh(runtime)
    assert last_row(notebooks)[2] == "无"


@pytest.mark.parametrize("corruption", ["no_write", "history"])
def test_success_requires_exact_notebook_contents(runtime, monkeypatch, capsys, corruption):
    notebooks, observation = runtime
    pending(notebooks, append=True)
    helper = runpy.run_path(str(SCRIPT))
    status = json.loads(observation.read_text())["current_state"]["status"]
    namespace = helper["refresh"].__globals__
    real_update = namespace["atomic_update"]

    def corrupt_write(path, original, replacement):
        if path.name != "redline-guardian-keeps.tsv":
            return real_update(path, original, replacement)
        if corruption == "no_write":
            return
        real_update(path, original, replacement)
        if corruption == "history":
            path.write_bytes(path.read_bytes().replace(b"00000", b"00099", 1))

    monkeypatch.setitem(namespace, "atomic_update", corrupt_write)
    with pytest.raises(ValueError, match="verification failed"):
        helper["refresh"](notebooks, status, "0")
    assert not capsys.readouterr().out


def test_same_month_later_feedback_replaces_unknown_carry_forward(runtime):
    notebooks, observation = runtime
    refresh_observation = runpy.run_path(str(SCRIPT))["refresh_observation"]
    data = json.loads(observation.read_text())
    data["current_state"]["time"] = {"current_month": 1}
    observation.write_text(json.dumps(data))
    refresh_observation(notebooks, observation)
    risk = notebooks / "redline-guardian-keeps.tsv"
    assert len(risk.read_text().splitlines()) == 2
    data["events"] = "HR皱着眉头跟你说，你积攒了不少风险，自己好好处理好。"
    observation.write_text(json.dumps(data))
    refresh_observation(notebooks, observation)
    assert len(risk.read_text().splitlines()) == 2
    assert "隐患2" in last_row(notebooks)[1]
    assert "隐患偏高" in last_row(notebooks)[2]
    saved = risk.read_bytes()
    refresh_observation(notebooks, observation)
    assert risk.read_bytes() == saved


@pytest.mark.parametrize("label", ["旧职级实测封顶", "职级成长上限"])
def test_dictionary_cannot_override_fixed_caps(runtime, label):
    notebooks, observation = runtime
    (notebooks / 'event-translator-dictionary.tsv').write_text(
        f'{label}\tL1:O4/S10/N4；L2:S20/N6\n')
    pending(notebooks, 'L=1;O3;S9;N3;H6;D10;W4;R0', labels='无')
    write_observation(observation, level='L1', output=3, skill=9, network=3, health=6, dignity=10)
    run_refresh(runtime)
    assert last_row(notebooks)[2] == '绩效产出封顶且专业技能封顶且人脉封顶'
