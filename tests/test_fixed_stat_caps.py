"""Fixed cap projections match the installed engine before any cap observations."""

import asyncio
import json
from pathlib import Path
import runpy
import shutil

import pytest


SKILL = Path(__file__).resolve().parents[1] / "solution/skills/observe-decide-review"
API = runpy.run_path(str(SKILL / "scripts/read-context.py"))
REFRESH = API["REDLINE"]
FIELDS = dict(O="output", S="skill", N="network", H="health", D="dignity",
              W="wealth", R="hidden_risk")


@pytest.fixture(scope="module")
def engine_caps():
    from career_emulator.promotion_conditions import PromotionLoader

    config, requirements = asyncio.run(PromotionLoader().load())
    return {
        req.from_level: {stat: int(req.conditions[stat].threshold * config.stat_clamp.clamp_factor)
                         for stat in config.stat_clamp.clamped_stats if stat in req.conditions}
        for req in requirements
    }


@pytest.mark.parametrize("level", range(1, 11))
@pytest.mark.parametrize("offset,change", [(-1, 50), (0, 1), (1, 0), (20, -1),
                                          (0, -500), (1, -2), (-1, 0)])
def test_projection_matches_engine_at_cap_boundaries(engine_caps, monkeypatch, level, offset, change):
    from career_emulator.server import models

    monkeypatch.setattr(models, "get_stat_clamp_requirements", lambda: engine_caps)
    # Start L10 above every L9 cap to detect an accidental inherited limit.
    limits = engine_caps.get(f"L{level}", dict(output=500, skill=500, network=500))
    state = dict(L=level, H=5, D=5, W=5, R=0,
                 **{metric: limits[FIELDS[metric]] + offset for metric in "OSN"})
    delta = dict.fromkeys("OSN", change)
    career = models.CareerState(level=f"L{level}", **{FIELDS[m]: state[m] for m in FIELDS})
    career.apply_delta({FIELDS[m]: value for m, value in delta.items()})
    projected = API["projected_status"](state, REFRESH["fixed_stat_caps"](), delta)
    assert projected == {m: getattr(career, field) for m, field in FIELDS.items()}


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("month", range(1, 7))
def test_first_six_months_use_fixed_caps_without_learning(tmp_path, month, legacy):
    root = tmp_path / "skill"
    shutil.copytree(SKILL, root)
    notebooks = root / "notebooks"
    session = "fixed-cap-test"
    (notebooks / "workflow-state.json").write_text(json.dumps({"session_id": session}))
    cap_path = notebooks / "stat-cap.tsv"
    assert not cap_path.exists()
    if legacy:
        cap_path.write_bytes(b"obsolete cap guesses must be ignored\n")
    observation = {
        "current_state": {"session_id": session, "time": {"current_month": month},
                          "status": dict(level="L1", output=3, skill=9, network=3,
                                         health=5, dignity=5, wealth=5, energy=3,
                                         duration_in_level=month)},
        "current_event": {"title": "季度体力行动分配", "decision_type": "energy_action"},
        "choices": [
            {"choice": 1, "action": "增加已封顶指标", "energy_cost": 1,
             "status_updates": {"Output": 3, "Skill": 3, "Network": 3}},
            {"choice": 2, "action": "恢复健康", "energy_cost": 1,
             "status_updates": {"Health": 1}},
        ],
        "events": "",
    }
    path = tmp_path / "observation.json"
    path.write_text(json.dumps(observation))
    refresh = runpy.run_path(str(root / "scripts/refresh-context.py"))
    context = runpy.run_path(str(root / "scripts/read-context.py"))
    refresh["refresh_observation"](notebooks, path, emit=False)
    assert context["read_context"](notebooks=notebooks)["推荐选项"] == 2
    assert (notebooks / "redline-guardian-keeps.tsv").read_text().splitlines()[-1].endswith(
        "\t绩效产出封顶且专业技能封顶且人脉封顶")
    if legacy:
        assert cap_path.read_bytes() == b"obsolete cap guesses must be ignored\n"
    else:
        assert not cap_path.exists()


def test_fixed_caps_preserve_existing_hint_wording():
    state = dict(L=1, O=3, S=9, N=3, H=5, D=5, W=5, R=0)
    assert API["decision_hints"](state, REFRESH["fixed_stat_caps"]()) == {
        "守红线": "无已触发项",
        "补短板": "当前长板是“专业技能”，比晋升要求多1分，同时“专业技能”已封顶；"
                 "“绩效产出”不多不少恰好满足晋升要求，同时“绩效产出”已封顶；"
                 "“人脉”不多不少恰好满足晋升要求，同时“人脉”已封顶",
    }
