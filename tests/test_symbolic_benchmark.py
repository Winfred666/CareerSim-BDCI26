"""Offline acceleration and paired uncertainty must preserve the official game."""
from pathlib import Path
import json

import pytest

from scripts.benchmark_symbolic_policy import ErrorModel, load_inputs, rollout
from scripts.compare_symbolic_scores import median_interval, paired_summary, read_runs

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_loaded_resources_preserve_actions_settlement_and_game_isolation():
    scripts = ROOT / 'solution/skills/observe-decide-review/scripts'
    inputs = load_inputs(scripts/'numeric-policy.py', scripts/'read-context.py',
                         ROOT/'logs/conditional_model.json', ROOT/'logs/conditional.py')
    resources = {}
    for index in (0, 1, 31, 127):
        seed = f'resource-parity-{index}'
        fresh = await rollout(*inputs, seed, 0, 'historical', True, False)
        cached = await rollout(*inputs, seed, 0, 'historical', True, False, resources=resources)
        assert cached['ending'] == fresh['ending']
        assert [row['choice'] for row in cached['trace']] == [row['choice'] for row in fresh['trace']]
        # Statistics retain randomly selected narrative IDs, not decision inputs.
        assert {k:v for k,v in cached['final_state'].items() if k!='statistics'} == {
            k:v for k,v in fresh['final_state'].items() if k!='statistics'}
        assert cached['steps'] == fresh['steps']


@pytest.mark.asyncio
async def test_official_terminal_grading_cannot_change_numeric_actions(monkeypatch):
    from career_emulator.game import GameEngine
    scripts=ROOT/'solution/skills/observe-decide-review/scripts'
    inputs=load_inputs(scripts/'numeric-policy.py',scripts/'read-context.py',
                      ROOT/'logs/conditional_model.json',ROOT/'logs/conditional.py')
    original=await rollout(*inputs,'grading-isolation',0,'none',True,False)
    get_calculator=GameEngine._get_ending_score_calculator
    class ChangedResult:
        def __init__(self,result):self.result=result
        def __getattr__(self,name):return getattr(self.result,name)
        def to_dict(self):return dict(self.result.to_dict(),quantitative_score=-123)
    class ChangedCalculator:
        def __init__(self,calculator):self.calculator=calculator
        def compute(self,session):return ChangedResult(self.calculator.compute(session))
    async def changed(engine):return ChangedCalculator(await get_calculator(engine))
    monkeypatch.setattr(GameEngine,'_get_ending_score_calculator',changed)
    altered=await rollout(*inputs,'grading-isolation',0,'none',True,False)
    assert altered['ending']['quantitative_score']==-123
    assert original['ending']['quantitative_score']!=-123
    assert [row['choice'] for row in original['trace']]==[row['choice'] for row in altered['trace']]
    assert {k:v for k,v in original['final_state'].items() if k!='statistics'}=={
        k:v for k,v in altered['final_state'].items() if k!='statistics'}


def test_exact_median_interval_handles_small_and_constant_samples():
    assert median_interval(range(1,11)) == [2,9]
    assert median_interval(range(1,11),planned_looks=3) == [1,10]
    assert median_interval([50]*512) == [50,50]
    assert median_interval([50]) == [None,None]
    with pytest.raises(ValueError, match='empty'):
        median_interval([])


def test_unpaired_seeds_cannot_be_reported_as_paired_improvement():
    with pytest.raises(ValueError, match='identical seed'):
        paired_summary({('a',0):{}}, {('b',0):{}})


@pytest.mark.parametrize('mistake,reason', [('repeat','duplicate seed'),('new_policy','mixed policy')])
def test_score_extension_rejects_duplicate_seeds_and_changed_policy(tmp_path, mistake, reason):
    metadata = {name: {'sha256':'same'} for name in
                ('model','conditional','policy','read_context','translation_error','exported_model')}
    metadata['sampling'] = 'historical'
    first = tmp_path/'first.json'
    second = tmp_path/'second.json'
    first.write_text(json.dumps({'metadata':metadata, 'runs':[{'seed':'a','replicate':0}]}))
    if mistake=='new_policy':
        metadata['policy']['sha256'] = 'changed'
    second.write_text(json.dumps({'metadata':metadata,
                                 'runs':[{'seed':'a' if mistake=='repeat' else 'b','replicate':0}]}))
    with pytest.raises(ValueError, match=reason):
        read_runs([first,second])


def test_native_prediction_sampling_uses_true_effect_without_reversing_errors(tmp_path):
    model = tmp_path/'model.json'
    implementation = tmp_path/'conditional.py'
    model.write_text(json.dumps({'metrics': {'R': {
        'emission_pmfs': {'1': {'0': 1}, '4': {'3': 1}, '5': {'2': 1}},
        'prediction_counts': {}}}}))
    implementation.write_text('def distribution(*args): return {"pmf": {0: 1}}\n')
    errors = ErrorModel(model, implementation)
    # Underprediction is sampled directly, including true R beyond questionnaire range.
    for actual, predicted in ((1,0), (4,3), (5,2)):
        assert errors.sample('R', actual, 'calibrated', 'fixed') == predicted
        assert errors.sample('R', actual, 'none', 'fixed') == actual
    with pytest.raises(ValueError, match='no nominal delta'):
        errors.sample('R', 99, 'calibrated', 'fixed')


def test_native_sampler_preserves_original_historical_law(tmp_path):
    original = ErrorModel(ROOT/'logs/conditional_model.json', ROOT/'logs/conditional.py')
    augmented = json.loads((ROOT/'logs/conditional_model.json').read_text())
    augmented['metrics']['N']['emission_pmfs'] = {'1': {'-3': 1}}
    path = tmp_path/'augmented.json'
    path.write_text(json.dumps(augmented))
    native = ErrorModel(path, ROOT/'logs/conditional.py')
    for actual in range(-3,4):
        assert native.prediction_pmf('N', actual, 'historical') == original.prediction_pmf('N', actual, 'historical')
    assert native.sample('N', 1, 'calibrated', 'fixed') == -3
