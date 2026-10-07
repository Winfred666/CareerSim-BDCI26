"""Competition promotion ratios depend only on the current level."""
import json
from fractions import Fraction
from pathlib import Path
import runpy
import shutil

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'
HELPERS = runpy.run_path(str(SKILL / 'scripts/refresh-context.py'))
CONTEXT = runpy.run_path(str(SKILL / 'scripts/read-context.py'))


@pytest.mark.parametrize('level,skill,output,network', [
    (1, 8, 3, 3), (2, 18, 5, 6), (3, 35, 14, 18),
    (4, 90, 22, 30), (5, 130, 32, 45), (6, 170, 45, 60),
    (7, 210, 60, 80), (8, 250, 75, 100), (9, 290, 90, 120),
    (10, 290, 90, 120),
])
def test_fixed_table_uses_exact_s_over_o_and_s_over_n(level, skill, output, network):
    target = HELPERS['fixed_promotion_target'](level)
    assert target == (Fraction(skill, output), Fraction(skill, network))
    saved = HELPERS['format_promotion_target'](target, f'固定表，当前L{level}')
    assert HELPERS['promotion_target'](saved) == target
    shown = [('2.67', '2.67'), ('3.60', '3.00'), ('2.50', '1.94'),
             ('4.09', '3.00'), ('4.06', '2.89'), ('3.78', '2.83'),
             ('3.50', '2.63'), ('3.33', '2.50'), ('3.22', '2.42'), ('3.22', '2.42')][level - 1]
    assert f'S:O={shown[0]}，S:N={shown[1]}' in saved


@pytest.mark.parametrize('level', [0, 11, None, True, 'L2'])
def test_invalid_level_is_rejected(level):
    with pytest.raises(ValueError, match='promotion level'):
        HELPERS['fixed_promotion_target'](level)


def setup_runtime(tmp_path, *, level=3, month=18, events=''):
    notebooks = tmp_path / 'notebooks'
    shutil.copytree(SKILL / 'notebooks', notebooks)
    (notebooks / 'workflow-state.json').write_text(json.dumps({'session_id': 'test', 'current_month': month}))
    (notebooks / 'redline-guardian-keeps.tsv').write_text(
        'event_key\t当前状态\t触发红线状态（可无或多个）\n'
        f'00001\tL={level};O10;S20;N10;H5;D5;W4;R0\t待判定\n')
    status = dict(level=f'L{level}', output=10, skill=20, network=10,
                  health=5, dignity=5, wealth=4, energy=3)
    observation = {'current_state': {'session_id': 'test',
                   'time': {'current_month': month}, 'status': status}, 'events': events,
                   'choices': [{'choice': 1, 'action': 'fixed', 'status_updates': {}}]}
    path = tmp_path / 'observation.json'
    path.write_text(json.dumps(observation))
    return notebooks, path


def promotion_rows(notebooks):
    return [line.split('\t') for line in
            (notebooks / 'promotion-signal-researcher-keeps.tsv').read_text().splitlines()]


def test_fixed_caps_do_not_write_or_change_promotion_ratio(tmp_path):
    notebooks, observation = setup_runtime(tmp_path, level=3, month=13)
    data = json.loads(observation.read_text())
    data['current_state']['status'].update(output=16, skill=42, network=21)
    observation.write_text(json.dumps(data))
    before = promotion_rows(notebooks)
    HELPERS['refresh_observation'](notebooks, observation)
    assert promotion_rows(notebooks) == before
    assert all(metric + '封顶' in (notebooks / 'redline-guardian-keeps.tsv').read_text()
               for metric in ('绩效产出', '专业技能', '人脉'))
    assert not (notebooks / 'stat-cap.tsv').exists()
    context = CONTEXT['read_context'](notebooks=notebooks)
    assert context == {'推荐选项': 1, 'notes': 'L3第13月；名义预测无增减；按红线、晋升门槛与净值选择。'}
    HELPERS['refresh_observation'](notebooks, observation)
    assert promotion_rows(notebooks) == before


@pytest.mark.parametrize('events', [
    '【半年谈话】继续积累\n半年绩效奖金（B）',
    '【半年谈话】成果不足\n半年绩效奖金（C）',
])
def test_missed_review_keeps_current_level_ratio(tmp_path, events):
    notebooks, observation = setup_runtime(tmp_path, events=events)
    HELPERS['refresh_observation'](notebooks, observation)
    rows = promotion_rows(notebooks)
    assert len(rows) == 3
    assert HELPERS['promotion_target'](rows[-1][4]) == (Fraction(35, 14), Fraction(35, 18))
    assert '固定表，当前L3' in rows[-1][4]
    HELPERS['refresh_observation'](notebooks, observation)
    assert promotion_rows(notebooks) == rows


def test_existing_forecast_cannot_override_current_level(tmp_path):
    notebooks, observation = setup_runtime(tmp_path, level=2, month=7)
    path = notebooks / 'promotion-signal-researcher-keeps.tsv'
    path.write_text(path.read_text().replace('S:O=2.67，S:N=2.67', 'S:O=99.00，S:N=99.00'))
    HELPERS['refresh_observation'](notebooks, observation)
    context = CONTEXT['read_context'](notebooks=notebooks)
    assert context == {'推荐选项': 1, 'notes': 'L2第7月；名义预测无增减；按红线、晋升门槛与净值选择。'}
