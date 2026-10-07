import json
from fractions import Fraction
from pathlib import Path
import runpy
import shutil

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'
record = runpy.run_path(str(SKILL / 'scripts/record-review.py'))['record']
refresh = runpy.run_path(str(SKILL / 'scripts/refresh-context.py'))['refresh']
record_promotion = record.__globals__['refresh'].record_promotion


@pytest.fixture
def runtime(tmp_path):
    notes = tmp_path / 'notebooks'
    shutil.copytree(SKILL / 'notebooks', notes)
    (notes / 'workflow-state.json').write_text('{"session_id":"game"}')
    status = dict(level='L2', output=6, skill=20, network=6, health=7, dignity=10, wealth=5)
    obs = notes / 'review-context.json'
    obs.write_text(json.dumps({'event_key': '00001',
                              'current_state': {'session_id': 'game', 'status': status}}))
    return notes, obs, status


def test_public_log_review_uses_fixed_caps_and_is_idempotent(runtime):
    notes, obs, status = runtime
    (notes / 'event-translator-dictionary.tsv').write_text('旧职级实测封顶\tL2:S21/N7\n')
    assert record(notes, {'Output': 1, 'Dignity': 1}) == 'review_recorded 00001'
    path = notes / 'redline-guardian-keeps.tsv'
    saved = path.read_bytes()
    record(notes, {'Output': 1, 'Dignity': 1})
    assert path.read_bytes() == saved
    assert '绩效产出7;' in saved.decode() and '尊严10;' in saved.decode()
    refresh(notes, status, '0')
    assert not (notes / 'stat-cap.tsv').exists()
    assert '绩效产出封顶' in path.read_text()


@pytest.mark.parametrize('updates', [{'R': 1}, {'Output': True}, {'Output': 1, 'O': 2}, {'Other': 2}])
def test_invalid_log_delta_does_not_write(runtime, updates):
    notes, obs, _ = runtime
    path = notes / 'redline-guardian-keeps.tsv'
    before = path.read_bytes()
    with pytest.raises(ValueError):
        record(notes, updates)
    assert path.read_bytes() == before


def test_missing_saved_observation_does_not_write_notebooks(runtime):
    notes, obs, _ = runtime
    obs.unlink()
    before = {path.name: path.read_bytes() for path in notes.iterdir() if path.is_file()}
    with pytest.raises(FileNotFoundError, match='review-context.json'):
        record(notes, {})
    assert {path.name: path.read_bytes() for path in notes.iterdir() if path.is_file()} == before


def test_wrong_session_and_nonsequential_event_are_rejected(runtime):
    notes, obs, _ = runtime
    redline = notes / 'redline-guardian-keeps.tsv'
    original = redline.read_text()
    redline.write_text(original.replace('00000', '00002'))
    with pytest.raises(ValueError, match='next event'):
        record(notes, {})
    redline.write_text(original)
    (notes / 'workflow-state.json').write_text('{"session_id":"another-game"}')
    with pytest.raises(ValueError, match='session mismatch'):
        record(notes, {})


def risk_fixture(runtime):
    notes, obs, status = runtime
    data = json.loads(obs.read_text())
    data['current_state']['time'] = {'current_month': 39}
    data['current_event'] = {'title': '远程办公发现同事长期不在线',
        'description': '同事坦白确实在照顾家人，并透露已经在考虑离职。他的离职会让团队关键项目出现人力缺口。'}
    data['choices'] = [{'choice': 1}, {'choice': 2}]
    obs.write_text(json.dumps(data))
    redline = notes / 'redline-guardian-keeps.tsv'
    redline.write_text(redline.read_text().replace('R0>', 'R1>'))
    return notes, obs, status


def test_review_carries_r_unchanged_even_for_former_special_case(runtime):
    notes, obs, status = risk_fixture(runtime)
    record(notes, {'Skill': 1}, 1)
    risk = (notes / 'redline-guardian-keeps.tsv').read_bytes()
    assert '隐患1\t待判定' in risk.decode()
    record(notes, {'Skill': 1}, 1)
    assert (notes / 'redline-guardian-keeps.tsv').read_bytes() == risk
    refresh(notes, status, '0')
    assert '隐患1' in (notes / 'redline-guardian-keeps.tsv').read_text()


def test_unverified_choice_does_not_change_r(runtime):
    notes, obs, _ = risk_fixture(runtime)
    record(notes, {'Skill': 1}, 2)
    assert '隐患1\t待判定' in (notes / 'redline-guardian-keeps.tsv').read_text()


def test_invalid_review_does_not_write_notebooks(runtime):
    notes, obs, _ = risk_fixture(runtime)
    data = json.loads(obs.read_text())
    data['event_key'] = '00003'
    obs.write_text(json.dumps(data))
    before = {p.name: p.read_bytes() for p in notes.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match='next event'):
        record(notes, {}, 1)
    with pytest.raises(ValueError, match='observed menu'):
        record(notes, {}, 4)
    assert {p.name: p.read_bytes() for p in notes.iterdir() if p.is_file()} == before


def test_partial_save_retry_preserves_r(runtime, monkeypatch):
    notes, obs, _ = risk_fixture(runtime)
    risk_path = notes / 'redline-guardian-keeps.tsv'
    risk_path.write_text(risk_path.read_text().replace('R1>', 'R2>'))
    namespace = record.__globals__['refresh']
    original = namespace.atomic_update
    def fail_redline(path, old, new):
        if path.name == 'redline-guardian-keeps.tsv':
            raise OSError('injected redline failure')
        original(path, old, new)
    monkeypatch.setattr(namespace, 'atomic_update', fail_redline)
    with pytest.raises(OSError):
        record(notes, {'Skill': 1}, 1)
    saved = risk_path.read_bytes()
    assert 'R2>' in saved.decode()
    monkeypatch.setattr(namespace, 'atomic_update', original)
    record(notes, {'Skill': 1}, 1)
    assert '隐患2\t待判定' in risk_path.read_text()


def test_same_title_different_node_does_not_infer_r(runtime):
    notes, obs, _ = risk_fixture(runtime)
    data = json.loads(obs.read_text())
    data['current_event']['description'] = '另一个未知节点'
    obs.write_text(json.dumps(data))
    record(notes, {}, 1)
    assert '隐患1\t待判定' in (notes / 'redline-guardian-keeps.tsv').read_text()


@pytest.mark.parametrize('month,events', [
    (23, 'HR：不错，继续保持'),
    (18, '【半年谈话】\n例行会谈\n\nLeader 照本宣科：「说实话，一开始亮点不算突出，整体达标。」\n半年绩效奖金（B）\n晋升通知：L3→L4\nHR：不错，继续保持'),
])
def test_review_does_not_consume_feedback_or_change_other_notebooks(runtime, month, events):
    notes, obs, _ = risk_fixture(runtime)
    data = json.loads(obs.read_text())
    data['current_state']['time'] = {'current_month': month}
    data['events'] = events
    obs.write_text(json.dumps(data))
    others = {p.name: p.read_bytes() for p in notes.iterdir()
              if p.is_file() and p.name != 'redline-guardian-keeps.tsv'}
    record(notes, {})
    assert '隐患1\t待判定' in (notes / 'redline-guardian-keeps.tsv').read_text()
    assert {name: (notes / name).read_bytes() for name in others} == others
    saved = {p.name: p.read_bytes() for p in notes.iterdir() if p.is_file()}
    record(notes, {})
    assert {p.name: p.read_bytes() for p in notes.iterdir() if p.is_file()} == saved


def test_feedback_retry_does_not_reinterpret_r(runtime):
    notes, obs, _ = risk_fixture(runtime)
    data = json.loads(obs.read_text())
    data['events'] = 'HR：有点风险'
    obs.write_text(json.dumps(data))
    record(notes, {}, 1)
    saved = (notes / 'redline-guardian-keeps.tsv').read_bytes()
    assert '隐患1\t待判定' in saved.decode()
    record(notes, {}, 1)
    assert (notes / 'redline-guardian-keeps.tsv').read_bytes() == saved


def test_feedback_save_failure_is_retryable(runtime, monkeypatch):
    notes, obs, _ = runtime
    data = json.loads(obs.read_text())
    data['current_state']['time'] = {'current_month': 18}
    data['events'] = '半年绩效奖金（B）\n晋升通知：L3→L4\nHR：不错'
    obs.write_text(json.dumps(data))
    namespace = record.__globals__['refresh']
    original = namespace.atomic_update
    def fail_promotion(path, old, new):
        if path.name == 'promotion-signal-researcher-keeps.tsv':
            raise OSError('injected promotion failure')
        original(path, old, new)
    before = (notes / 'redline-guardian-keeps.tsv').read_bytes()
    monkeypatch.setattr(namespace, 'atomic_update', fail_promotion)
    with pytest.raises(OSError, match='promotion failure'):
        namespace.record_feedback(notes, data)
    assert (notes / 'redline-guardian-keeps.tsv').read_bytes() == before
    monkeypatch.setattr(namespace, 'atomic_update', original)
    assert namespace.record_feedback(notes, data) == ('0', '高')
    assert (notes / 'promotion-signal-researcher-keeps.tsv').read_text().count('第18月\t') == 1


def test_promotion_update_discards_retired_column(runtime):
    notes, obs, _ = runtime
    path = notes / 'promotion-signal-researcher-keeps.tsv'
    path.write_text('时间\t状态(O;S;N;R)\t下个晋升点\t距晋升点（月）\t反馈结果\t晋升比例推算\tshortfall\n'
                    '第6月\tO3;S9;N3;R0\t12\t6\tC；晋升通知：L1→L2\t下次晋升比例推算：S:O=3.00，S:N=3.00\t至少S+2\n')
    data = json.loads(obs.read_text())
    data['current_state']['time'] = {'current_month': 18}
    data['events'] = '半年绩效奖金（B）'
    obs.write_text(json.dumps(data))
    record.__globals__['refresh'].record_feedback(notes, data)
    saved = path.read_bytes()
    assert all(len(line.split('\t')) == 5 for line in saved.decode().splitlines())
    assert '距晋升点' not in saved.decode()
    assert 'shortfall' not in saved.decode() and '至少专业技能+2' not in saved.decode()
    assert saved.decode().splitlines()[0].endswith('晋升比例推算')
    assert '第6月' in saved.decode() and '第18月' in saved.decode()
    record.__globals__['refresh'].record_feedback(notes, data)
    assert path.read_bytes() == saved


def promotion_observation(month, level, output, skill, network, events):
    return {
        'events': events,
        'current_state': {
            'time': {'current_month': month},
            'status': {'level': level, 'output': output, 'skill': skill, 'network': network},
        },
    }


def pending_promotion(notes, level, output, skill, network):
    (notes / 'redline-guardian-keeps.tsv').write_text(
        'event_key\tstate\tlabels\n'
        f'00001\tL={level};O{output};S{skill};N{network};H8;D10;W5;R0\t待判定\n')


def test_promotion_ratio_uses_pre_penalty_state_and_is_idempotent(runtime):
    notes, _, _ = runtime
    pending_promotion(notes, 1, 3, 9, 3)
    record_promotion(notes, promotion_observation(
        6, 'L2', 3, 9, 3,
        '晋升通知：半年组织评审通过，您已从 L1 晋升至 L2（工程师）。'), '0')
    pending_promotion(notes, 2, 6, 22, 9)
    observation = promotion_observation(
        12, 'L3', 6, 21, 4,
        '晋升通知：半年组织评审通过，您已从 L2 晋升至 L3（高级工程师）。\n升迁节奏过快，原有人脉圈还没消化你的新身份')
    record_promotion(notes, observation, '0')
    path = notes / 'promotion-signal-researcher-keeps.tsv'
    row = path.read_text().splitlines()[-1].split('\t')
    assert row[1] == 'O6;S21;N7;R0'
    assert '评审前日志推算' in row[3]
    # The new level selects the fixed competition ratio.
    expected = (Fraction(35, 14), Fraction(35, 18))
    assert record.__globals__['refresh'].promotion_target(row[4]) == expected
    assert '固定表，当前L3' in row[4]
    saved = path.read_bytes()
    pending_promotion(notes, 3, 7, 22, 5)
    record_promotion(notes, observation, '0')
    assert path.read_bytes() == saved
    record_promotion(notes, promotion_observation(
        18, 'L3', 9, 30, 9, '【半年谈话】继续积累'), '0')
    inference = path.read_text().splitlines()[-1].split('\t')[-1]
    assert record.__globals__['refresh'].promotion_target(inference) == expected


def test_missing_pre_review_state_never_uses_post_penalty_status(runtime):
    notes, _, _ = runtime
    record_promotion(notes, promotion_observation(
        12, 'L3', 6, 21, 4, '晋升通知：L2→L3\n升迁节奏过快'), '0')
    row = (notes / 'promotion-signal-researcher-keeps.tsv').read_text().splitlines()[-1].split('\t')
    assert row[1] == 'O?;S?;N?;R0'
    assert '评审前状态未知' in row[3]


@pytest.mark.parametrize('pending', [False, True])
def test_no_promotion_records_observed_metrics_not_pending_estimate(runtime, pending):
    notes, _, _ = runtime
    if pending:
        pending_promotion(notes, 1, 99, 99, 99)
    observation = promotion_observation(
        18, 'L1', 2, 9, 2, '【半年谈话】整体达标\n半年绩效奖金（C）')
    record_promotion(notes, observation, '0')
    path = notes / 'promotion-signal-researcher-keeps.tsv'
    row = path.read_text().splitlines()[-1].split('\t')
    assert row[1] == 'O2;S9;N2;R0'
    assert row[2] == '24'
    assert row[3] == 'C；未晋升，记录当前观察状态'
    assert record.__globals__['refresh'].promotion_target(row[4]) == (Fraction(8, 3), Fraction(8, 3))
    saved = path.read_bytes()
    record_promotion(notes, observation, '0')
    assert path.read_bytes() == saved


def test_nonpromotion_observation_repairs_legacy_unknown_row(runtime):
    notes, _, _ = runtime
    path = notes / 'promotion-signal-researcher-keeps.tsv'
    with path.open('a') as f:
        f.write('第18月\tO?;S?;N?;R0\t24\tC；评审前状态未知（缺少跨月行动复核，不用扣分后状态替代）'
                '\t下次晋升比例推算：暂无可核验基准；理由：尚无成功晋升前比例\n')
    record_promotion(notes, promotion_observation(
        18, 'L1', 2, 9, 2, '半年绩效奖金（C）'), '0')
    assert path.read_text().count('第18月\t') == 1
    assert 'O?;S?;N?' not in path.read_text()
    assert 'O2;S9;N2;R0' in path.read_text()


def test_successful_promotion_unknown_row_can_recover_verified_pending_state(runtime):
    notes, _, _ = runtime
    observation = promotion_observation(12, 'L3', 6, 21, 4, '晋升通知：L2→L3')
    record_promotion(notes, observation, '0')
    pending_promotion(notes, 2, 6, 21, 7)
    record_promotion(notes, observation, '0')
    text = (notes / 'promotion-signal-researcher-keeps.tsv').read_text()
    assert text.count('第12月\t') == 1
    assert 'O6;S21;N7;R0' in text
    assert '评审前状态未知' not in text


def test_initial_target_uses_fixed_table_even_if_notebook_is_stale(runtime):
    notes, _, _ = runtime
    helpers = record.__globals__['refresh']
    path = notes / 'promotion-signal-researcher-keeps.tsv'
    assert helpers.fixed_promotion_target(1) == (Fraction(8, 3), Fraction(8, 3))
    assert '固定比赛晋升表' in path.read_text()
    assert helpers.fixed_promotion_target(10) == helpers.fixed_promotion_target(9)


def test_feedback_uses_explicit_labels_without_dictionary(runtime):
    notes, _, _ = runtime
    infer = record.__globals__['refresh'].feedback_shortfalls
    (notes / 'event-translator-dictionary.tsv').unlink(missing_ok=True)
    assert infer('【半年谈话】整体达标，继续保持，有瓶颈早点说', notes) == []
    assert infer('成果不足，协作不足', notes) == []
    assert infer('【半年谈话】成果不足，协作不足', notes) == ['O', 'N']
    assert infer('【半年谈话】技能不足', notes) == ['S']
    assert infer('【半年谈话】整体达标\n半年绩效奖金（B）\n成果不足', notes) == []


def test_review_does_not_apply_public_menu_risk_effect(runtime):
    notes, obs, _ = risk_fixture(runtime)
    data = json.loads(obs.read_text())
    data['current_event'] = {'title': '季度体力行动分配'}
    data['choices'] = [{'choice': 4, 'status_updates': {'HiddenRisk': -1}}]
    obs.write_text(json.dumps(data))
    record(notes, {}, 4)
    saved = (notes / 'redline-guardian-keeps.tsv').read_bytes()
    assert '隐患1\t待判定' in saved.decode()
    record(notes, {}, 4)
    assert (notes / 'redline-guardian-keeps.tsv').read_bytes() == saved
