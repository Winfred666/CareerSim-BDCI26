"""Public review constraints and exact quarterly combinations; return one choice."""
from pathlib import Path
import runpy

ERRORS = runpy.run_path(str(Path(__file__).with_name('translation-error.py')))
STAT_CAPS = runpy.run_path(str(Path(__file__).with_name('refresh-context.py')))['STAT_CAPS']

D_REQUIRED = (0, 2, 3, 3, 4, 4, 5, 5, 6, 7)
P_REQUIRED = (0, 3, 4, 5, 4, 4, 6, 8, 10, 12)
N_PENALTY = (0, 0, 0, 2, 2, 3, 4, 4, 5, 5)
SALARY = (0, .3, .4, .5, .7, 1, 1.5, 2.3, 3.6, 5.7, 9.1)
COEFFICIENTS = (6.88932981e-05, -.0037202381, .0856316138, -1.09791667,
                8.59103009, -42.2239583, 129.157396, -235.174405, 228.665873, -86.)
# Fixed, public menus only. No story ids, text matching or future event pool.
ENERGY = ((2, {'O':2,'H':-1,'D':-1,'R':1}), (2, {'S':1,'H':-1}),
          (1, {'N':1,'D':1}), (2, {'R':-1}), (1, {'D':2}),
          (1, {'H':2}), (1, {'O':1,'D':1}))
MAIN = ({'O':2,'S':1,'N':1}, {'O':1,'N':1,'D':1}, {'O':1,'N':1},
        {'S':2,'N':1}, {'O':1,'D':1}, {'S':1,'N':2}, {'N':1,'D':1}, {'H':1,'R':-1})
VALUE = dict(gap=6., other_gap=3., surplus=.25, imminent_loss=4.,
             risk_reduction=1.5, risk_increase=6., late_health=1.25)
NET_WEIGHTS = dict(H=.75, D=.75, W=.25)


def performance(state, month, adjustment=0):
    low, high = min(state['S'], state['N']), max(state['S'], state['N'])
    level_term = 0.
    for c in COEFFICIENTS:
        level_term = level_term*state['L']+c
    p = round((min(high,2*low)+.3*max(0,high-2*low))/15+state['O']/10+level_term)+adjustment
    return max(4,p) if month<=12 else p


def wealth_loss_price(state, after, context):
    """Time increases aversion to effective cash loss, never positive cash value."""
    loss = max(0,state['W']-after['W'])
    return .25*0.5*max(0,min(1,(context['month']-1)/47))*loss


def surplus_increment(state, after, metric, required):
    """Taper positive buffers to the known cap; required gains/losses stay intact."""
    change = after[metric]-state[metric]
    cap = STAT_CAPS[state['L']].get(metric)
    if state['L']==1 or change<=0 or cap is None:return change
    below = min(after[metric],required)-min(state[metric],required)
    lo,hi = max(required,state[metric]),max(required,after[metric])
    remaining_lo,remaining_hi = max(0,cap-lo),max(0,cap-hi)
    return below+(remaining_lo**2-remaining_hi**2)/(2*max(1,cap-required))


def decision_value(state, after, context, helpers, delta, promotion=True):
    """Fixed rules on effective increments, promotion gaps and asymmetric risk.

    Caps/floors and rank-scaled W have already been applied by project(). These
    common coefficients use only public state/time, never event ids or text.
    """
    value = sum(w*(after[m]-state[m]) for m,w in NET_WEIGHTS.items())-wealth_loss_price(state,after,context)
    req = helpers['requirements'].get(state['L'],{})
    gaps = {m:max(0,v-state[m]) for m,v in req.items()}
    primary = max(gaps,key=gaps.get) if any(gaps.values()) else None
    ready = bool(req) and not any(gaps.values())
    imminent = ready and context['month']%6==5 and context['month']<48
    for m,v in req.items():
        if m=='O' and context['month']>=48:continue  # No further work review can use O.
        change = after[m]-state[m]
        value += VALUE['surplus']*surplus_increment(state,after,m,v)
        if change<0 and imminent:
            value += VALUE['imminent_loss']*change
        elif promotion and context['month']<48:
            needed = gaps[m]-max(0,v-after[m])
            weight = VALUE['gap'] if m==primary or (needed<0 and not ready) else VALUE['other_gap']
            value += (weight-VALUE['surplus'])*needed
    if not req:
        value += VALUE['surplus']*sum(after[m]-state[m] for m in ('SN' if context['month']>=48 else 'SON'))
    if state['R'] is None:
        change = delta.get('R',0)
    else:
        change = after['R']-state['R']
    value += -VALUE['risk_reduction']*change if change<0 else -VALUE['risk_increase']*change
    if context['month']>=47:
        value += .75*(VALUE['late_health']-1)*(after['H']-state['H'])
    return value


def review(state, caps, context, helpers):
    """Forecast the next fixed review; unknown future stories are not simulated."""
    month = 6*(context['month']//6+1)
    level = state['L']
    s = dict(state, R=state['R'] if state['R'] is not None else 1)
    p = performance(s, month)
    bad = p<4
    talk = round(3.5*p+s['D']+.25*s['H']-2*s['R']-4*context.get('risk_bursts',0)-3*bad)
    delta, adjustment = ({'D':2,'N':1,'R':-1},2) if talk>=35 else (({'D':1},0) if talk>=20 else ({'D':-2,'H':-1,'R':1},-2))
    s = dict(helpers['project'](s,caps,delta), L=level)
    p = performance(s,month,adjustment)
    # Bonuses use the old rank, before promotion.
    bonus = 3 if p>=12 else 1.5 if p>=8 else .5 if p>=5 else -1 if p>=3 else -2
    s['W'] += SALARY[level]*(1+bonus)
    req = helpers['requirements'].get(level)
    duration = context.get('duration',1)+month-context['month']
    promoted = bool(req and duration>=6 and s['D']>=D_REQUIRED[level] and p>=P_REQUIRED[level]
                    and all(s[m]>=v for m,v in req.items()))
    if promoted:
        last_promotion = context['month']-context.get('duration',1)+1 if level>1 else 0
        rapid = last_promotion>0 and month-last_promotion<12
        s.update(L=level+1, N=max(0,s['N']-N_PENALTY[level]-3*rapid), D=max(0,s['D']-rapid))
    safe = s['H']>0 and not (s['R']>=5 and context.get('risk_bursts',0)>0) and not (bad and context.get('bad_reviews',0)>0)
    if bad:
        s.update(N=max(0,s['N']-1),D=max(0,s['D']-3))
    s['review_performance'] = p
    return s, promoted, safe


def quarter_key(state, after, caps, context, helpers):
    """Prioritize safety, the known review and gaps, then fixed net increments."""
    thresholds = helpers.get('low_thresholds', {'H':3,'D':3,'W':2})
    low = sum(max(0,v+1-after[m]) for m,v in thresholds.items())
    risk = after['R'] if after['R'] is not None else 1
    safety = (after['H']>0,-max(0,risk-1),-low)
    if context['month']>=48:
        # No reviews exist beyond the simulation's end.
        delta = {m:after[m]-state[m] for m in 'SONHDW'} | {'R':risk-(state['R'] if state['R'] is not None else 1)}
        return safety,after['H']>0 and risk<5,decision_value(state,after,context,helpers,delta,promotion=False)
    forecast,promoted,alive = review(after,caps,context,helpers)
    delta = {m:forecast[m]-state[m] for m in 'SONHDW'} | {'R':forecast['R']-(state['R'] if state['R'] is not None else 1)}
    settled_context = dict(context,month=6*(context['month']//6+1))
    net = decision_value(state,forecast,settled_context,helpers,delta,promotion=False)
    gaps = [max(0,v-after[m]) for m,v in helpers['requirements'].get(after['L'],{}).items()]
    a,b = min(after['S'],after['N']),max(after['S'],after['N'])
    perf = (min(b,2*a)+.3*max(0,b-2*a))/15+after['O']/10
    # Once the last review passes, use the remaining fixed budget for net gains.
    return (safety,alive,promoted,-max(gaps,default=0),-sum(gaps),
            perf if not any(gaps) and not (context['month']>=45 and promoted) else 0,
            net)


def quarter_best(state, caps, context, helpers, first_options=None):
    """Enumerate up to three energy actions plus one main action; keep all redlines."""
    remaining = context.get('energy',3)
    limit = max(0,3-context.get('energy_actions',0))
    candidates = first_options or [dict(choice=i+1,energy_cost=c,metrics=d) for i,(c,d) in enumerate(ENERGY)]
    best, seen = None, set()
    allow_skip = first_options is None or any(o.get('energy_cost',0)==0 for o in first_options)
    def walk(s, energy, count, first):
        nonlocal best
        memo = (tuple(s[m] for m in 'SONHDWR'),energy,count,first)
        if memo in seen:return
        seen.add(memo)
        if first or allow_skip:
            for i,d in enumerate(MAIN,1):
                if not helpers['guard'](s,[{'metrics':d}]):continue
                after = dict(helpers['project'](s,caps,d),L=s['L'])
                value = quarter_key(state,after,caps,context,helpers)
                key = (after['H']>0,value,-count,-first,-i)
                if best is None or key>best[0]:best=(key,first,after)
        if energy<=0 or count>=limit:return
        menu = candidates if count==0 else [dict(choice=i+1,energy_cost=c,metrics=d) for i,(c,d) in enumerate(ENERGY)]
        for o in helpers['guard'](s,menu):
            cost=o.get('energy_cost',0)
            if cost<=0 or cost>energy:continue
            after = dict(helpers['project'](s,caps,o['metrics']),L=s['L'])
            if after['H']<=0 or (after['R'] is not None and after['R']>=5):continue
            walk(after,energy-cost,count+1,first or o['choice'])
    walk(state,remaining,0,0)
    return best


def _choose_nominal(state, caps, options, context, helpers):
    candidates = helpers['guard'](state,options)
    if not candidates:return None
    solvent = [o for o in candidates if helpers['project'](state,caps,o['metrics'])['W']>0]
    candidates = solvent or candidates
    month, kind = context['month'],context['kind']
    projected = {o['choice']:dict(helpers['project'](state,caps,o['metrics']),L=state['L']) for o in candidates}
    if kind=='story' and month%6==5 and month<48:
        previews = {o['choice']:review(projected[o['choice']],caps,context,helpers) for o in candidates}
        survivors = [o for o in candidates if previews[o['choice']][2]]
        if survivors:candidates=survivors
        if month==47 and context.get('story_actions',0)==2:
            def final_review_key(o):
                after,promoted,alive = previews[o['choice']]
                plan_context = dict(context,month=48,energy=3,energy_actions=0)
                plan = quarter_best(after,caps,plan_context,helpers)
                end = plan[2] if plan else after
                end = dict(end,W=end['W']+SALARY[end['L']])
                delta = {m:end[m]-state[m] for m in 'SONHDW'} | {'R':end['R']-(state['R'] if state['R'] is not None else 1)}
                net = decision_value(state,end,plan_context,helpers,delta,promotion=False)
                net += wealth_loss_price(state,end,plan_context)-wealth_loss_price(state,projected[o['choice']],context)
                return alive,promoted,net,-o['choice']
            return max(candidates,key=final_review_key)['choice']
        closing = [o for o in candidates if previews[o['choice']][1]]
        candidates = closing or candidates
    if kind=='energy':
        plan = quarter_best(state,caps,context,helpers,candidates)
        if plan and plan[1]:return plan[1]
        skip = next((o['choice'] for o in candidates if not o.get('energy_cost',0)),None)
        if skip is not None:return skip
    if kind=='main':
        return max(candidates,key=lambda o:(quarter_key(state,projected[o['choice']],caps,context,helpers),-o['choice']))['choice']
    # The existing R/low-metric priorities remain inside the feasible candidates.
    safety = {o['choice']:helpers['safety'](state,projected[o['choice']],o['metrics']) for o in candidates}
    best = max(safety.values())
    candidates = [o for o in candidates if safety[o['choice']]==best]
    req = helpers['requirements'].get(state['L'],{})
    if kind=='story' and all(
            not any(projected[o['choice']][m]>state[m] for m in 'SONHDW')
            and (projected[o['choice']]['R']>=state['R'] if state['R'] is not None else o['metrics']['R']>=0)
            for o in candidates):
        forecast = month<48 and (month%6==5 or bool(req) and all(state[m]>=v for m,v in req.items()))
        def net_key(o):
            after = projected[o['choice']]
            immediate = decision_value(state,after,context,helpers,o['metrics'],promotion=False)
            alive,promoted = True,False
            if forecast:
                settled,promoted,alive = previews[o['choice']] if month%6==5 else review(after,caps,context,helpers)
                delta = {m:settled[m]-after[m] for m in 'SONHDW'} | {'R':settled['R']-(after['R'] if after['R'] is not None else 1)}
                settled_context = dict(context,month=6*(month//6+1))
                immediate += decision_value(after,settled,settled_context,helpers,delta,promotion=False)
            return alive,promoted,immediate,-o['choice']
        return max(candidates,key=net_key)['choice']
    if month>=48 or (month>=42 and state['L']>=5):
        return max(candidates,key=lambda o:(decision_value(state,projected[o['choice']],context,
                       helpers,o['metrics'],promotion=False),-o['choice']))['choice']
    if month%6==5:
        req = helpers['requirements'].get(state['L'],{})
        ready = [o for o in candidates if all(projected[o['choice']][m]>=v for m,v in req.items())]
        if ready and req:
            def buffer_key(o):
                after,promoted,alive = review(projected[o['choice']],caps,context,helpers)
                return (alive,promoted,min(after['review_performance'],P_REQUIRED[state['L']]),
                        decision_value(state,projected[o['choice']],context,helpers,o['metrics']),
                        -o['choice'])
            return max(ready,key=buffer_key)['choice']
    return max(candidates,key=lambda o:(decision_value(state,projected[o['choice']],context,
                       helpers,o['metrics']),-o['choice']))['choice']


def choose(state, caps, options, context, helpers):
    selected = _choose_nominal(state, caps, options, context, helpers)
    if selected is None or context['kind']!='story' or state['R'] is None:return selected
    candidates = helpers['guard'](state,options)
    nonfatal = [o for o in candidates if not (
        state['R'] is not None and context.get('risk_bursts',0)>0
        and helpers['project'](state,caps,o['metrics'])['R']>=5)]
    candidates = nonfatal or candidates
    if all(not any(o['metrics'].get(m,0)>0 for m in 'SONHDW')
           and o['metrics'].get('R',0)>=0 for o in candidates):return selected
    if context['month']%6==5 and context['month']<48:
        survivors = [o for o in candidates if review(
            dict(helpers['project'](state,caps,o['metrics']),L=state['L']),caps,context,helpers)[2]]
        candidates = survivors or candidates
    low = helpers.get('low_thresholds',{'H':3,'D':3,'W':2})
    if state['R']>1 or any(state[m]<=v for m,v in low.items()):
        safety = {o['choice']:helpers['safety'](state,helpers['project'](state,caps,o['metrics']),o['metrics']) for o in candidates}
        safe = [o for o in candidates if safety[o['choice']]==max(safety.values())]
        if any(o['choice']==selected for o in safe):candidates = safe
    req = helpers['requirements'].get(state['L'],{})
    if state['L']==1 and all(state[m]>=v for m,v in req.items()):
        held = [o for o in candidates if all(helpers['project'](state,caps,o['metrics'])[m]>=v for m,v in req.items())]
        if any(o['choice']==selected for o in held):candidates = held
    if context['month']%6==5 and context['month']<48:
        closing = [o for o in candidates if review(
            dict(helpers['project'](state,caps,o['metrics']),L=state['L']),caps,context,helpers)[1]]
        if any(o['choice']==selected for o in closing):candidates = closing
    if len(candidates)==1:return candidates[0]['choice']
    votes = dict.fromkeys((o['choice'] for o in candidates),0)
    memo = {}
    for scenario in ERRORS['posterior_scenarios'](candidates,samples=32):
        # Clipped outcomes may coincide; preserve raw low-metric prohibitions.
        key = tuple((o['choice'],tuple(helpers['project'](state,caps,o['metrics']).values()),
                     bool(helpers['guard'](state,[o]))) for o in scenario)
        if key not in memo:memo[key] = _choose_nominal(state,caps,scenario,context,helpers)
        winner = memo[key]
        if winner is not None:votes[winner] += 1
        if votes.get(selected,0)>16:return selected
        if winner is not None and winner!=selected and votes[winner]>=17:return winner
    winner = max(candidates,key=lambda o:(votes[o['choice']],o['choice']==selected,-o['choice']))['choice']
    return winner if votes[winner]>=16 else selected
