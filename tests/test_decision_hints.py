"""The read context reports exact S/O/N promotion requirements."""
from pathlib import Path
import runpy

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "solution/skills/observe-decide-review/scripts/read-context.py"
_hint = runpy.run_path(str(SCRIPT))["decision_hints"]
_guide = runpy.run_path(str(SCRIPT))["decision_guidance"]


def state(**values):
    return dict(L=3, O=4, S=8, N=3, H=5, D=5, W=5, R=0) | values


def caps(level, **values):
    return {level: (0, dict(O=None, S=None, N=None) | values)}


@pytest.mark.parametrize("level,requirements", [
    (1, (8, 3, 3)), (2, (18, 5, 6)), (3, (35, 14, 18)),
    (4, (90, 22, 30)), (5, (130, 32, 45)), (6, (170, 45, 60)),
    (7, (210, 60, 80)), (8, (250, 75, 100)), (9, (290, 90, 120)),
])
def test_each_level_uses_exact_requirements(level, requirements):
    s, o, n = requirements
    result = _hint(state(L=level, S=s-1, O=o, N=n+2), caps(level))
    assert result["补短板"] == (
        "当前短板是“专业技能”，差1分晋升；“绩效产出”不多不少恰好满足晋升要求；"
        "当前长板是“人脉”，比晋升要求多2分"
    )
    assert set(result) == {"守红线", "补短板"}
    assert not any(word in str(result) for word in ("比例", "保持", "放心增", "排除封顶"))


@pytest.mark.parametrize("relative", [-1, 0, 1])
@pytest.mark.parametrize("level,requirements", [(1, (8, 3, 3)), (2, (18, 5, 6)), (4, (90, 22, 30))])
def test_capped_metric_still_reports_promotion_gap(level, requirements, relative):
    s, o, n = requirements
    current = state(L=level, S=s+relative, O=o+relative, N=n+relative)
    result = _hint(current, caps(level, S=current["S"], O=current["O"], N=current["N"]))["补短板"]
    parts = result.split("；")
    for metric, part in zip(("“专业技能”", "“绩效产出”", "“人脉”"), parts):
        if relative < 0:
            assert part == f"当前短板是{metric}，差1分晋升，同时{metric}已封顶"
        elif relative > 0:
            assert part == f"当前长板是{metric}，比晋升要求多1分，同时{metric}已封顶"
        else:
            assert part == f"{metric}不多不少恰好满足晋升要求，同时{metric}已封顶"
    assert "保持" not in result


def test_no_near_cap_guidance():
    result = _hint(state(L=1, S=8, O=2, N=2, H=9, D=9), caps(1, S=9, O=3, N=3))
    assert "快封顶" not in str(result)
    assert "末期" not in str(result)
    labels_for = runpy.run_path(str(SCRIPT.with_name("refresh-context.py")))["labels_for"]
    assert labels_for(state(L=1, S=8, O=2, N=2, H=9, D=9), caps(1, S=9, O=3, N=3)[1][1], 1) == "无"
    assert labels_for(state(L=1, S=9, O=3, N=3, H=10, D=10), caps(1, S=9, O=3, N=3)[1][1], 1) == "身心健康封顶且绩效产出封顶且专业技能封顶且人脉封顶"


def test_contradicted_cap_is_ignored():
    result = _hint(state(L=4, S=91, O=20, N=30), caps(4, S=90))["补短板"]
    assert "当前长板是“专业技能”，比晋升要求多1分" in result
    assert "当前短板是“绩效产出”，差2分晋升" in result


def test_l10_has_no_next_promotion():
    assert _hint(state(L=10), caps(10))["补短板"] == "当前已是L10，无下一职级晋升要求"


@pytest.mark.parametrize("bad", [dict(S=None), dict(S=True), dict(H=11), dict(N=-1), dict(R=-1), dict(L=0)])
def test_bad_state_is_rejected(bad):
    with pytest.raises(ValueError):
        _hint(state(**bad), caps(bad.get("L", 3) or 3))


def test_low_network_uses_promotion_gap_without_extra_redline_policy():
    result = _hint(state(N=1), caps(3))
    assert result["守红线"] == "无已触发项"
    assert "当前短板是“人脉”，差17分晋升" in result["补短板"]


def option(choice, **delta):
    return {'choice':choice, 'metrics':dict(O=0,N=0,S=0,H=0,W=0,R=0) | delta}


def test_m18_energy_targets_skill_instead_of_larger_output_gain():
    result = _guide(state(L=4,S=43,O=18,N=15,H=9), caps(4), [
        option(1,O=2,H=-1,R=1),option(2,S=1,H=-1),option(3,N=1),option(6,H=2),option(7,O=1)])
    assert result['推荐选项'] == 2
    assert result['补短板'] == _hint(state(L=4,S=43,O=18,N=15,H=9),caps(4))['补短板']


def test_m18_main_targets_skill_instead_of_total_gains():
    result = _guide(state(L=4,S=43,O=20,N=16,H=8,R=1), caps(4), [
        option(1,O=2,S=1,N=1),option(4,S=2,N=1),option(6,S=1,N=2)])
    assert result['推荐选项'] == 4


@pytest.mark.parametrize('current,choices,expected', [
    (dict(H=3),[option(2,S=1,H=-1),option(6,H=2)],6),
    (dict(R=2),[option(1,S=2),option(4,R=-1)],4),
    (dict(D=3),[option(2,S=1),option(5,D=2)],5),
])
def test_redline_relief_precedes_skill(current,choices,expected):
    result = _guide(state(L=4,S=80,O=26,N=36,**current),caps(4),choices)
    assert result['推荐选项'] == expected
    assert result['守红线'] == _hint(state(L=4,S=80,O=26,N=36,**current),caps(4))['守红线']


@pytest.mark.parametrize('metric,value', [(m, v) for m in ('H', 'D') for v in (0, 1, 2, 3)])
def test_low_redline_cannot_be_traded_for_other_gains_even_at_zero(metric, value):
    choices = [option(1, **{metric: -1, 'S': 3, 'W': 3}), option(2)]
    result = _guide(state(**{metric: value}), caps(3), choices)
    assert result['推荐选项'] == 2
    assert result['options'] == choices


@pytest.mark.parametrize('metric', ['H', 'D'])
def test_value_four_is_outside_low_redline(metric):
    result = _guide(state(**{metric: 4}), caps(3), [option(1, **{metric: -1, 'S': 3}), option(2)])
    assert result['推荐选项'] == 1


def test_low_health_is_protected_despite_l1_output_and_network_losses():
    current = state(L=1, H=2, D=10, S=7, O=3, N=3, W=1)
    choices = [option(1, O=1, N=2, H=-1), option(2, O=-1, N=-1)]
    result = _guide(current, caps(1, O=3, N=3, S=9), choices)
    assert result['推荐选项'] == 2


def test_no_recommendation_if_every_option_cuts_a_low_redline():
    choices = [option(1, H=-1, S=3), option(2, D=-1, W=3)]
    current = state(H=3, D=3)
    result = _guide(current, caps(3), choices)
    assert '推荐选项' not in result
    assert result['options'] == choices
    assert '禁止选预测削减' in result['守红线']
    assert '“尊严”过低' in result['守红线']


def test_risk_two_remains_visible_but_safer_option_is_recommended():
    result = _guide(state(L=4,S=80,O=26,N=36),caps(4),[option(2,S=3,R=2),option(1,S=1)])
    assert result['options'] == [option(2,S=3,R=2),option(1,S=1)]
    assert '禁止选项' not in result
    assert result['推荐选项'] == 1
    assert not any(key in result for key in ['当前状态','晋升安排','下次评审月'])


def test_shortfall_hint_limits_no_gain_to_guarded_options_without_hiding_others():
    choices = [option(1,S=2,R=2),option(2,O=2)]
    result = _guide(state(L=3,S=26,O=8,N=9),caps(3),choices)
    assert result['推荐选项'] == 2
    assert result['守红线'] == _hint(state(L=3,S=26,O=8,N=9),caps(3))['守红线']
    assert '当前短板是“专业技能”，差9分晋升' in result['补短板']
    assert result['options'] == choices


def test_risk_threshold_and_fatal_health_rank_lower_without_filtering():
    result = _guide(state(L=4,S=80,O=26,N=36,H=2,R=1),caps(4),[
        option(1,S=3,R=1),option(2,S=3,H=-2),option(3,H=2)])
    assert len(result['options']) == 3
    assert result['推荐选项'] == 3
    assert '禁止选项' not in result
    risky = _guide(state(),caps(3),[option(2,R=2)])
    assert risky['推荐选项'] == 2
    assert risky['守红线'] == _hint(state(),caps(3))['守红线']
    assert risky['options'] == [option(2,R=2)]


def test_filling_last_skill_point_does_not_reward_overflow_or_capped_network():
    result = _guide(state(L=4,S=89,O=26,N=36),caps(4,O=26,S=108,N=36),[
        option(1,S=2,N=3,H=-1),option(2,S=1)])
    assert result['推荐选项'] == 2


def test_no_skill_gain_falls_back_to_other_shortfall_and_known_cap():
    result = _guide(state(L=4,S=80,O=26,N=29),caps(4,O=26),[option(1,O=3),option(2,N=1)])
    assert result['推荐选项'] == 2


def test_all_requirements_met_preserves_them_before_upkeep():
    result = _guide(state(L=4,S=90,O=22,N=30),caps(4),[option(1,S=-1,H=3),option(2,H=1)])
    assert result['推荐选项'] == 2
    assert _guide(state(L=10),caps(10),[option(1),option(2,H=1)])['推荐选项'] == 2


def test_met_requirements_keep_free_uncapped_skill_instead_of_capped_output():
    current = state(L=2, S=19, O=6, N=6, H=10)
    limits = caps(2, O=6, S=21, N=7)
    choices = [option(1,O=2,S=1,N=1),option(4,S=2,N=1)]
    assert _guide(current,limits,choices)['推荐选项'] == 4
    assert _guide(current,limits,[option(1,S=1),option(4,S=2,H=-1)])['推荐选项'] == 1
    assert _guide(state(L=2,S=21,O=6,N=6,H=10),limits,
                  [option(1,N=1),option(4,S=2)])['推荐选项'] == 1


def test_equal_shortfall_relief_and_cost_keep_uncapped_network_reserve():
    current = state(L=2,S=20,O=6,N=5,H=10)
    limits = caps(2,O=6,S=21,N=7)
    result = _guide(current,limits,[option(1,O=2,S=1,N=1),option(6,S=1,N=2)])
    assert result['推荐选项'] == 6
    assert result['补短板'] == _hint(current,limits)['补短板']


@pytest.mark.asyncio
async def test_fixed_requirements_match_installed_official_promotion_rules():
    from career_emulator.promotion_conditions import PromotionLoader
    _, requirements = await PromotionLoader().load()
    fixed = runpy.run_path(str(SCRIPT))['PROMOTION_REQUIREMENTS']
    fields = dict(S='skill',O='output',N='network')
    assert fixed == {int(req.from_level[1:]): {m:req.conditions[f].threshold for m,f in fields.items()}
                     for req in requirements}
