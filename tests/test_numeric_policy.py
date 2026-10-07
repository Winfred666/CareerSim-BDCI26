"""Fixed decisions retain redlines and review rules without a terminal scorer."""
from pathlib import Path
import random
import runpy
import json

import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'solution/skills/observe-decide-review/scripts'
API=runpy.run_path(str(SCRIPTS/'read-context.py'))
POLICY=API['POLICY']
HELPERS={'project':API['projected_status'],'guard':API['redline_candidates'],
         'safety':API['redline_score'],'greedy':API['_greedy_guidance'],
         'requirements':API['PROMOTION_REQUIREMENTS']}
CAPS={l:(0,{m:int(v*1.2)for m,v in req.items()})for l,req in API['PROMOTION_REQUIREMENTS'].items()}
CAPS[10]=(0,dict(S=None,O=None,N=None))

def state(**kw):
    return dict(L=6,S=170,O=45,N=60,H=8,D=10,W=20,R=0)|kw

def option(choice,**delta):
    return {'choice':choice,'metrics':dict.fromkeys('SONHDWR',0)|delta}

def context(**kw):
    return dict(month=47,kind='story',duration=12,story_actions=0,energy=3,energy_actions=0)|kw


def net(current, ctx, **delta):
    changes=option(1,**delta)['metrics']
    after=dict(API['projected_status'](current,CAPS,changes),L=current['L'])
    return POLICY['decision_value'](current,after,ctx,HELPERS,changes)


@pytest.mark.parametrize('metric',list('SONH'))
def test_capped_positive_predictions_have_zero_net_value(metric):
    current=state(S=204,O=54,N=72,H=10)
    assert net(current,context(month=40),**{metric:3})==0


@pytest.mark.parametrize('metric',list('SON'))
def test_promotion_met_positive_value_is_lower_and_imminent_loss_is_higher(metric):
    ready=state()
    lacking=dict(ready,**{metric:ready[metric]-3})
    assert net(lacking,context(month=40),**{metric:1}) > net(ready,context(month=40),**{metric:1})
    assert net(ready,context(month=41),**{metric:-1}) < net(ready,context(month=40),**{metric:-1})


def test_risk_reduction_is_rewarded_and_single_increment_is_costly():
    current=state(R=1)
    assert net(current,context(month=40),R=-1)==pytest.approx(1.5)
    assert net(current,context(month=40),R=1)==pytest.approx(-6)
    assert net(state(R=0),context(month=40),R=-1)==0


@pytest.mark.parametrize('risk',[None,0,1,2])
def test_risk_double_increment_is_costly_but_remains_selectable(risk):
    current=state(R=risk,S=140,H=7)
    choices=[option(1,R=2,S=3,H=3,W=3),option(2,N=1)]
    assert API['redline_candidates'](current,choices)==choices
    assert POLICY['_choose_nominal'](current,CAPS,choices,context(month=40),HELPERS)==2
    # With exact effects the finite risk penalty retains the original choice;
    # uncertain R++ may be an overcall and is assessed by the error model.
    exact=[dict(o,certain=True) for o in choices]
    assert POLICY['choose'](current,CAPS,exact,context(month=40),HELPERS)==2
    assert POLICY['choose'](current,CAPS,choices[:1],context(month=40),HELPERS)==1
    assert net(current,context(month=40),R=2)==pytest.approx(-2*POLICY['VALUE']['risk_increase'])
    assert API['semantic_deadlock'](current,CAPS,choices[:1]) is None


def test_soft_risk_cannot_trade_away_a_low_health_prohibition():
    current=state(H=3,S=140)
    choices=[option(1,H=-1,S=3,R=-2),option(2,R=2,N=1)]
    assert API['redline_candidates'](current,choices)==choices[1:]
    assert POLICY['choose'](current,CAPS,choices,context(month=40),HELPERS)==2


def test_soft_risk_does_not_remove_predicted_second_burst_abstention():
    current=state(R=4)
    choices=[option(1,R=2,S=3)]
    assert '淘汰' in API['semantic_deadlock'](current,CAPS,choices,context(month=40,risk_bursts=1))
    assert API['decision_confidence'](current,CAPS,choices,context(month=40,risk_bursts=1),1)['confidence']==0


def test_late_health_prices_both_directions_and_respects_the_cap():
    current=state(H=8)
    assert net(current,context(month=46),H=1)==pytest.approx(.75)
    assert net(current,context(month=47),H=1)==pytest.approx(.9375)
    assert net(current,context(month=47),H=-1)==pytest.approx(-.9375)
    assert net(state(H=10),context(month=47),H=2)==0


def test_net_uses_effective_rank_scaled_cash_loss():
    # Official L7 loss is -4; aversion prices that effective loss once.
    assert net(state(L=7),context(month=1),W=-1)==pytest.approx(-1)
    assert net(state(L=7),context(month=48),W=-1)==pytest.approx(-1.5)


@pytest.mark.parametrize('level',[1,6,7,10])
def test_positive_wealth_keeps_fixed_value_at_every_time_and_rank(level):
    for month in (1,24,47,48):
        assert net(state(L=level,S=1,O=1,N=1),context(month=month),W=3)==pytest.approx(.75)


def test_wealth_loss_aversion_increases_with_time_without_terminal_score_caps():
    current=state(L=7)
    losses=[net(current,context(month=month),W=-1) for month in (1,24,48)]
    assert losses[0]>losses[1]>losses[2]
    assert net(state(L=7,W=85),context(month=48),W=-1)==pytest.approx(-1.5)
    assert net(state(L=7,W=80),context(month=48),W=3)==pytest.approx(.75)


def test_final_fixed_recovery_reserves_story_choices_for_cash_but_keeps_health_guard():
    choices=[option(1,H=1),option(2,H=-1,W=3)]
    ctx=context(month=47,story_actions=2)
    assert POLICY['choose'](state(W=30),CAPS,choices,ctx,HELPERS)==2
    assert POLICY['choose'](state(W=30,H=3),CAPS,choices,ctx,HELPERS)==1


def test_final_fixed_recovery_cannot_override_next_review_survival():
    current=state(L=5,S=100,O=6,N=50,R=0,H=8,D=10)
    ctx=context(month=47,bad_reviews=1,story_actions=0)
    choices=[option(1,O=3),option(2,O=-1,W=3)]
    alive=[POLICY['review'](dict(API['projected_status'](current,CAPS,o['metrics']),L=5),
                           CAPS,ctx,HELPERS)[2] for o in choices]
    assert alive==[True,False]
    assert POLICY['choose'](current,CAPS,choices,ctx,HELPERS)==1


def test_no_future_promotion_premium_after_month_48():
    assert net(state(S=150),context(month=48),S=3)==pytest.approx(.75)
    assert net(state(O=30),context(month=48),O=3)==0
    assert net(state(S=150),context(month=40),S=3)>net(state(S=150),context(month=48),S=3)


@pytest.mark.parametrize('level',[6,10])
def test_last_fixed_budget_does_not_buy_output_after_the_final_review(level):
    current=state(L=level,O=30,H=9)
    choices=[option(1,O=3),option(2,H=1)]
    assert POLICY['choose'](current,CAPS,choices,context(month=48),HELPERS)==2
    assert net(current,context(month=48),O=3)==0


def test_rank_without_another_promotion_has_no_performance_buffer_lookup():
    choices=[option(1,H=2),option(2,O=3)]
    assert POLICY['choose'](state(L=10),CAPS,choices,context(month=5),HELPERS)==1


@pytest.mark.parametrize('month', [1,5,23,47,48])
@pytest.mark.parametrize('metric,value', [('H',1),('H',3),('D',1),('D',3),('W',-2),('W',0),('W',1),('W',2)])
def test_every_phase_preserves_all_low_metric_prohibitions(month,metric,value):
    current=state(**{metric:value})
    choices=[option(1,**{metric:-1,'S':3,'N':3}),option(2)]
    assert POLICY['choose'](current,CAPS,choices,context(month=month),HELPERS)==2
    assert metric in API['low_redlines'](current)


def test_all_forbidden_stays_without_a_recommendation():
    current=state(H=3,W=2)
    choices=[option(1,H=-1,S=3),option(2,W=-1,N=3)]
    assert POLICY['choose'](current,CAPS,choices,context(),HELPERS) is None
    assert '推荐选项' not in API['decision_guidance'](current,CAPS,choices,context())


def test_negative_menu_minimizes_fixed_net_loss_instead_of_raw_metric_cost():
    current=state(L=2,S=10,O=4,N=4,H=8,D=8)
    choices=[option(1,N=-2),option(2,H=-1)]
    ctx=context(month=13)
    assert API['_greedy_guidance'](current,CAPS,choices)['推荐选项']==2
    assert POLICY['choose'](current,CAPS,choices,ctx,HELPERS)==1
    losses=[POLICY['decision_value'](current,dict(API['projected_status'](current,CAPS,o['metrics']),L=2),
                                   ctx,HELPERS,o['metrics'],promotion=False) for o in choices]
    assert losses==pytest.approx([-.5,-.75])


def test_least_loss_uses_rank_scaled_wealth_deductions():
    current=state(L=7,S=210,O=60,N=50)
    choices=[option(1,N=-3),option(2,W=-1)]
    ctx=context(month=37)
    after=API['projected_status'](current,CAPS,choices[1]['metrics'])
    assert after['W']==16  # L7 turns the nominal -1 into an actual -4.
    assert POLICY['choose'](current,CAPS,choices,ctx,HELPERS)==1
    costs=[POLICY['decision_value'](current,dict(API['projected_status'](current,CAPS,o['metrics']),L=7),
                                  ctx,HELPERS,o['metrics'],promotion=False) for o in choices]
    assert costs[1]<costs[0]


def test_capped_positive_predictions_with_losses_choose_the_best_net_value():
    current=state(L=2,S=21,O=6,N=7,H=10,D=10)
    choices=[option(1,S=3,H=-2),option(2,N=2,D=-1)]
    ctx=context(month=13)
    assert API['semantic_deadlock'](current,CAPS,choices) is None
    assert POLICY['choose'](current,CAPS,choices,ctx,HELPERS)==2


def test_least_loss_cannot_override_an_existing_low_health_prohibition():
    current=state(L=2,S=10,O=4,N=4,H=3)
    choices=[option(1,H=-1),option(2,N=-3)]
    assert POLICY['choose'](current,CAPS,choices,context(month=13),HELPERS)==2


def test_least_loss_preserves_imminent_review_survival_before_net_score():
    current=state(L=2,S=18,O=2,N=0,H=8,D=8)
    choices=[option(1,O=-1),option(2,H=-1)]
    ctx=context(month=23,story_actions=2,bad_reviews=1)
    alive=[POLICY['review'](dict(API['projected_status'](current,CAPS,o['metrics']),L=2),
                            CAPS,ctx,HELPERS)[2] for o in choices]
    assert alive==[False,True]
    assert POLICY['choose'](current,CAPS,choices,ctx,HELPERS)==2


def test_capped_gain_net_value_includes_the_earned_promotion():
    current=state(L=1,S=9,O=3,N=3,H=8,D=10)
    choices=[option(1,S=1,N=-1),option(2,S=1,H=-1)]
    ctx=context(month=1,duration=1)
    immediate=[POLICY['decision_value'](current,dict(API['projected_status'](current,CAPS,o['metrics']),L=1),
                                      ctx,HELPERS,o['metrics'],promotion=False) for o in choices]
    assert immediate[0]>immediate[1]
    assert POLICY['choose'](current,CAPS,choices,ctx,HELPERS)==2


@pytest.mark.parametrize('month',[1,5,47,48])
def test_risk_priority_is_preserved_before_score_or_promotion(month):
    current=state(R=2,S=140)
    choices=[option(1,H=2,S=3,N=3),option(2,R=-1)]
    assert POLICY['choose'](current,CAPS,choices,context(month=month),HELPERS)==2


def test_w2_guard_changes_the_original_crossing_case():
    current=state(L=1,S=8,O=3,N=3,H=5,D=8,W=2,R=2)
    choices=[option(1,N=-2),option(4,N=1,W=-2)]
    assert API['redline_candidates'](current,choices)==[choices[0]]
    assert API['LOW_THRESHOLDS']['W']==2
    assert '“个人财富”过低' in API['decision_hints'](current,CAPS)['守红线']


def test_last_month_harvests_health_instead_of_unscored_extra_skill():
    choices=[option(1,S=3),option(2,H=2)]
    assert POLICY['choose'](state(S=150),CAPS,choices,context(month=48),HELPERS)==2


def test_complete_performance_gate_beats_health_when_son_already_pass():
    choices=[option(1,H=2),option(2,O=1)]
    assert POLICY['performance'](state(),48)==5
    assert POLICY['choose'](state(),CAPS,choices,context(month=47),HELPERS)==2


def test_performance_matches_the_official_formula():
    from career_emulator.failure_conditions import field_value
    from career_emulator.server.models import CareerState
    rng=random.Random(521)
    for _ in range(1200):
        current=state(L=rng.randint(1,10),S=rng.randint(0,300),O=rng.randint(-1,100),N=rng.randint(0,150))
        month=rng.choice([6,12,18,48]);adjustment=rng.choice([-2,0,2])
        career=CareerState(level=f"L{current['L']}",skill=current['S'],output=current['O'],network=current['N'],
                           current_month=month,statistics={'performance_score_adjustment':adjustment})
        assert POLICY['performance'](current,month,adjustment)==field_value(career,'performance_score')


def test_terminal_objective_is_absent_from_every_decision_path(monkeypatch):
    assert 'ending_value' not in POLICY
    def forbidden(*args,**kwargs):
        raise AssertionError('a terminal objective must never enter decision making')
    monkeypatch.setitem(POLICY['choose'].__globals__,'ending_value',forbidden)
    monkeypatch.setitem(POLICY['ERRORS']['equal_prediction_choice'].__globals__,'ending_value',forbidden)
    for kind,month in [('story',1),('story',47),('energy',48),('main',48)]:
        ctx=context(month=month,kind=kind,story_actions=2)
        if kind=='energy':
            choices=[dict(option(i+1,**d),energy_cost=c) for i,(c,d) in enumerate(POLICY['ENERGY'])]
        elif kind=='main':choices=[option(i+1,**d) for i,d in enumerate(POLICY['MAIN'])]
        else:choices=[option(1,N=1),option(2,H=1)]
        assert type(POLICY['choose'](state(),CAPS,choices,ctx,HELPERS)) is int
    # Equal capped predictions also exercise the uncertainty tie path.
    current=state(L=1,S=9,O=3,N=3)
    assert type(POLICY['choose'](current,CAPS,[option(1,N=1),option(2,N=2)],context(month=1),HELPERS)) is int


def test_terminal_grade_caps_have_no_effect_on_fixed_increment_values():
    for wealth in (79,80,85):
        assert net(state(W=wealth),context(month=48),W=1)==pytest.approx(.25)
    for skill in (119,120,150):
        assert net(state(S=skill),context(month=48),S=1)==pytest.approx(.25)


@pytest.mark.parametrize('metric',list('SON'))
def test_surplus_positive_gains_taper_before_known_non_l1_caps(metric):
    required=API['PROMOTION_REQUIREMENTS'][6][metric]
    cap=CAPS[6][1][metric]
    samples=[required,(required+cap)//2,cap-1,cap]
    gains=[net(state(**{metric:value}),context(month=40),**{metric:1}) for value in samples]
    assert gains[0]>gains[1]>gains[2]>gains[3]==0
    assert gains[2]>0


@pytest.mark.parametrize('metric',list('SON'))
def test_cap_taper_preserves_required_gains_and_all_negative_prices(metric):
    required=API['PROMOTION_REQUIREMENTS'][6][metric]
    cap=CAPS[6][1][metric]
    lacking=state(**{metric:required-1})
    assert net(lacking,context(month=40),**{metric:1})==pytest.approx(6)
    for value in (required,(required+cap)//2,cap):
        current=state(**{metric:value})
        after=dict(API['projected_status'](current,CAPS,{metric:-1}),L=6)
        assert POLICY['surplus_increment'](current,after,metric,required)==-1


def test_l1_gain_keeps_its_buffer_value_without_taper():
    current=state(L=1,S=8,O=3,N=3)
    assert net(current,context(month=1),S=1)==pytest.approx(.25)


def history(tmp_path,events,log=''):
    notes=tmp_path/'notes';notes.mkdir()
    translation=runpy.run_path(str(SCRIPTS/'translation-state.py'))
    logpath=tmp_path/'test.log';logpath.write_text(f'Log file: {logpath}\n'+log)
    for number,month,choices,selected,risk_delta,entry in events:
        task={'session_id':'test','event_id':f'{number:05d}','choices':choices}
        d=translation['event_dir'](notes,task);d.mkdir(parents=True)
        for name,value in [('task.json',task),('public-event.json',{'current_month':month}),
                           ('action.json',{'choice':selected,'log':{'file':str(logpath),'entry':entry}})]:
            (d/name).write_text(json.dumps(value))
        (d/'R').mkdir();(d/'R/complete.json').write_text(json.dumps({'values':{str(selected):{'R':risk_delta}}}))
    return notes,translation


def test_public_receipts_track_month_counts_and_risk_without_private_state(tmp_path):
    story=[{'choice':1,'action':'public'}]
    notes,t=history(tmp_path,[(1,47,story,1,2,0),(2,47,story,1,-1,1)])
    task={'session_id':'test','event_id':'00003','choices':story}
    obs={'current_state':{'time':{'current_month':47},'status':{'duration_in_level':6,'energy':0}}}
    ctx,risk=API['decision_context'](notes,task,t,obs,0)
    assert ctx['story_actions']==2 and risk==1
    obs['current_state']['time']['current_month']=48
    ctx,risk=API['decision_context'](notes,task,t,obs,2)
    assert ctx['story_actions']==0 and risk==2


def test_public_warnings_reset_risk_and_count_reviews_once(tmp_path):
    story=[{'choice':1,'action':'public'}]
    log='[salary] HR风险提示\n[warning] 隐患好巧不巧一起炸了\n[warning] 不妙的半年绩效\n[action] first\n[action] second\n'
    notes,t=history(tmp_path,[(1,47,story,1,3,3),(2,47,story,1,1,4)],log)
    task={'session_id':'test','event_id':'00003','choices':story}
    obs={'current_state':{'time':{'current_month':47},'status':{'duration_in_level':6,'energy':0}}}
    ctx,risk=API['decision_context'](notes,task,t,obs,2)
    assert risk==1 and ctx['risk_bursts']==1 and ctx['bad_reviews']==1
    assert API['decision_context'](notes,task,t,obs,2)==(ctx,risk)


def test_quarter_combo_returns_one_step_and_replans_using_fixed_priorities():
    current=state(S=150,H=7,D=8,R=2)
    origin=dict(current)
    ctx=context(month=48,kind='energy')
    menu=[dict(choice=i+1,energy_cost=cost,metrics=dict.fromkeys('SONHDWR',0)|delta)
          for i,(cost,delta) in enumerate(POLICY['ENERGY'])]
    menu.append(dict(choice=8,energy_cost=0,metrics=dict.fromkeys('SONHDWR',0)))
    # Replan the known combination, preserving every intermediate guard.
    recommendation=POLICY['choose'](current,CAPS,menu,ctx,HELPERS)
    assert type(recommendation) is int and recommendation in range(1,8)
    global_plan=POLICY['quarter_best'](current,CAPS,ctx,HELPERS,menu)
    assert recommendation==global_plan[1]
    for _ in range(3):
        available=[o for o in menu if o['energy_cost']<=ctx['energy']]
        choice=POLICY['choose'](current,CAPS,available,ctx,HELPERS)
        if choice==8:break
        selected=next(o for o in available if o['choice']==choice)
        assert selected in API['redline_candidates'](current,available)
        current=dict(API['projected_status'](current,CAPS,selected['metrics']),L=current['L'])
        ctx['energy']-=selected['energy_cost'];ctx['energy_actions']+=1
        if ctx['energy']==0:break
    ctx['kind']='main'
    main=[option(i+1,**delta)for i,delta in enumerate(POLICY['MAIN'])]
    choice=POLICY['choose'](current,CAPS,main,ctx,HELPERS)
    current=dict(API['projected_status'](current,CAPS,main[choice-1]['metrics']),L=current['L'])
    assert current['R']<=1
    assert POLICY['quarter_key'](origin,current,CAPS,ctx,HELPERS)==POLICY['quarter_key'](origin,global_plan[2],CAPS,ctx,HELPERS)
    assert current['H']==10 and current['D']>=8


def test_final_review_uses_cash_when_output_cannot_complete_performance_gate():
    current=state(S=170,O=44,N=60,H=8,D=10)
    choices=[option(1,O=1,H=-2),option(2,H=2,W=2)]
    after=dict(API['projected_status'](current,CAPS,choices[0]['metrics']),L=6)
    assert POLICY['performance'](after,48)==5  # L6 requires performance >=6.
    assert POLICY['choose'](current,CAPS,choices,context(story_actions=2),HELPERS)==2


def test_terminal_quarter_does_not_predict_a_review_after_month_48():
    current=state(L=1,S=9,O=3,N=3,H=10,D=10,W=10)
    key=POLICY['quarter_key'](current,current,CAPS,context(month=48,bad_reviews=1),HELPERS)
    assert key[1] is True
    assert key[2]==0


def test_review_buffer_prices_risk_instead_of_tying_it_with_one_skill_point():
    current=state(L=1,S=9,O=3,N=3,H=5,D=10,W=2,R=0)
    choices=[option(1,S=-1,O=2),option(3,S=1,N=2,R=1)]
    assert API['_greedy_guidance'](current,CAPS,choices)['推荐选项']==3
    ctx=context(month=5,duration=5,story_actions=1)
    # Both pass the complete review, but retaining R=0 loses only 0.125 score.
    assert all(POLICY['review'](dict(API['projected_status'](current,CAPS,o['metrics']),L=1),
                                CAPS,ctx,HELPERS)[1] for o in choices)
    assert POLICY['choose'](current,CAPS,choices,ctx,HELPERS)==1


def test_dignity_delta_can_block_promotion_even_with_son_at_their_caps():
    current=state(S=204,O=54,N=72,H=6,D=4,W=30)
    choices=[option(1,H=2,D=-2),option(2,D=1)]
    assert POLICY['choose'](current,CAPS,choices,context(month=41),HELPERS)==2
    omitted=[dict(o,metrics=dict(o['metrics'],D=0)) for o in choices]
    assert POLICY['choose'](current,CAPS,omitted,context(month=41),HELPERS)==1


def test_l1_cap_does_not_guarantee_survival_after_the_protection_period():
    current=state(L=1,S=9,O=3,N=3,H=10,D=10,R=1)
    assert POLICY['performance'](current,18)==3
    after,promoted,alive=POLICY['review'](current,CAPS,context(month=17,duration=17),HELPERS)
    assert not promoted and alive and after['review_performance']==1
    assert not POLICY['review'](current,CAPS,context(month=17,duration=17,bad_reviews=1),HELPERS)[2]
