"""Conditional noise must preserve the raw guards and discrete promotion logic."""
import json
from pathlib import Path
import runpy

import pytest

from scripts.benchmark_symbolic_policy import ErrorModel

SCRIPTS = Path(__file__).resolve().parents[1]/'solution/skills/observe-decide-review/scripts'
API = runpy.run_path(str(SCRIPTS/'read-context.py'))
POLICY = API['POLICY']
CAPS = {l: (0, {m: int(v*1.2) for m, v in req.items()}) for l, req in API['PROMOTION_REQUIREMENTS'].items()}
CAPS[10] = (0, dict(S=None, O=None, N=None))
HELPERS = dict(project=API['projected_status'], guard=API['redline_candidates'],
               safety=API['redline_score'], greedy=API['_greedy_guidance'],
               requirements=API['PROMOTION_REQUIREMENTS'])


def state(**values):
    return dict(L=1, S=9, O=3, N=3, H=10, D=10, W=2, R=0) | values


def option(choice, **delta):
    return dict(choice=choice, metrics=dict.fromkeys('SONHDWR', 0) | delta)


def context(**values):
    return dict(month=1, kind='story', duration=1, story_actions=0, energy=3, energy_actions=0) | values


def test_pmf_mass_and_error_definition_are_preserved():
    model = POLICY['ERRORS']['MODEL']
    assert model['error_definition'] == 'nominal actual delta - predicted delta'
    assert set(model['metrics']) == set('ONSHWR')
    for cells in model['metrics'].values():
        assert set(cells) == set(map(str, range(-3, 4)))
        for pmf in cells.values():
            assert abs(sum(pmf.values())-1) < 1e-12
            assert min(pmf.values()) >= 0


def test_saved_model_matches_requested_conditional_backoff():
    root = SCRIPTS.parents[3]
    source = root/'logs/conditional_model-coherent-20261005.json'
    if not source.exists():
        return  # The exported submission model is independently covered above.
    import hashlib
    model = json.loads(source.read_text())
    distribution = runpy.run_path(str(root/'logs/conditional_current.py'))['distribution']
    exported = POLICY['ERRORS']['MODEL']
    assert exported['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    for metric in exported['metrics']:
        for predicted in range(-3, 4):
            expected = distribution(model, metric, predicted)['pmf']
            assert {int(k): v for k, v in exported['metrics'][metric][str(predicted)].items()} == expected


def test_capped_equal_predictions_prefer_more_likely_gate_retention(monkeypatch):
    point_model(monkeypatch)
    POLICY['ERRORS']['MODEL']['metrics']['N']['1'] = {'-2': .8, '0': .2}
    POLICY['ERRORS']['MODEL']['metrics']['N']['2'] = {'-3': .1, '0': .9}
    current = state()
    choices = [option(1, N=1), option(2, N=2)]
    assert API['projected_status'](current, CAPS, choices[0]['metrics']) == API['projected_status'](current, CAPS, choices[1]['metrics'])
    assert POLICY['_choose_nominal'](current, CAPS, choices, context(), HELPERS) == 1
    assert POLICY['choose'](current, CAPS, choices, context(), HELPERS) == 2


def test_error_model_never_changes_a_distinct_nominal_shortfall_decision():
    current = state(S=7)
    choices = [option(1, S=1), option(2, N=2)]
    assert POLICY['_choose_nominal'](current, CAPS, choices, context(), HELPERS) == 1
    assert POLICY['choose'](current, CAPS, choices, context(), HELPERS) == 1


def test_unknown_risk_cannot_make_different_raw_risks_equivalent():
    current = state(R=None)
    choices = [option(1, N=2, R=2), option(2, N=1)]
    assert POLICY['choose'](current, CAPS, choices, context(), HELPERS) == 2


def test_posterior_does_not_rescue_a_raw_forbidden_wealth_loss():
    choices = [option(1, N=2, W=-1), option(2, N=1)]
    assert POLICY['choose'](state(), CAPS, choices, context(), HELPERS) == 2
    assert POLICY['choose'](state(), CAPS, choices[:1], context(), HELPERS) is None


def test_inverse_sampling_subtracts_a_positive_actual_minus_prediction_error():
    model = ErrorModel.__new__(ErrorModel)
    model.model = {'metrics': {'O': {'prediction_counts': {}}}}
    model.pmfs = {'O': {p: {1: 1.} for p in range(-3, 4)}}
    for mode in ('uniform', 'historical', 'manual'):
        for key in ('a', 'b', 'c'):
            assert model.sample('O', 1, mode, key) == 0
    assert model.sample('O', 1, 'none', 'a') == 1


def point_model(monkeypatch):
    metrics = {m: {str(p): {'0': 1.} for p in range(-3, 4)} for m in 'ONSHWR'}
    monkeypatch.setitem(POLICY['ERRORS']['MODEL'], 'metrics', metrics)


def test_all_raw_zero_and_all_forbidden_are_zero_confidence_abstentions():
    choices = [option(1), option(2)]
    result = API['decision_confidence'](state(), CAPS, choices, context(), 1)
    assert result['confidence'] == 0 and result['reason'] == '所有选项均预测无正向增量'
    choices = [option(1, W=-1, S=2), option(2, H=-1, N=2)]
    result = API['decision_confidence'](state(H=3), CAPS, choices, context(), None)
    assert result['confidence'] == 0 and '无可推荐选项' in result['reason']


@pytest.mark.parametrize('metric', list('SONHDWR'))
@pytest.mark.parametrize('delta', [-1, 1])
def test_raw_benefit_direction_defines_purely_negative_events(metric, delta):
    current = state(L=2, S=21, O=6, N=7, H=10, D=10, W=10)
    menu = [option(1, **{metric: delta}), option(2)]
    benefit = delta < 0 if metric == 'R' else delta > 0
    reason = API['semantic_deadlock'](current, CAPS, menu)
    assert reason == (None if benefit else '所有选项均预测无正向增量')


def test_zero_safe_choice_does_not_hide_a_purely_negative_translation(monkeypatch):
    point_model(monkeypatch)
    current = state(H=3)
    menu = [option(1, H=-1), option(2)]
    selected = POLICY['choose'](current, CAPS, menu, context(), API['_policy_helpers']())
    assert selected == 2
    result = API['decision_confidence'](current, CAPS, menu, context(), selected)
    assert result['confidence'] == 0 and result['reason'] == '所有选项均预测无正向增量'


def test_capped_raw_gains_with_net_losses_remain_stable_recommendations(monkeypatch):
    point_model(monkeypatch)
    current = state(L=2, S=21, O=6, N=7, H=10, D=10, W=20)
    menu = [option(1, S=3, H=-2), option(2, N=2, D=-1)]
    selected = POLICY['choose'](current, CAPS, menu, context(month=13), API['_policy_helpers']())
    assert selected == 2 and API['semantic_deadlock'](current, CAPS, menu) is None
    result = API['decision_confidence'](current, CAPS, menu, context(month=13), selected)
    assert result['confidence'] == 1 and result['reason'] is None


def test_each_l1_option_breaking_a_different_held_gate_has_zero_confidence():
    choices = [option(1, O=-1, H=1), option(2, N=-1, H=1)]
    result = API['decision_confidence'](state(H=8), CAPS, choices, context(), 1)
    assert result['confidence'] == 0 and result['reason'] == '所有选项均破坏L1已满足的晋升门槛'


def test_second_risk_burst_is_dead_even_when_each_option_has_positive_growth():
    choices = [option(1, S=1, R=1), option(2, N=1, R=2)]
    result = API['decision_confidence'](state(L=2, S=10, O=5, N=4, R=4), CAPS, choices,
                                        context(risk_bursts=1), 1)
    assert result['confidence'] == 0 and result['reason'] == '所有守红线候选均预测触发淘汰'


def test_hidden_risk_sign_error_can_trigger_correction_without_a_nominal_deadlock(monkeypatch):
    point_model(monkeypatch)
    POLICY['ERRORS']['MODEL']['metrics']['R']['0'] = {'2': 1.}
    current = state(L=2, S=0, O=0, N=0, R=3, H=8, W=4)
    menu = [option(1, S=1), option(2, N=1, R=-1)]
    ctx = context(risk_bursts=1)
    assert API['semantic_deadlock'](current,CAPS,menu,ctx) is None
    result = API['decision_confidence'](current,CAPS,menu,ctx,1)
    assert result['confidence'] == 0 and result['safe_fraction'] == 0 and result['reason']


@pytest.mark.parametrize('metric',list('ONSHWR'))
def test_generation_and_runtime_posteriors_share_one_joint_law(metric):
    root = SCRIPTS.parents[3]
    data = json.loads((root/'logs/conditional_model-coherent-20261005.json').read_text())['metrics'][metric]
    for predicted in data['prediction_domain']:
        joint = {int(a):prior*data['emission_pmfs'][a][str(predicted)]
                 for a,prior in data['actual_prior'].items()}
        mass = sum(joint.values())
        posterior = POLICY['ERRORS']['MODEL']['metrics'][metric][str(predicted)]
        for actual,probability in joint.items():
            assert posterior[str(actual-predicted)] == pytest.approx(probability/mass,abs=1e-12)


def test_second_bad_review_is_dead_at_the_imminent_boundary():
    choices = [option(1, S=1), option(2, H=1)]
    result = API['decision_confidence'](state(S=7, H=8), CAPS, choices,
                                        context(month=23, story_actions=2, bad_reviews=1), 1)
    assert result['confidence'] == 0 and result['reason'] == '所有守红线候选均预测触发淘汰'


def test_posterior_sign_flip_can_abstain_from_a_nominally_positive_recommendation(monkeypatch):
    point_model(monkeypatch)
    POLICY['ERRORS']['MODEL']['metrics']['S']['1'] = {'-2': 1.}
    current = state(L=2, S=0, O=0, N=0, H=8, D=8, W=4)
    choices = [option(1, S=1), option(2, N=1)]
    selected = POLICY['_choose_nominal'](current, CAPS, choices, context(), HELPERS)
    assert selected == 1 and API['semantic_deadlock'](current, CAPS, choices) is None
    result = API['decision_confidence'](current, CAPS, choices, context(), selected)
    assert result['confidence'] == 0 and result['safe_fraction'] == 1
    assert result['reason'] == '决策置信度低于纠偏阈值'


def test_purely_negative_posterior_scenarios_require_correction(monkeypatch):
    point_model(monkeypatch)
    POLICY['ERRORS']['MODEL']['metrics']['N']['1'] = {'-2': 1.}
    current = state(N=2, H=8, W=4)
    choices = [option(1, N=1)]
    assert API['semantic_deadlock'](current, CAPS, choices) is None
    result = API['decision_confidence'](current, CAPS, choices, context(), 1)
    assert result['confidence'] == 0 and result['safe_fraction'] == 1
    assert result['reason'] == '决策置信度低于纠偏阈值'


def test_deterministic_integer_posterior_keeps_a_stable_recommendation(monkeypatch):
    point_model(monkeypatch)
    current = state(L=2, S=0, O=0, N=0, H=8, D=8, W=4)
    choices = [option(1, S=1), option(2, N=1)]
    result = API['decision_confidence'](current, CAPS, choices, context(), 1)
    assert result == {'confidence': 1., 'safe_fraction': 1., 'samples': 32, 'reason': None}


def test_runtime_confidence_is_repeatable_and_does_not_touch_global_randomness():
    import random
    current = state(L=2, S=16, O=5, N=4, H=8, W=4)
    choices = [option(1, S=1), option(2, N=1)]
    before = random.getstate()
    a = API['decision_confidence'](current, CAPS, choices, context(), 1)
    b = API['decision_confidence'](current, CAPS, choices, context(), 1)
    assert a == b and random.getstate() == before
    assert 0 <= a['confidence'] <= 1


def test_official_delta_stays_exact_inside_a_mixed_menu(monkeypatch):
    point_model(monkeypatch)
    POLICY['ERRORS']['MODEL']['metrics']['S']['1'] = {'-2': 1.}
    options = [dict(option(1, S=1), certain=True), option(2, S=1)]
    scenarios = list(POLICY['ERRORS']['posterior_scenarios'](options))
    assert len(scenarios) == 32
    assert all(s[0]['metrics']['S'] == 1 and s[1]['metrics']['S'] == -1 for s in scenarios)


def test_hand_quantified_out_of_domain_risk_keeps_its_raw_magnitude():
    options = [option(1,R=4),option(2,R=5)]
    scenarios = list(POLICY['ERRORS']['posterior_scenarios'](options))
    assert all(s[0]['metrics']['R']==4 and s[1]['metrics']['R']==5 for s in scenarios)


def test_unseen_event_identity_and_text_preserve_recommendation_and_uncertainty():
    """Synthetic metric vectors, without loading any local event-pool records."""
    from copy import deepcopy

    current = state(L=6, S=134, O=21, N=28, H=3, D=8, W=13)
    choices = [option(1, S=-1, O=-1, N=-1, H=2),
               option(2, N=2), option(3, S=-1, N=-1, H=1, W=1)]
    ctx = context(month=37, duration=2)
    raw = POLICY['_choose_nominal'](current,CAPS,choices,ctx,API['_policy_helpers']())
    raw_assessment = API['decision_confidence'](current,CAPS,choices,ctx,raw)
    assert 0 < raw_assessment['confidence'] < .5
    selected = POLICY['choose'](current, CAPS, choices, ctx, API['_policy_helpers']())
    selected_index = next(i for i,o in enumerate(choices) if o['choice']==selected)
    expected = API['decision_confidence'](current, CAPS, choices, ctx, selected)
    assert expected['samples'] in (32, 128) and 0 < expected['confidence'] <= 1
    before = deepcopy(choices)
    for event_id, text in [('unseen-next-year-event', '从未出现的新行业场景'),
                           ('another-new-pool:42', '与原事件完全不同的项目任务')]:
        decorated = [dict(o, text=f'{text}，选项{o["choice"]}') for o in choices]
        changed = dict(ctx, event_id=event_id, current_event={'description': text})
        winner = POLICY['choose'](current, CAPS, decorated, changed, API['_policy_helpers']())
        assert winner == selected
        assert API['decision_confidence'](current, CAPS, decorated, changed, winner) == expected
        assert API['decision_confidence'](current, CAPS, list(reversed(decorated)), changed, winner) == expected
    assert choices == before

    # New menus may renumber the same metric vectors. Small quadrature changes
    # must not change the classification of this clearly uncertain example.
    from itertools import permutations
    for numbers in permutations((1, 2, 3)):
        renumbered = [dict(o, choice=number) for o, number in zip(choices, numbers)]
        winner = POLICY['choose'](current, CAPS, renumbered, ctx, API['_policy_helpers']())
        assert winner == numbers[selected_index]
        assessment = API['decision_confidence'](current, CAPS, renumbered, ctx, winner)
        assert assessment['samples'] in (32, 128)
        assert assessment == expected


def test_stratified_scenarios_cover_each_conditional_pmf_without_event_examples():
    from collections import Counter

    for predicted in range(-3, 4):
        menu = [option(1, **dict.fromkeys('ONSHWR', predicted))]
        scenarios = list(POLICY['ERRORS']['posterior_scenarios'](menu))
        for metric, cells in POLICY['ERRORS']['MODEL']['metrics'].items():
            counts = Counter(s[0]['metrics'][metric]-predicted for s in scenarios)
            for error, probability in cells[str(predicted)].items():
                assert abs(counts[int(error)]/32-probability) <= 1/32 + 1e-12


def test_distinct_prediction_vectors_keep_their_scenarios_after_menu_renumbering():
    from itertools import permutations

    menu = [option(1, S=1), option(2, N=2), option(3, H=1, W=-1)]
    original = list(POLICY['ERRORS']['posterior_scenarios'](menu))
    for numbers in permutations((1, 2, 3)):
        changed = [dict(o, choice=n) for o, n in zip(menu, numbers)]
        changed.reverse()
        actual = list(POLICY['ERRORS']['posterior_scenarios'](changed))
        for before, after in zip(original, actual):
            by_number = {o['choice']: o['metrics'] for o in after}
            assert [by_number[n] for n in numbers] == [o['metrics'] for o in before]


def test_general_metric_menus_keep_confidence_after_relabeling_and_reordering():
    import random

    rng = random.Random(10906)
    for i in range(24):
        level = rng.randrange(2, 8)
        req = API['PROMOTION_REQUIREMENTS'][level]
        current = state(L=level, H=rng.randrange(3, 11), W=rng.randrange(2, 15),
                        **{m: rng.randrange(0, int(v*1.2)+1) for m, v in req.items()})
        menu = [option(j, **{m: rng.randrange(-1, 3) for m in 'SONHW'}) for j in range(1, 4)]
        ctx = context(month=rng.randrange(1, 49), duration=6, story_actions=rng.randrange(3))
        selected = POLICY['choose'](current, CAPS, menu, ctx, API['_policy_helpers']())
        expected = API['decision_confidence'](current, CAPS, menu, ctx, selected)
        numbers = rng.sample((5, 11, 36), 3)
        renamed = [dict(o, choice=n, text=f'未见事件{i}的选项') for o, n in zip(menu, numbers)]
        rng.shuffle(renamed)
        mapped = None if selected is None else numbers[selected-1]
        actual = API['decision_confidence'](current, CAPS, renamed, dict(ctx, event_id=f'new:{i}'), mapped)
        assert actual == expected


def test_identical_predicted_options_have_exchangeable_confidence():
    current = state(L=2, S=0, O=0, N=0, H=8, W=4)
    menu = [option(1, S=1), option(2, S=1), option(3, N=1)]
    a = API['decision_confidence'](current, CAPS, menu, context(), 1)
    b = API['decision_confidence'](current, CAPS, menu, context(), 2)
    assert a == b
