"""Exercise persisted questions, numeric decisions and file completion barriers."""
import json
import re
from pathlib import Path
import runpy
import shutil

import pytest
from tests.review_logs import write_action_log

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


def correction_event(packet):
    """Correction exposes public facts and state constraints, no numeric preference."""
    constraints = packet['constraints']
    assert set(constraints) == {'守红线', '晋升条件'}
    assert '优先选' not in str(constraints) and '预测' not in str(constraints)
    assert set(packet) <= {'current_event', 'event_history', 'options', 'constraints'}
    return {key: value for key, value in packet.items() if key != 'constraints'}


def assert_recommendation(packet, choice):
    assert set(packet) == {'推荐选项', 'notes'}
    assert packet['推荐选项'] == choice
    assert isinstance(packet['notes'], str) and packet['notes']


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    notes = root / 'notebooks'
    (notes / 'workflow-state.json').write_text('{"session_id":"game"}')
    observation = {'current_state': {'session_id': 'game', 'time': {'current_month': 1},
                   'status': dict(level='L1', output=2, skill=6, network=3, health=5, dignity=5, wealth=4, energy=3)},
                   'current_event': {'title': 'PRIVATE TITLE', 'description': 'PRIVATE STORY'},
                   'choices': [{'choice': 1, 'action': 'PRIVATE ACTION', 'description': 'PRIVATE DETAIL'}], 'events': ''}
    path = tmp_path / 'observation.json'
    path.write_text(json.dumps(observation))
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    refresh = runpy.run_path(str(root / 'scripts/refresh-context.py'))
    context = runpy.run_path(str(root / 'scripts/read-context.py'))
    return root, notes, observation, path, state, refresh, context


def complete(state, metric, notes):
    result = state['answer'](metric, 'ready', notes)
    lane = state['event_dir'](notes, state['current_task'](notes)) / metric
    for _ in range(80):
        if result['complete']:
            return result
        ids = [str(c['choice']) for c in state['current_task'](notes)['choices']]
        if metric == 'O':
            answer = '1' if '本选项实际作用于哪类事务' in result['question'] else '0'
        elif metric == 'N':
            answer = json.dumps({'joint': False} if 'joint' in result['answer_template'] else {k: {'N': 0} for k in ids})
        elif metric == 'HW':
            previous = state['read'](lane / '.answers.json') if (lane / '.answers.json').exists() else {}
            if '_route' not in previous:
                answer = json.dumps({'existing_H_load': False, 'physical_risk': False})
            elif '_joint' not in previous:
                answer = json.dumps({key: {'H': 0, 'W': 0} for key in ids})
            else:
                answer = json.dumps({key: False for key in ids})
        else:
            answer = json.dumps({k: 0 if metric in ('N', 'R') else {m: 0 for m in metric} for k in ids})
        result = state['answer'](metric, answer, notes)
    raise AssertionError('questionnaire did not terminate')


def test_ready_has_public_event_and_first_question_without_advancing(runtime):
    _, notes, observation, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        first = state['answer'](metric, 'ready', notes)
        assert first['event']['current_event'] == observation['current_event']
        assert 'choices' not in first['event']
        assert first['question'].count('PRIVATE ACTION') >= 1
        assert first['question'].count('PRIVATE DETAIL') >= 1
        assert first['question'] and not first['complete']
        assert first == state['answer'](metric, 'ready', notes)
    assert '1.' in state['answer']('O', 'ready', notes)['question']
    assert '当前各选项' in state['answer']('S', 'ready', notes)['question']


def test_o_batches_only_the_current_branch_and_rejects_partial_answers(runtime):
    _, notes, observation, path, state, refresh, _ = runtime
    observation['choices'] = [{'choice': i, 'action': f'OPTION {i}'} for i in (1, 2, 7)]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    answer = lambda text: state['answer']('O', text, notes)
    assert answer('ready')['answer_template'] == 'JSON {"1":<编号>,"2":<编号>,"7":<编号>}'
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'O'
    assert '答案格式错误' in answer('{"1":0}')['question']
    assert '答案格式错误' in answer('{"1":false,"2":1,"7":0}')['question']
    assert not (lane / '.answers.json').exists()
    result = answer('{"1":1,"2":2,"7":1}')
    assert result['answer_template'] == 'JSON {"1":<编号>,"7":<编号>}'
    assert 'OPTION 2' not in result['question']
    assert not answer('{"1":1,"7":0}')['complete']
    assert not answer('2')['complete']
    assert not answer('1')['complete']
    assert answer('3')['complete']
    assert state['read'](lane / 'complete.json')['values'] == {'1': {'O': 2}, '2': {'O': -3}, '7': {'O': 0}}


def test_o_zero_options_finish_in_two_batch_submissions(runtime):
    _, notes, observation, path, state, refresh, _ = runtime
    observation['choices'] = [{'choice': i, 'action': f'OPTION {i}'} for i in (1, 2, 3)]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    state['answer']('O', 'ready', notes)
    assert not state['answer']('O', '{"1":1,"2":1,"3":1}', notes)['complete']
    assert state['answer']('O', '{"1":0,"2":0,"3":0}', notes)['complete']


def test_r_answer_template_uses_only_pending_nonconsecutive_option_ids(runtime):
    _, notes, observation, path, state, refresh, _ = runtime
    observation['choices'] = [{**observation['choices'][0], 'choice': i} for i in (2, 7)]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    first = state['answer']('R', 'ready', notes)
    assert first['answer_template'] == 'JSON {"2":<整数>,"7":<整数>}'
    following = state['answer']('R', '{"2":2,"7":0}', notes)
    assert following['answer_template'] == 'JSON {"7":<整数>}'
    finished = state['answer']('R', '{"7":-2}', notes)
    assert finished['complete'] and 'answer_template' not in finished


@pytest.mark.parametrize('metric,answers', [
    ('O', ['1', '1', '1']),
    ('R', ['{"1":0}', '{"1":0}', '{"1":1}']),
    ('HW', ['{"existing_H_load":false,"physical_risk":false}',
            '{"1":{"H":-1,"W":0}}', '{"1":true}']),
])
def test_ready_resumes_each_stage_and_keeps_completed_event_isolated(runtime, metric, answers):
    _, notes, observation, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    lane = state['event_dir'](notes, state['current_task'](notes)) / metric
    first = state['answer'](metric, 'ready', notes)
    for answer in answers:
        result = state['answer'](metric, answer, notes)
        before = {p.name: p.read_bytes() for p in lane.iterdir() if p.is_file()}
        resumed = state['answer'](metric, 'ready', notes)
        assert resumed['complete'] == result['complete']
        assert resumed['question'] == result['question']
        assert resumed.get('answer_template') == result.get('answer_template')
        assert before == {p.name: p.read_bytes() for p in lane.iterdir() if p.is_file()}
    assert result['complete']
    assert (lane / '.answer-trace.json').exists() == (metric != 'R')

    state['prepare'](notes, observation, '00002')
    fresh = state['answer'](metric, 'ready', notes)
    assert not fresh['complete'] and fresh['question'] == first['question']
    new_lane = state['event_dir'](notes, state['current_task'](notes)) / metric
    assert not (new_lane / '.answers.json').exists()
    assert not (new_lane / 'complete.json').exists()
    assert before == {p.name: p.read_bytes() for p in lane.iterdir() if p.is_file()}


def test_ready_preserves_n_without_resetting_other_roles_or_event(runtime):
    _, notes, _, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    directory = state['event_dir'](notes, state['current_task'](notes))
    complete(state, 'S', notes)
    preserved = {p: p.read_bytes() for p in (
        directory / 'S/complete.json', directory / 'N/.event.json',
        directory / 'N/.policy.json', directory / 'task.json')}
    first = state['answer']('N', 'ready', notes)
    incomplete = state['answer']('N', '{}', notes)
    assert not incomplete['complete']
    assert not (directory / 'N/.answers.json').exists()
    assert state['answer']('N', 'ready', notes) == first
    assert not (directory / 'N/.answers.json').exists()
    complete(state, 'N', notes)
    assert (directory / 'N/complete.json').exists()
    assert state['answer']('N', 'ready', notes)['complete']
    assert (directory / 'N/complete.json').exists()
    assert preserved == {p: p.read_bytes() for p in preserved}


def test_decide_requires_all_metrics_and_exposes_semantics_only_on_deadlock(runtime):
    _, notes, observation, path, state, refresh, context = runtime
    observation['event_history'] = {'steps': [{
        'description': '上轮原题', 'current_month': 1,
        'selected_choice': {'action': '当时选择', 'description': '原始选项说明'},
    }]}
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in 'ON':
        complete(state, metric, notes)
    with pytest.raises(ValueError, match='incomplete: S'):
        context['read_context'](notebooks=notes)
    for metric in ('S', 'HW', 'R'):
        complete(state, metric, notes)
    result = context['read_context']('00001', notebooks=notes)
    assert correction_event(result) == {
        'current_event': observation['current_event'],
        'event_history': observation['event_history'],
        'options': observation['choices'],
    }
    with pytest.raises(ValueError, match='stale event_id'):
        context['read_context']('00002', notebooks=notes)


def test_positive_translation_returns_one_choice_and_ready_to_use_notes(runtime):
    _, notes, _, path, state, refresh, context = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'S/complete.json'
    receipt = state['read'](lane)
    receipt['values']['1']['S'] = 1
    state['save'](lane, receipt)
    result = context['read_context'](notebooks=notes)
    assert_recommendation(result, 1)
    assert result['notes'] == 'L1第1月；名义预测专业技能+1；按红线、晋升门槛与净值选择。'
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('alternative_error', [-2, 0])
def test_uncertainty_returns_bare_event_or_a_reliable_alternative(runtime, monkeypatch, alternative_error):
    _, notes, observation, path, state, refresh, context = runtime
    observation['choices'] = [{'choice': 1, 'action': '增技能方案'}, {'choice': 2, 'action': '协作方案'}]
    observation['event_history'] = {'steps': [{'description': '公开前情'}]}
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    for group in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, group, notes)
    directory = state['event_dir'](notes, state['current_task'](notes))
    for group, choice in [('S', '1'), ('N', '2')]:
        file = directory/group/'complete.json'
        receipt = state['read'](file)
        receipt['values'][choice][group] = 1
        state['save'](file, receipt)
    metrics = {m: {str(p): {'0': 1.} for p in range(-3, 4)} for m in 'ONSHWR'}
    metrics['S']['1'] = {'-2': 1.}
    metrics['N']['1'] = {str(alternative_error): 1.}
    monkeypatch.setitem(context['POLICY']['ERRORS']['MODEL'], 'metrics', metrics)
    packet = context['read_context'](notebooks=notes)
    if alternative_error == 0:
        # A reliable capped raw gain is preferable to the known S loss.
        assert_recommendation(packet, 2)
    else:
        assert correction_event(packet) == {'current_event': observation['current_event'],
                          'event_history': observation['event_history'], 'options': observation['choices']}


def test_semantic_correction_returns_all_options_with_current_state_constraints(runtime):
    _, notes, observation, path, state, refresh, context = runtime
    observation['current_state']['status'].update(output=3, skill=7, network=3, health=2, dignity=3, wealth=4)
    observation['choices'] = [{'choice': 1, 'action': '接下连班'}, {'choice': 2, 'action': '婉拒换班'}]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    deltas = {'O': {1: {'O': 1}, 2: {'O': -1}},
              'N': {1: {'N': 2}, 2: {'N': -1}},
              'HW': {1: {'H': -1, 'W': 0}, 2: {'H': 0, 'W': 0}}}
    directory = state['event_dir'](notes, state['current_task'](notes))
    for metric, values in deltas.items():
        receipt_path = directory / metric / 'complete.json'
        receipt = state['read'](receipt_path)
        receipt['values'] = {str(key): value for key, value in values.items()}
        state['save'](receipt_path, receipt)
    result = context['read_context'](notebooks=notes)
    assert correction_event(result) == {
        'current_event': observation['current_event'],
        'options': observation['choices'],
    }
    assert '“身心健康”过低' in result['constraints']['守红线']
    assert '“尊严”过低' in result['constraints']['守红线']
    assert '当前短板是“专业技能”，差1分晋升' in result['constraints']['晋升条件']


def test_correction_packet_is_independent_of_current_translation_preferences(runtime, monkeypatch):
    _, notes, observation, path, state, refresh, context = runtime
    observation['current_state']['status'].update(health=3, dignity=3, wealth=2)
    observation['choices'] = [{'choice': 1, 'action': '方案甲'}, {'choice': 2, 'action': '方案乙'}]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    chosen = []
    def force_correction(current, caps, options, decision, choice):
        chosen.append(choice)
        return {'reason': '强制低可信测试', 'confidence': 0}
    monkeypatch.setitem(context['read_context'].__globals__, 'decision_confidence', force_correction)
    receipt_path = state['event_dir'](notes, state['current_task'](notes)) / 'S/complete.json'
    receipt = state['read'](receipt_path)
    packets = []
    for preferred in ('1', '2'):
        receipt['values'] = {str(i): {'S': 3 if str(i)==preferred else 0} for i in (1,2)}
        state['save'](receipt_path, receipt)
        packets.append(context['read_context'](notebooks=notes))
    # Even an all-forbidden translation and opposite R forecasts cannot leak
    # into the correction constraints, which depend on the current state only.
    directory = receipt_path.parent.parent
    deltas = {1: dict(O=3,N=-3,S=-3,H=-1,W=-1,R=3),
              2: dict(O=-3,N=3,S=3,H=-1,W=3,R=-3)}
    for group in ('O','N','S','HW','R'):
        file = directory/group/'complete.json'
        saved = state['read'](file)
        saved['values'] = {str(i): {m:deltas[i][m] for m in group} for i in (1,2)}
        state['save'](file,saved)
    packets.append(context['read_context'](notebooks=notes))
    assert chosen == [1, 2, None]  # The hidden recommendation really did change.
    assert packets[0] == packets[1] == packets[2]
    assert correction_event(packets[0]) == {'current_event': observation['current_event'], 'options': observation['choices']}
    assert all(name in packets[0]['constraints']['守红线'] for name in ('“身心健康”过低', '“尊严”过低', '“个人财富”过低'))
    assert '差2分晋升' in packets[0]['constraints']['晋升条件']


def test_all_low_health_losses_return_semantic_packet_without_unsafe_recommendation(runtime):
    _, notes, observation, path, state, refresh, context = runtime
    observation['current_state']['status']['health'] = 3
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    receipt_path = state['event_dir'](notes, state['current_task'](notes)) / 'HW/complete.json'
    receipt = state['read'](receipt_path)
    receipt['values']['1']['H'] = -1
    state['save'](receipt_path, receipt)
    result = context['read_context'](notebooks=notes)
    assert correction_event(result) == {
        'current_event': observation['current_event'],
        'options': observation['choices'],
    }


@pytest.mark.parametrize('field,metric', [('health', 'Health'), ('dignity', 'Dignity')])
def test_official_negative_delta_cannot_cut_a_low_redline(runtime, field, metric):
    _, notes, observation, path, _, refresh, context = runtime
    observation['current_state']['status'][field] = 3
    observation['choices'] = [
        {'choice': 1, 'action': '削减红线补技能', 'status_updates': {metric: -1, 'Skill': 3}},
        {'choice': 2, 'action': '维持红线', 'status_updates': {}},
    ]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    assert_recommendation(context['read_context'](notebooks=notes), 2)


def test_official_menu_without_safe_option_cannot_return_unsafe_recommendation(runtime):
    _, notes, observation, path, _, refresh, context = runtime
    observation['current_state']['status']['health'] = 3
    observation['choices'] = [{'choice': 1, 'action': '削减红线', 'status_updates': {'Health': -1}}]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    with pytest.raises(ValueError, match='所有选项均削减已过低指标，无法推荐'):
        context['read_context'](notebooks=notes)


@pytest.mark.parametrize('other_h', [-1, 0])
def test_purely_negative_or_forbidden_packet_omits_official_metric_deltas(runtime, other_h):
    _, notes, observation, path, state, refresh, context = runtime
    observation['current_state']['status']['dignity'] = 3
    observation['current_state']['status']['health'] = 3
    observation['choices'] = [
        {'choice': 1, 'action': '削减尊严', 'status_updates': {'Dignity': -1}},
        {'choice': 2, 'action': '维持红线'},
    ]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    answers = {'O': ['1', '0'], 'N': ['{"2":{"N":0}}'], 'S': ['{"2":{"S":0}}'],
               'HW': ['{"existing_H_load":false,"physical_risk":false}',
                          json.dumps({'2': {'H': other_h, 'W': 0}}), '{"2":false}'], 'R': ['{"2":0}'] * 3}
    for group, sequence in answers.items():
        state['answer'](group, 'ready', notes)
        for answer in sequence:
            result = state['answer'](group, answer, notes)
        assert result['complete'], (group, result)
    result = context['read_context'](notebooks=notes)
    assert correction_event(result) == {
        'current_event': observation['current_event'],
        'options': [{'choice': 1, 'action': '削减尊严'}, {'choice': 2, 'action': '维持红线'}],
    }


def test_l1_deadlock_catches_each_option_losing_a_different_threshold(runtime):
    *_, context = runtime
    state = dict(L=1, S=9, O=3, N=3, H=8, D=9, W=2, R=0)
    caps = {1: ('known', dict(S=9, O=3, N=3))}
    options = [
        {'choice': 1, 'metrics': dict(O=-1, N=1, S=1, H=-1, R=0)},
        {'choice': 2, 'metrics': dict(O=0, N=-1, S=0, H=1, R=0)},
    ]
    assert context['semantic_deadlock'](state, caps, options) == '所有选项均破坏L1已满足的晋升门槛'
    options[0]['metrics']['O'] = 1
    assert context['semantic_deadlock'](state, caps, options) is None


def test_half_year_hr_talk_cannot_mask_the_explicit_salary_risk_hint(runtime):
    _, notes, observation, path, _, refresh, _ = runtime
    observation['events'] = ('【半年谈话】HR认为最近干得还不错\n'
                             '闲聊的时候，关系好的HR生气地说，你早干啥去了，之前埋的雷全要炸了！')
    path.write_text(json.dumps(observation))
    assert refresh['record_feedback'](notes, observation) == ('4', '低')


def test_capped_raw_gains_and_risk_reduction_are_not_deadlocks(runtime):
    *_, context = runtime
    state = dict(L=2, S=18, O=5, N=6, H=10, D=10, W=2, R=1)
    caps = {2: ('known', dict(S=18, O=5, N=6))}
    options = [{'choice': 1, 'metrics': dict(S=1, H=1, R=0)}]
    assert context['semantic_deadlock'](state, caps, options) is None
    options[0]['metrics']['R'] = -1
    assert context['semantic_deadlock'](state, caps, options) is None


def test_safe_least_loss_candidates_are_not_deadlocks(runtime):
    *_, context = runtime
    state = dict(L=2, S=21, O=6, N=6, H=10, D=10, W=4, R=0)
    caps = {2: ('known', dict(S=21, O=6, N=7))}
    options = [
        {'choice': 1, 'metrics': dict(O=-1, N=0, S=1, R=0)},
        {'choice': 2, 'metrics': dict(O=-1, N=1, R=2)},
        {'choice': 4, 'metrics': dict(N=-1, S=1, R=-1)},
    ]
    assert context['semantic_deadlock'](state, caps, options) is None
    options[0]['metrics']['N'] = 1
    assert context['semantic_deadlock'](state, caps, options) is None


def feedback_event(runtime, *, delta=1, prediction=0, hw=None):
    root, notes, observation, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for group in ('O', 'N', 'S', 'HW', 'R'):
        if group == 'O' and prediction:
            state['answer']('O', 'ready', notes)
            state['answer']('O', '1', notes)
            state['answer']('O', '2' if prediction < 0 else '1', notes)
            assert state['answer']('O', str(abs(prediction)), notes)['complete']
        elif group == 'HW' and hw is not None:
            state['answer']('HW', 'ready', notes)
            state['answer']('HW', '{"existing_H_load":false,"physical_risk":false}', notes)
            state['answer']('HW', json.dumps({'1': hw}), notes)
            assert state['answer']('HW', json.dumps({'1': hw['H'] != 0}), notes)['complete']
        else:
            complete(state, group, notes)
    helper = runpy.run_path(str(root / 'scripts/questionnaire-feedback.py'))
    helper['cache_review'](notes, {'O': delta}, 1)
    return helper


@pytest.mark.parametrize('predicted,actual', [
    (0, 1), (0, -1), (1, 0), (-1, 0), (1, -1), (-1, 1),
    (1, 3), (3, 1), (-1, -3), (-3, -1), (0, 0), (2, 2), (-2, -2),
])
def test_only_direction_errors_enter_questionnaire_edit(runtime, predicted, actual):
    feedback_event(runtime, delta=actual, prediction=predicted)
    _, notes, observation, _, state, _, _ = runtime
    state['prepare'](notes, observation, '00002')
    first = state['answer']('O', 'ready', notes)
    different_direction = (predicted > 0) - (predicted < 0) != (actual > 0) - (actual < 0)
    assert (first.get('kind') == 'questionnaire_edit') == different_direction
    history = state['event_dir'](notes, state['current_task'](notes)).parent / 'O-direction-errors.md'
    assert history.exists() == different_direction
    assert not state['read'](notes / 'questionnaire-feedback.json')['pending']


def test_feedback_keeps_previous_event_history_and_only_the_executed_option(runtime):
    _, notes, observation, path, state, _, _ = runtime
    observation['event_history'] = {'steps': [{
        'description': 'PREVIOUS CHAIN STORY',
        'selected_choice': {'action': 'PREVIOUS CHAIN ACTION'}}]}
    observation['choices'].append({'choice': 7, 'action': 'UNSELECTED ACTION'})
    path.write_text(json.dumps(observation))
    feedback_event(runtime)
    old_public = state['event_dir'](notes, state['current_task'](notes)) / 'public-event.json'
    old_public.unlink()  # The saved teaching context survives loss of the old public archive.
    observation['current_event'] = {'title': 'CURRENT TITLE', 'description': 'CURRENT STORY'}
    observation['choices'] = [{'choice': 3, 'action': 'CURRENT ACTION'}]
    state['prepare'](notes, observation, '00002')
    first = state['answer']('O', 'ready', notes)
    request = state['read'](state['event_dir'](notes, state['current_task'](notes)) / 'O/.edit-request.json')
    history = Path(request['history_path']).read_text()
    for text in ('PRIVATE TITLE', 'PRIVATE STORY', 'PRIVATE ACTION', 'PRIVATE DETAIL',
                 'PREVIOUS CHAIN STORY', 'PREVIOUS CHAIN ACTION'):
        assert text in history
    assert 'UNSELECTED ACTION' not in history
    assert 'PRIVATE TITLE' in first['question'] and 'PRIVATE STORY' not in first['question']
    assert 'PREVIOUS CHAIN STORY' not in first['question']
    assert 'CURRENT STORY' not in first['question']
    assert Path(request['history_path']).is_absolute()
    assert request['history_path'] in first['question']
    assert request['feedback']['choice'] == 1
    assert request['feedback']['predicted'] == {'O': 0}
    assert request['feedback']['actual'] == {'O': 1}


def test_hw_feedback_omits_same_direction_magnitude_errors(runtime):
    helper = feedback_event(runtime, delta=0, hw={'H': 0, 'W': 1})
    _, notes, observation, _, state, _, _ = runtime
    helper['cache_review'](notes, {'H': 1, 'W': 3}, 1)
    state['prepare'](notes, observation, '00002')
    assert state['answer']('HW', 'ready', notes)['kind'] == 'questionnaire_edit'
    request = state['read'](state['event_dir'](notes, state['current_task'](notes)) / 'HW/.edit-request.json')
    assert request['feedback']['predicted'] == {'H': 0}
    assert request['feedback']['actual'] == {'H': 1}


def test_legacy_magnitude_only_feedback_is_consumed_without_a_revision(runtime):
    feedback_event(runtime, delta=0)
    _, notes, observation, _, state, _, _ = runtime
    state['save'](notes / 'questionnaire-feedback.json', {'session_id': 'game', 'pending': {
        'O': {'event_id': '00001', 'choice': 1, 'title': 'PRIVATE TITLE',
              'action': 'PRIVATE ACTION', 'predicted': {'O': 1}, 'actual': {'O': 3}}}})
    state['prepare'](notes, observation, '00002')
    assert 'kind' not in state['answer']('O', 'ready', notes)
    assert not state['read'](notes / 'questionnaire-feedback.json')['pending']
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'O'
    assert not (lane / '.edit-request.json').exists()


def test_legacy_direction_feedback_restores_previous_public_narrative(runtime):
    feedback_event(runtime)
    _, notes, observation, _, state, _, _ = runtime
    path = notes / 'questionnaire-feedback.json'
    feedback = state['read'](path)
    feedback['pending']['O'].pop('event')
    feedback['pending']['O'].pop('description')
    state['save'](path, feedback)
    state['prepare'](notes, observation, '00002')
    first = state['answer']('O', 'ready', notes)
    request = state['read'](state['event_dir'](notes, state['current_task'](notes)) / 'O/.edit-request.json')
    assert 'PRIVATE STORY' not in first['question'] and 'PRIVATE DETAIL' not in first['question']
    assert 'PRIVATE STORY' in request['feedback']['event']['current_event']['description']
    assert 'PRIVATE DETAIL' in Path(request['history_path']).read_text()


def test_feedback_is_consumed_on_first_ready_even_if_edit_is_skipped(runtime):
    feedback_event(runtime)
    _, notes, observation, _, state, _, _ = runtime
    state['prepare'](notes, observation, '00002')
    first = state['answer']('O', 'ready', notes)
    assert first['kind'] == 'questionnaire_edit'
    assert 'PRIVATE TITLE' in first['question'] and '真实{"O": 1}' in first['question']
    assert not state['read'](notes / 'questionnaire-feedback.json')['pending']
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'O'
    request = (lane / '.edit-request.json').read_bytes()
    again = state['answer']('O', 'ready', notes)
    assert again == first
    assert (lane / '.edit-request.json').read_bytes() == request
    assert not state['read'](notes / 'questionnaire-feedback.json')['pending']
    assert state['answer']('O', 'skip', notes)['questionnaire_skipped']
    assert 'kind' not in state['answer']('O', 'ready', notes)


def test_n_feedback_retains_the_flat_answer_without_legacy_route_fields(runtime):
    helper = feedback_event(runtime, delta=0)
    _, notes, _, _, state, _, _ = runtime
    helper['cache_review'](notes, {'N': 1}, 1)
    feedback = state['read'](notes / 'questionnaire-feedback.json')['pending']['N']
    assert feedback['prior_answers'] == {'N': 0}
    assert feedback['actual'] == {'N': 1}


def test_edit_cannot_target_an_unvisited_o_branch(runtime):
    helper = feedback_event(runtime, prediction=1, delta=0)
    root, notes, observation, _, state, _, _ = runtime
    feedback = state['read'](notes / 'questionnaire-feedback.json')['pending']['O']
    steps = feedback['answer_path']
    assert [{'question': step['question'], 'answer': step['answer']} for step in steps] == [
        {'question': 'kind', 'answer': 1}, {'question': 'business', 'answer': 1},
        {'question': 'scale_plus', 'answer': 1}]
    assert all(step['text'] for step in steps)
    state['prepare'](notes, observation, '00002')
    first = state['answer']('O', 'ready', notes)
    assert '当前问答题JSON' not in first['question']
    path = root / 'scripts/questionnaires/O.json'
    before = path.read_bytes()
    wrong = state['answer']('O', json.dumps({'question': 'other', 'old': '', 'new': '不能误改此题。'}), notes)
    assert '只修改实际答题路径' in wrong['question'] and path.read_bytes() == before
    assert state['answer']('O', json.dumps({'question': 'business', 'old': '', 'new': '保留有效边界。'}), notes)['questionnaire_updated']


def test_hw_feedback_retains_route_joint_and_confirmation_after_completion(runtime):
    helper = feedback_event(runtime, delta=0, hw={'H': -1, 'W': 0})
    _, notes, _, _, state, _, _ = runtime
    helper['cache_review'](notes, {'H': 0}, 1)
    steps = state['read'](notes / 'questionnaire-feedback.json')['pending']['HW']['answer_path']
    assert [{'question': step['question'], 'answer': step['answer']} for step in steps] == [
        {'question': 'route', 'answer': {'existing_H_load': False, 'physical_risk': False}},
        {'question': 'ordinary', 'answer': {'H': -1, 'W': 0}},
        {'question': 'confirm', 'answer': True}]
    assert all(step['text'] for step in steps)


def test_direction_history_survives_consumption_and_review_retries(runtime):
    helper = feedback_event(runtime)
    _, notes, observation, _, state, _, _ = runtime
    task = state['current_task'](notes)
    path = helper['history_path'](state, notes, task, 'O')
    first = path.read_text()
    helper['cache_review'](notes, {'O': 1}, 1)
    assert path.read_text() == first
    state['prepare'](notes, observation, '00002')
    state['answer']('O', 'ready', notes)
    assert state['answer']('O', 'skip', notes)['questionnaire_skipped']
    for group in state['GROUPS']:
        complete(state, group, notes)
    helper['cache_review'](notes, {'O': -1}, 1)
    history = path.read_text()
    assert history.count('## 00001 | ') == history.count('## 00002 | ') == 1
    assert first in history and '回答：' in history
    helper['cache_review'](notes, {}, 1)
    assert path.read_text() == history
    other_game = {**task, 'session_id': 'another game'}
    assert helper['history_path'](state, notes, other_game, 'O') != path
    assert not helper['history_path'](state, notes, task, 'R').exists()


def test_each_metric_has_an_independent_history_including_h_and_w(runtime):
    helper = feedback_event(runtime, delta=0)
    _, notes, observation, _, state, _, _ = runtime
    helper['cache_review'](notes, {'O': 1, 'N': -1, 'S': 1, 'H': 1, 'W': -1}, 1)
    task = state['current_task'](notes)
    paths = [helper['history_path'](state, notes, task, metric) for metric in ('O', 'N', 'S', 'H', 'W')]
    assert len(set(paths)) == 5
    for metric, path in zip(('O', 'N', 'S', 'H', 'W'), paths):
        text = path.read_text()
        assert text.startswith(f'# {metric} 方向误判历史')
        actual = next(line for line in text.splitlines() if line.startswith('预测：')).split('；真实：')[1]
        assert set(json.loads(actual)) == {metric}
    assert not helper['history_path'](state, notes, task, 'HW').exists()
    state['prepare'](notes, observation, '00002')
    result = state['answer']('HW', 'ready', notes)
    request = state['read'](state['event_dir'](notes, state['current_task'](notes)) / 'HW/.edit-request.json')
    assert request['history_paths'] == [str(p.resolve()) for p in paths[-2:]]
    assert all(str(p.resolve()) in result['question'] for p in paths[-2:])


def test_skip_edit_returns_current_question_without_another_ready(runtime):
    feedback_event(runtime)
    root, notes, observation, _, state, _, _ = runtime
    state['prepare'](notes, observation, '00002')
    state['answer']('O', 'ready', notes)
    before = (root / 'scripts/questionnaires/O.json').read_bytes()
    first = state['answer']('O', 'skip', notes)
    assert first['questionnaire_skipped'] and 'questionnaire_updated' not in first
    assert first['event']['current_event'] == observation['current_event']
    assert '本选项实际作用于哪类事务' in first['question']
    assert (root / 'scripts/questionnaires/O.json').read_bytes() == before
    assert not state['answer']('O', '1', notes)['complete']
    assert state['answer']('O', '0', notes)['complete']


def test_one_text_patch_updates_only_own_question_and_receipt_version(runtime):
    feedback_event(runtime)
    root, notes, observation, _, state, _, _ = runtime
    state['prepare'](notes, observation, '00002')
    for group in ('N', 'S', 'HW', 'R'):
        complete(state, group, notes)
    task = state['current_task'](notes)
    directory = state['event_dir'](notes, task)
    other_receipt = (directory / 'S/complete.json').read_bytes()
    other_question = (root / 'scripts/questionnaires/N.json').read_bytes()
    question_path = root / 'scripts/questionnaires/O.json'
    before = state['read'](question_path)
    state['answer']('O', 'ready', notes)
    replacement = before['questions']['kind']['text'] + '普通协商本身不计专门耗损。'
    result = state['answer']('O', json.dumps({'question': 'kind', 'text': replacement}), notes)
    assert result['questionnaire_updated'] and 'kind' not in result
    after = state['read'](question_path)
    assert after['questions']['kind']['text'] == replacement
    assert after['questions']['kind']['options'] == before['questions']['kind']['options']
    assert (root / 'scripts/questionnaires/N.json').read_bytes() == other_question
    assert (directory / 'S/complete.json').read_bytes() == other_receipt
    updated = state['current_task'](notes)
    assert updated['scripts']['O'] != task['scripts']['O']
    assert updated['scripts']['S'] == task['scripts']['S']
    review_path = notes / 'review-context.json'
    review = state['read'](review_path)
    review['event_key'] = '00002'
    state['save'](review_path, review)
    assert replacement in result['question']
    assert result['event']['current_event'] == observation['current_event']
    assert not state['answer']('O', '1', notes)['complete']
    assert state['answer']('O', '0', notes)['complete']
    assert state['option_metrics'](notes)[0]['metrics']['O'] == 0
    state['save'](question_path, before)  # Simulate reinstall restoring source questions.
    state['prepare'](notes, observation, '00003')
    assert state['read'](question_path) == after


def test_official_quarter_actions_preserve_pending_learning(runtime):
    helper = feedback_event(runtime)
    _, notes, observation, _, state, _, _ = runtime
    before = (notes / 'questionnaire-feedback.json').read_bytes()
    observation['choices'][0]['status_updates'] = {'Output': 1}
    observation['current_event']['decision_type'] = 'energy_action'
    state['prepare'](notes, observation, '00002')
    helper['cache_review'](notes, {'O': 1}, 1)
    assert state['answer']('O', 'ready', notes)['complete']
    assert (notes / 'questionnaire-feedback.json').read_bytes() == before
    observation['choices'][0].pop('status_updates')
    state['prepare'](notes, observation, '00003')
    assert state['answer']('O', 'ready', notes)['kind'] == 'questionnaire_edit'


def test_invalid_patch_cannot_change_answer_values_or_another_file(runtime):
    feedback_event(runtime)
    root, notes, observation, _, state, _, _ = runtime
    state['prepare'](notes, observation, '00002')
    state['answer']('O', 'ready', notes)
    path = root / 'scripts/questionnaires/O.json'
    before = path.read_bytes()
    result = state['answer']('O', '{"question":"kind","text":"x","options":[9]}', notes)
    assert result['kind'] == 'questionnaire_edit' and result['question'].startswith('答案格式错误')
    assert path.read_bytes() == before
    assert state['answer']('O', 'ready', notes)['kind'] == 'questionnaire_edit'


def test_short_phrase_patch_preserves_the_rest_of_the_question(runtime):
    feedback_event(runtime)
    root, notes, observation, _, state, _, _ = runtime
    state['prepare'](notes, observation, '00002')
    first = state['answer']('O', 'ready', notes)
    assert '"old"' in first['answer_template'] and '"new"' in first['answer_template']
    path = root / 'scripts/questionnaires/O.json'
    before = state['read'](path)
    wrong = state['answer']('O', '{"question":"kind","old":"不存在的词","new":"x"}', notes)
    assert wrong['kind'] == 'questionnaire_edit' and state['read'](path) == before
    result = state['answer']('O', '{"question":"kind","old":"其他事务","new":"非核心业务事务"}', notes)
    assert result['questionnaire_updated'] and 'kind' not in result
    after = state['read'](path)
    assert after['questions']['kind']['text'] == before['questions']['kind']['text'].replace('其他事务', '非核心业务事务')
    assert after['questions']['business'] == before['questions']['business']


@pytest.mark.parametrize('metric', ['N', 'S'])
@pytest.mark.parametrize('submitted_key', [None, '| Network | 人脉 | 0–不限 | 综合人脉能力 |', 'other'])
def test_single_question_patch_owns_key_and_tolerates_legacy_mislabel(runtime, metric, submitted_key):
    root, notes, observation, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for group in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, group, notes)
    helper = runpy.run_path(str(root / 'scripts/questionnaire-feedback.py'))
    helper['cache_review'](notes, {metric: 1}, 1)
    state['prepare'](notes, observation, '00002')
    first = state['answer'](metric, 'ready', notes)
    assert first['kind'] == 'questionnaire_edit'
    assert first['answer_template'].startswith('JSON {"old":')
    assert 'question' not in first['answer_template']
    assert len(first['edit_arguments']) == 2
    question_path = root / f'scripts/questionnaires/{metric}.json'
    before = state['read'](question_path)
    patch = {'old': '', 'new': '只判断当前选项本步效果。'}
    if submitted_key is not None:
        patch['question'] = submitted_key
    result = state['answer'](metric, json.dumps(patch), notes)
    assert result['questionnaire_updated'] and 'kind' not in result
    assert result['event']['current_event'] == observation['current_event']
    after = state['read'](question_path)
    assert after['questions']['question']['text'] == before['questions']['question']['text'] + '\n只判断当前选项本步效果。'
    assert after['questions']['question']['options'] == before['questions']['question']['options']


def test_multibranch_patch_requires_an_explicit_key(runtime):
    feedback_event(runtime)
    root, notes, observation, _, state, _, _ = runtime
    state['prepare'](notes, observation, '00002')
    first = state['answer']('O', 'ready', notes)
    assert '<题键：' in first['answer_template']
    question_path = root / 'scripts/questionnaires/O.json'
    before = question_path.read_bytes()
    result = state['answer']('O', '{"old":"","new":"只判断当前选项本步效果。"}', notes)
    assert result['kind'] == 'questionnaire_edit'
    assert result['question'].startswith('答案格式错误')
    assert question_path.read_bytes() == before


def test_zero_public_error_and_hidden_r_do_not_trigger_learning(runtime):
    feedback_event(runtime, delta=0)
    _, notes, observation, _, state, _, _ = runtime
    assert not state['read'](notes / 'questionnaire-feedback.json')['pending']
    state['prepare'](notes, observation, '00002')
    assert 'kind' not in state['answer']('O', 'ready', notes)
    assert 'kind' not in state['answer']('R', 'ready', notes)


def test_legacy_r_feedback_and_overrides_cannot_edit_hidden_questionnaire(runtime):
    root, notes, observation, _, state, _, _ = runtime
    question_path = root / 'scripts/questionnaires/R.json'
    original = question_path.read_bytes()
    changed = state['read'](question_path)
    changed['questions'][next(iter(changed['questions']))]['text'] = 'invalid hidden-risk teaching'
    state['save'](notes / 'questionnaire-overrides.json', {
        'session_id': 'game', 'questionnaires': {'R': changed}})
    state['prepare'](notes, observation, '00002')
    assert question_path.read_bytes() == original
    state['save'](notes / 'questionnaire-feedback.json', {
        'session_id': 'game', 'pending': {'R': {
            'event_id': '00001', 'predicted': {'R': 1}, 'actual': {'R': -1}}}})
    task = state['current_task'](notes)
    lane = state['event_dir'](notes, task) / 'R'
    state['save'](lane / '.edit-request.json', {})
    first = state['answer']('R', 'ready', notes)
    assert 'kind' not in first and not (lane / '.edit-request.json').exists()
    state['save'](lane / '.edit-request.json', {})
    assert state['answer']('R', '{"1":2}', notes)['complete']
    assert question_path.read_bytes() == original
    helper = runpy.run_path(str(root / 'scripts/questionnaire-feedback.py'))
    with pytest.raises(ValueError, match='R is hidden'):
        helper['apply_edit'](state, 'R', '{}', notes, task, lane)


def test_official_menu_bypasses_questions_and_preserves_cost(runtime):
    _, notes, observation, path, state, refresh, context = runtime
    observation['current_event'] = {'title': '季度体力行动分配', 'decision_type': 'energy_action'}
    observation['choices'] = [
        {'choice': 3, 'action': '人脉维护', 'action_id': 'network_maintenance',
         'status_updates': {'Network': 1, 'Dignity': 1}, 'energy_cost': 1},
        {'choice': 8, 'action': '保留体力（不行动）', 'action_id': 'no_action',
         'status_updates': {}, 'energy_cost': 0},
    ]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    assert all(state['answer'](m, 'ready', notes)['complete'] for m in ('O', 'N', 'S', 'HW', 'R'))
    assert [c['choice'] for c in state['current_task'](notes)['choices']] == [3]
    assert [c['choice'] for c in state['read'](notes / 'review-context.json')['choices']] == [3]
    assert state['option_metrics'](notes) == [
        {'choice': 3, 'metrics': dict(O=0, N=1, S=0, H=0, W=0, R=0),
         'energy_cost': 1, 'selectable': True}]
    assert state['current_task'](notes)['choices'][0]['status_updates']['Dignity'] == 1
    assert_recommendation(context['read_context'](notebooks=notes), 3)


def test_energy_skip_remains_only_when_no_safe_action_is_affordable(runtime):
    _, notes, observation, path, state, refresh, context = runtime
    observation['current_event'] = {'title': '季度体力行动分配', 'decision_type': 'energy_action'}
    observation['current_state']['status']['energy'] = 1
    observation['choices'] = [
        {'choice': 1, 'action': '高强度工作', 'action_id': 'high_intensity_work',
         'status_updates': {'Output': 2, 'Health': -1}, 'energy_cost': 2},
        {'choice': 8, 'action': '保留体力（不行动）', 'action_id': 'no_action',
         'status_updates': {}, 'energy_cost': 0},
    ]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    assert [c['choice'] for c in state['current_task'](notes)['choices']] == [1, 8]
    # Internal option data retains eligibility; the Leader only sees usable choices.
    assert [o['selectable'] for o in state['option_metrics'](notes)] == [False, True]
    assert_recommendation(context['read_context'](notebooks=notes), 8)


def test_energy_risk_reduction_counts_as_harmless(runtime):
    _, notes, observation, path, state, refresh, context = runtime
    observation['current_event'] = {'title': '季度体力行动分配', 'decision_type': 'energy_action'}
    observation['current_state']['status']['energy'] = 2
    observation['choices'] = [
        {'choice': 4, 'action': '处理历史埋雷', 'action_id': 'clean_up_risk',
         'status_updates': {'HiddenRisk': -1}, 'energy_cost': 2},
        {'choice': 8, 'action': '保留体力（不行动）', 'action_id': 'no_action',
         'status_updates': {}, 'energy_cost': 0},
    ]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    assert [c['choice'] for c in state['current_task'](notes)['choices']] == [4]
    assert state['option_metrics'](notes) == [
        {'choice': 4, 'metrics': dict(O=0, N=0, S=0, H=0, W=0, R=-1),
         'energy_cost': 2, 'selectable': True}]
    assert_recommendation(context['read_context'](notebooks=notes), 4)


def test_wrong_version_and_wrong_observation_receipts_are_rejected(runtime):
    root, notes, _, path, state, refresh, context = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    directory = state['event_dir'](notes, state['current_task'](notes))
    receipt = directory / 'N/complete.json'
    original = json.loads(receipt.read_text())
    receipt.write_text(json.dumps({**original, 'observation_hash': 'stale'}))
    with pytest.raises(ValueError, match='receipt mismatch'):
        context['read_context'](notebooks=notes)
    receipt.write_text(json.dumps(original))
    script = root / 'scripts/questionnaires/O.py'
    script.write_text(script.read_text() + '\n# changed\n')
    with pytest.raises(ValueError, match='questionnaire changed'):
        state['answer']('O', 'ready', notes)






def test_full_receipt_sequence_and_terminal(runtime):
    root, notes, observation, path, state, refresh, context = runtime
    state['begin_stage']('observe', notes)
    refresh['refresh_observation'](notes, path, emit=False)
    state['finish_stage']('observe', notes)
    state['begin_stage']('analyse', notes)
    for m in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, m, notes)
    state['finish_stage']('analyse', notes)
    state['begin_stage']('decide', notes)
    context['read_context'](notebooks=notes)
    # Review records the executed action even if questionnaires change afterwards.
    questionnaire = root / 'scripts/questionnaires/O.py'
    questionnaire.write_text(questionnaire.read_text() + '\n# updated after action\n')
    log = write_action_log(root / 'game.log', observation, 1, {'Skill': 1})
    record = runpy.run_path(str(root / 'scripts/record-review.py'))['record_log']
    record(notes, log)
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'reviewed'
    state['begin_stage']('observe', notes)
    path.write_text(json.dumps({'current_state': observation['current_state'],
                               'ending_score': {'completed': True, 'survival_months': 48}}))
    refresh['refresh_observation'](notes, path, emit=False)
    assert state['finish_stage']('observe', notes)['phase'] == 'terminal'
    with pytest.raises(ValueError, match='blocked'):
        state['begin_stage']('analyse', notes)




def test_questions_need_no_cursor_and_remain_bound_to_the_current_event(runtime):
    _, notes, _, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    first = state['answer']('O', 'ready', notes)
    assert 'event_id' not in first and 'question_id' not in first
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'O'
    state['answer']('O', '0', notes)
    assert not (lane / 'cursor.json').exists()
    with pytest.raises(ValueError, match='stale event_id'):
        state['answer']('O', '2', notes, '00002')




def test_unique_file_join_waits_for_every_analysis_worker(runtime, monkeypatch):
    root, notes, _, path, state, refresh, context = runtime
    control = runpy.run_path(str(root / 'scripts/workflow-state.py'))

    def pause(_):
        raise InterruptedError('still waiting')

    monkeypatch.setattr(control['time'], 'sleep', pause)
    state['save'](notes / 'handbook-ready.json', {'session_id': 'game'})
    state['begin_stage']('observe', notes)
    refresh['refresh_observation'](notes, path, emit=False)
    state['finish_stage']('observe', notes)
    state['begin_stage']('analyse', notes)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
        if metric != 'R':
            with pytest.raises(InterruptedError, match='still waiting'):
                control['wait']()
            assert state['read'](notes / 'workflow-state.json')['phase'] == 'analyse_running'
    assert control['wait']() == context['read_context'](notebooks=notes)
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'decide_running'
    assert not (notes / 'dispatches').exists()
    assert not list(notes.rglob('.aggregate.lock'))
    assert not list(notes.rglob('result.json'))
