"""Changing takeover frequency must preserve hard guards and cache provenance."""
from pathlib import Path
import runpy

import pytest

from scripts.benchmark_correction_thresholds import (
    ARMS, actions, compact_row, memoized_decisions, threshold_confidence, two_dimensional_arms,
)
from scripts.audit_correction_thresholds import first_divergences


def fake_api(q32,q128,deadlock=None):
    def evaluate(*args,samples=32):
        return dict(confidence=q128 if samples==128 else q32,samples=samples,safe_fraction=1.)
    return {'POLICY':{'ERRORS':{'decision_stability':evaluate},'_choose_nominal':None,'review':None},
            '_policy_helpers':lambda:{},'semantic_deadlock':lambda *args:deadlock}


def test_two_dimensional_grid_contains_every_pair_and_preserves_reusable_names():
    grid=two_dimensional_arms()
    assert len(grid)==36
    values={.6,.65,.7,.75,.8,.9}
    assert {(a['normal'],a['risk']) for a in grid.values()}=={(n,r) for n in values for r in values}
    assert all(grid[name]==settings for name,settings in ARMS.items())


@pytest.mark.parametrize('risk,expected',[(0,False),(1,False),(2,True),(3,True),(None,True)])
def test_independent_risk_cutoff_only_applies_to_elevated_or_unknown_risk(risk,expected):
    result=threshold_confidence(fake_api(.75,.75),.65,.8)({'R':risk},{},[],{'kind':'story'},1)
    assert bool(result['reason'])==expected


@pytest.mark.parametrize('risk',[0,1,2,None])
def test_each_grid_axis_only_changes_its_own_state_branch(risk):
    api=fake_api(.75,.7)
    for settings in two_dimensional_arms().values():
        cutoff=settings['risk'] if risk is None or risk>1 else settings['normal']
        actual=threshold_confidence(api,settings['normal'],settings['risk'])({'R':risk},{},[],{'kind':'story'},1)
        expected=threshold_confidence(api,cutoff,cutoff)({'R':risk},{},[],{'kind':'story'},1)
        assert actual==expected


def test_two_dimensional_audit_can_validate_a_removed_correction():
    base=dict(state={'R':2},context={'kind':'story'},options=[{'choice':1},{'choice':2}],
              actual_deltas={'1':{'R':-1},'2':{'R':1}})
    c=dict(numeric_choice=1,oracle_choice=1,promotion_key_month=True,risk_recovery_conflict=True)
    before={0:dict(trace=[dict(base,choice=2,correction=c)],ending={})}
    after={0:dict(trace=[dict(base,choice=1)],ending={})}
    with pytest.raises(ValueError,match='removed a correction'):
        first_divergences(before,after)
    result=first_divergences(before,after,allow_routing_removal=True)
    assert result['correction_removed_first']==1
    assert result['after_matches_one_step_oracle']==1
    assert result['R_positive_corrected']==1


def test_first_divergence_rejects_changed_answers_when_both_routes_are_corrections():
    base=dict(state={'R':2},context={'kind':'story'},options=[],actual_deltas={})
    before={0:dict(trace=[dict(base,choice=1,correction={'numeric_choice':1})],ending={})}
    after={0:dict(trace=[dict(base,choice=2,correction={'numeric_choice':1})],ending={})}
    with pytest.raises(ValueError,match='switch between script and correction'):
        first_divergences(before,after,allow_routing_removal=True)


def test_shared_support_cache_separates_inputs_and_returns_independent_results():
    calls=[]
    def compute(*args,samples=32):
        calls.append((args[0],args[4],samples))
        return dict(confidence=samples/128,samples=samples,safe_fraction=1.)
    cached=memoized_decisions(compute,stability=True,maxsize=2)
    args=({'R':0},{},[],1,{'month':1},None,None,None)
    first=cached(*args)
    first['reason']='local threshold only'
    assert 'reason' not in cached(*args)
    assert cached(*args,samples=128)['confidence']==1
    assert len(calls)==2
    changed=({'R':2},*args[1:])
    cached(*changed)
    cached(*args)
    assert len(calls)==4  # Eviction recomputes rather than returning a different state.


def test_compact_summary_preserves_action_counts_without_retaining_private_trace():
    row=dict(seed='s',replicate=0,ending=dict(completed=True),final_state=dict(failure_reason=None),
             trace=[dict(context=dict(kind='story'),confidence=dict(samples=128),
                         actual_deltas={'1':{'R':1},'2':{'R':0}},
                         correction=dict(choice=2,numeric_choice=1,raw_guard_choices=[1,2],
                                         promotion_key_month=True,risk_recovery_conflict=True))])
    compact=compact_row(row)
    assert 'trace' not in compact
    assert actions([compact,compact])==actions([row,row])


def test_more_takeover_cutoff_never_revokes_an_existing_route():
    for r in (0,3):
        for i in range(33):
            for j in range(129):
                api=fake_api(i/32,j/128)
                old=threshold_confidence(api,7/16,.5)({'R':r},{},[],{'kind':'story'},1)
                more=threshold_confidence(api,.5,9/16)({'R':r},{},[],{'kind':'story'},1)
                high=threshold_confidence(api,9/16,10/16)({'R':r},{},[],{'kind':'story'},1)
                extreme=threshold_confidence(api,.75,.75)({'R':r},{},[],{'kind':'story'},1)
                leader=threshold_confidence(api,.9,.9)({'R':r},{},[],{'kind':'story'},1)
                assert not old['reason'] or more['reason']
                assert not more['reason'] or high['reason']
                assert not high['reason'] or extreme['reason']
                assert not extreme['reason'] or leader['reason']
                grid=[threshold_confidence(api,a['normal'],a['risk'])({'R':r},{},[],{'kind':'story'},1)
                      for a in ARMS.values()]
                assert not old['reason'] or grid[0]['reason']
                for before,after in zip(grid,grid[1:]):
                    assert not before['reason'] or after['reason']


def test_structural_abstentions_and_fixed_menus_ignore_new_cutoff():
    assess=threshold_confidence(fake_api(1,1,'结构性死局'),.5,9/16)
    result=assess({'R':0},{},[],{'kind':'story'},1)
    assert result==dict(confidence=0.,reason='结构性死局',samples=0)
    assert assess({'R':0},{},[],{'kind':'energy'},1)==dict(confidence=1.,reason=None,samples=0)


def test_benchmark_cutoff_matches_current_production_route():
    root=Path(__file__).resolve().parents[1]
    api=runpy.run_path(str(root/'solution/skills/observe-decide-review/scripts/read-context.py'))
    assess=threshold_confidence(api,.7,.7)
    options=[dict(choice=1,metrics=dict(O=1,N=0,S=1,H=0,D=0,W=0,R=1)),
             dict(choice=2,metrics=dict(O=0,N=1,S=0,H=1,D=0,W=0,R=0))]
    caps={4:(0,dict(S=108,O=26,N=36))}
    for r in (0,1,2,4):
        state=dict(L=4,O=20,N=29,S=89,H=4,D=4,W=3,R=r)
        context=dict(month=5,kind='story',duration=5,story_actions=2,energy=3,energy_actions=0,bad_reviews=0,risk_bursts=0)
        candidate=assess(state,caps,options,context,1)
        original=api['decision_confidence'](state,caps,options,context,1)
        # Diagnostic wording may be revised; the gate and sampled support must match.
        assert bool(candidate['reason'])==bool(original['reason'])
        assert {k:v for k,v in candidate.items() if k!='reason'}=={k:v for k,v in original.items() if k!='reason'}


@pytest.mark.parametrize('risk,correction',[(0,False),(1,False),(2,True),(3,True),(None,True)])
def test_runtime_cutoffs_can_be_configured_independently(risk,correction):
    root=Path(__file__).resolve().parents[1]
    api=runpy.run_path(str(root/'solution/skills/observe-decide-review/scripts/read-context.py'))
    fake=fake_api(.75,.75)
    runtime=api['decision_confidence']
    runtime.__globals__.update(POLICY=fake['POLICY'],semantic_deadlock=fake['semantic_deadlock'],
                              _policy_helpers=fake['_policy_helpers'],
                              NORMAL_CONFIDENCE_THRESHOLD=.65,RISK_CONFIDENCE_THRESHOLD=.8)
    result=runtime({'R':risk},{},[],{'kind':'story'},1)
    assert bool(result['reason'])==correction
    assert result==threshold_confidence(fake,.65,.8)({'R':risk},{},[],{'kind':'story'},1)


@pytest.mark.parametrize('r',[None,0,3])
@pytest.mark.parametrize('q32,q128',[(.7-.15625,.99),(.7-.125,.69),(.7,.7),(.7+.125,.69),(.7+.15625,0.)])
def test_runtime_gate_matches_benchmark_at_sampling_boundaries(r,q32,q128):
    root=Path(__file__).resolve().parents[1]
    api=runpy.run_path(str(root/'solution/skills/observe-decide-review/scripts/read-context.py'))
    fake=fake_api(q32,q128)
    runtime=api['decision_confidence']
    runtime.__globals__.update(POLICY=fake['POLICY'],semantic_deadlock=fake['semantic_deadlock'],
                              _policy_helpers=fake['_policy_helpers'])
    args=({'R':r},{},[],{'kind':'story'},1)
    assert runtime(*args)==threshold_confidence(fake,.7,.7)(*args)
