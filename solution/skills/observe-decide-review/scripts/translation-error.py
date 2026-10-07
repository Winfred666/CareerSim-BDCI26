"""Conditional error quadrature and resolution of equal nominal predictions."""
import json
from pathlib import Path
import random

MODEL = json.loads(Path(__file__).with_name('translation-errors.json').read_text())


def _canonical_options(options, selected=None):
    return sorted(options, key=lambda o: (
        tuple(o['metrics'].get(m, 0) for m in 'SONHDWR'),
        o.get('energy_cost', 0), bool(o.get('certain')),
        o['choice'] != selected if selected is not None else False, o['choice']))


def posterior_scenarios(options, samples=32, selected=None):
    """Fixed metric-ordered quadrature, independent of event text/menu numbering."""
    draws = {}
    # Different events can present the same vectors in a different order. Give
    # each vector a canonical stream; choice IDs only order identical vectors.
    ordered = _canonical_options(options, selected)
    for ordinal, option in enumerate(ordered):
        rng = random.Random(314159 + ordinal)
        metrics = {}
        if option.get('certain'):
            draws[option['choice']] = metrics
            continue
        for metric, cells in MODEL['metrics'].items():
            predicted = option['metrics'][metric]
            # Hand-quantified effects can exceed the questionnaire's domain.
            # Retain such raw values instead of extrapolating a fitted error.
            pmf = sorted((int(e), p) for e, p in cells.get(str(predicted), {'0':1.}).items())
            grid = [(i+.5)/samples for i in range(samples)]
            rng.shuffle(grid)
            values = []
            for quantile in grid:
                total = 0.
                for error, probability in pmf:
                    total += probability
                    if quantile < total:
                        values.append(predicted+error)
                        break
            metrics[metric] = values
        draws[option['choice']] = metrics
    for i in range(samples):
        yield [dict(option, metrics=dict(option['metrics'], **{
            m: values[i] for m, values in draws[option['choice']].items()})) for option in options]


def decision_stability(state, caps, options, selected, context, helpers, choose, review, samples=32):
    """Model agreement with a recommendation, including equal outcomes as agreement.

    O/N/S/H/W/R increments use signed-amplitude conditional errors. Current
    state and unmodeled D remain fixed. Agreement measures symbolic stability,
    not the probability that the semantic Leader will correct an event.
    """
    agreements, safe = 0, 0
    imminent = context['month']%6 == 5 and context.get('story_actions', 0) == 2
    # Count the recommendation if it remains optimal, including strategy ties.
    # Giving it first tie priority removes arbitrary menu IDs from confidence;
    # this does not change the nominal action sent to the real environment.
    aliases = {selected: 1}
    aliases.update((o['choice'], i+2) for i, o in enumerate(
        o for o in _canonical_options(options) if o['choice'] != selected))
    for scenario in posterior_scenarios(options, samples, selected):
        option = next(o for o in scenario if o['choice'] == selected)
        after = dict(helpers['project'](state, caps, option['metrics']), L=state['L'])
        alive = after['H'] > 0 and not (after['R'] is not None and after['R'] >= 5
                                        and context.get('risk_bursts', 0) > 0)
        if imminent:
            alive = alive and review(after, caps, context, helpers)[2]
        # Preserve the original low-metric prohibitions inside each scenario.
        permitted = option in helpers['guard'](state, scenario)
        safe += bool(alive and permitted)
        if not alive or not permitted:
            continue
        # Purely negative raw events need correction even if the script can pick
        # a least-loss choice. Capped raw gains do not cause this abstention.
        if helpers.get('deadlock') and helpers['deadlock'](state, caps, scenario, context):
            continue
        canonical = [dict(o, choice=aliases[o['choice']]) for o in scenario]
        winner = choose(state, caps, canonical, context, helpers)
        if winner is None:
            continue
        best = next(o for o in scenario if aliases[o['choice']] == winner)
        equal = (helpers['project'](state, caps, best['metrics']) == {m: after[m] for m in 'SONHDWR'}
                 and best['metrics']['R'] == option['metrics']['R'])
        agreements += winner == 1 or equal
    return {'confidence': agreements/samples, 'safe_fraction': safe/samples, 'samples': samples}


def equal_prediction_choice(state, caps, options, selected, context, helpers, net_value):
    if selected is None or context['kind'] != 'story':
        return selected
    guarded = helpers['guard'](state, options)
    chosen = next(o for o in guarded if o['choice'] == selected)
    target = helpers['project'](state, caps, chosen['metrics'])
    # With unknown current R, equal projected R=None can still hide different
    # risk priorities. Require the same raw R so the original constraint holds.
    tied = [o for o in guarded if helpers['project'](state, caps, o['metrics']) == target
            and o['metrics']['R'] == chosen['metrics']['R']]
    if len(tied) < 2:
        return selected
    base = dict(target, L=state['L'])
    base_net = net_value(state, base, context, helpers, chosen['metrics'])

    def key(option):
        if option.get('certain'):
            probability = float(all(base[m] >= v for m, v in helpers['requirements'].get(state['L'], {}).items()))
            return probability, base_net, -option['choice']
        probability, expected_net = 1., base_net
        for metric, cells in MODEL['metrics'].items():
            predicted = option['metrics'][metric]
            outcomes = [(helpers['project'](state, caps, {metric: predicted+int(error)})[metric], p)
                        for error, p in cells.get(str(predicted), {'0':1.}).items()]
            required = helpers['requirements'].get(state['L'], {}).get(metric)
            if required is not None:
                probability *= sum(p for value, p in outcomes if value >= required)
            # Compare effective increment rules, preserving caps and W scaling.
            expected_net += sum(p*(net_value(state,dict(base, **{metric: value}),context,helpers,option['metrics'])-base_net)
                                  for value, p in outcomes)
        return probability, expected_net, -option['choice']

    return max(tied, key=key)['choice']
