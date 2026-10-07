#!/usr/bin/env python3
"""Read dynamic decision inputs; stage rules stay in the agent context."""

import json
from pathlib import Path
import re
import runpy
import sys


NOTEBOOKS = Path(__file__).resolve().parent.parent / "notebooks"
REDLINE = runpy.run_path(str(Path(__file__).with_name("refresh-context.py")))
POLICY = runpy.run_path(str(Path(__file__).with_name("numeric-policy.py")))
metric_name = REDLINE["metric_name"]
LOW_THRESHOLDS = REDLINE["LOW_THRESHOLDS"]
NORMAL_CONFIDENCE_THRESHOLD = .7
RISK_CONFIDENCE_THRESHOLD = .7
PROMOTION_REQUIREMENTS = {
    1: {"S": 8, "O": 3, "N": 3},
    2: {"S": 18, "O": 5, "N": 6},
    3: {"S": 35, "O": 14, "N": 18},
    4: {"S": 90, "O": 22, "N": 30},
    5: {"S": 130, "O": 32, "N": 45},
    6: {"S": 170, "O": 45, "N": 60},
    7: {"S": 210, "O": 60, "N": 80},
    8: {"S": 250, "O": 75, "N": 100},
    9: {"S": 290, "O": 90, "N": 120},
}


def last_row(name: str, columns: int, notebooks: Path = NOTEBOOKS) -> list[str]:
    row = ""
    with (notebooks / name).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = line.rstrip("\r\n")
    values = row.split("\t")
    if len(values) != columns:
        raise ValueError(f"{name}: expected {columns} columns")
    return values


def decision_hints(state: dict, caps: dict) -> dict[str, str]:
    """Compare S/O/N with the next rank's exact requirements."""
    helpers = REDLINE
    level = state.get("L")
    if type(level) is not int or not 1 <= level <= 10:
        raise ValueError("invalid current level")
    for metric in "OSNHDW":
        value = state.get(metric)
        minimum = -1 if metric == "O" else 0
        if type(value) is not int or (metric != "W" and value < minimum):
            raise ValueError(f"invalid current {metric_name(metric)}")
        if metric in "HD" and value > 10:
            raise ValueError(f"invalid current {metric_name(metric)}")
    risk = state.get("R")
    if risk is not None and (type(risk) is not int or risk < 0):
        raise ValueError("invalid current R")

    known = caps[level][1]
    labels = helpers["labels_for"](state, known, level).split("且")
    low = low_redlines(state)
    guard = []
    if low:
        guard.append("、".join(f"“{metric_name(metric)}”过低" for metric in low)
                     + "，禁止选预测削减这些指标的选项（-1也不行）；优先补"
                     + "、".join(f"“{metric_name(metric)}”" for metric in low))
    if f"{metric_name('R')}偏高" in labels:
        guard.append(f"“{metric_name('R')}”偏高，避免增险，有机会减险")

    if level == 10:
        promotion = "当前已是L10，无下一职级晋升要求"
    else:
        parts = []
        for metric, required in PROMOTION_REQUIREMENTS[level].items():
            current = state[metric]
            name = f"“{metric_name(metric)}”"
            if current < required:
                description = f"当前短板是{name}，差{required-current}分晋升"
            elif current > required:
                description = f"当前长板是{name}，比晋升要求多{current-required}分"
            else:
                description = f"{name}不多不少恰好满足晋升要求"
            if known[metric] == current:
                description += f"，同时{name}已封顶"
            parts.append(description)
        promotion = "；".join(parts)
    return {"守红线": "；".join(guard) or "无已触发项", "补短板": promotion}


def projected_status(state: dict, caps: dict, delta: dict) -> dict:
    after = {}
    for metric in 'SONHDWR':
        if state[metric] is None:
            after[metric] = None
            continue
        change = delta.get(metric, 0)
        if metric == 'W' and change < 0:
            change *= 1 + round((state['L'] / 4) ** 2)
        value = state[metric] + change
        if metric != 'W':
            value = max(-1 if metric == 'O' else 0, value)
        cap = 10 if metric in 'HD' else caps[state['L']][1].get(metric)
        if cap is not None:
            value = min(cap, value)
        after[metric] = value
    return after


def low_redlines(state: dict) -> dict:
    return {m: threshold + 1 for m, threshold in LOW_THRESHOLDS.items() if state[m] <= threshold}


def redline_candidates(state: dict, options: list[dict]) -> list[dict]:
    """Keep the visible menu intact; low metrics cannot be traded for other gains."""
    low = low_redlines(state)
    return [option for option in options if all(option['metrics'].get(m, 0) >= 0 for m in low)]


def redline_score(state: dict, after: dict, delta: dict) -> tuple:
    low = low_redlines(state)
    gain = sum(min(safe, after[m]) - state[m] for m, safe in low.items())
    risk = state['R']
    if risk is not None and risk > 1:
        gain += risk - max(1, after['R'])
    return (-int(after['H'] <= 0), -max(0, (after['R'] if risk is not None else delta['R']) - 1), gain)


def semantic_deadlock(state: dict, caps: dict, options: list[dict], context: dict | None = None) -> str | None:
    candidates = redline_candidates(state, options)
    if not candidates:
        return '所有选项均预测削减已过低指标，无可推荐选项'
    pairs = [(projected_status(state, caps, option['metrics']), option) for option in candidates]
    if context is not None:
        def alive(after):
            after = dict(after, L=state['L'])
            if after['H'] <= 0 or (after['R'] is not None and after['R'] >= 5 and context.get('risk_bursts', 0) > 0):
                return False
            if context['month']%6 == 5 and context.get('story_actions', 0) == 2:
                return POLICY['review'](after, caps, context, _policy_helpers())[2]
            return True
        if all(not alive(row) for row, _ in pairs):
            return '所有守红线候选均预测触发淘汰'
    # A wholly negative raw event needs semantic correction. Capped positive
    # predictions are still informative; R reduction is a positive benefit.
    if all(not any(option['metrics'].get(m, 0) > 0 for m in 'SONHDW')
           and option['metrics'].get('R', 0) >= 0 for option in options):
        return '所有选项均预测无正向增量'
    if state['L'] == 1:
        best = max(redline_score(state, row, option['metrics']) for row, option in pairs)
        guarded = [(row, option) for row, option in pairs if redline_score(state, row, option['metrics']) == best]
        thresholds = {**PROMOTION_REQUIREMENTS[1], 'D': 2}
        if all(any(state[m] >= required and row[m] < required
                   for m, required in thresholds.items()) for row, _ in guarded):
            return '所有选项均破坏L1已满足的晋升门槛'
    return None


def _greedy_guidance(state: dict, caps: dict, options: list[dict]) -> dict:
    """Rank every option by safety, then the largest promotion gap; preserve the menu."""
    requirements = PROMOTION_REQUIREMENTS.get(state["L"], {})
    gaps = {metric: max(0, required - state[metric]) for metric, required in requirements.items()}
    shortfalls = [metric for metric in gaps if gaps[metric] > 0]
    primary = max(shortfalls, key=gaps.get) if shortfalls else None
    scores = {}
    if not options:
        raise ValueError("没有可选项")
    candidates = redline_candidates(state, options)
    if not candidates:
        return {**decision_hints(state, caps), 'options': options}
    for option in candidates:
        delta = option['metrics']
        risk = state['R']
        after = projected_status(state, caps, delta)
        progress = {m: gaps[m] - max(0, required - after[m]) for m, required in requirements.items()}
        primary_gain = progress.get(primary, 0)
        other_gain = sum(value for m, value in progress.items() if m != primary)
        cost = sum(max(0, state[m] - after[m]) for m in 'SONHDW') + max(0, delta['R'])
        upkeep = max(0, after['H'] - state['H']) + (max(0, risk - after['R']) if risk is not None else 0)
        reserve = (after['S'] - state['S'], sum(after[m] - state[m] for m in 'SON'))
        scores[option['choice']] = (redline_score(state, after, delta), primary_gain, other_gain,
                                    upkeep if not shortfalls else 0, -cost, reserve, -option['choice'])
    best_guard = max(scores[o['choice']][0] for o in candidates)
    guarded = [o['choice'] for o in candidates if scores[o['choice']][0] == best_guard]
    recommended = max(guarded, key=scores.get)
    return {'推荐选项': recommended, **decision_hints(state,caps), 'options': options}


def recommendation_notes(state: dict, context: dict, option: dict) -> str:
    changes = '、'.join(f'{metric_name(m)}{option["metrics"].get(m,0):+d}' for m in 'SONHDWR'
                       if option['metrics'].get(m,0)) or '无增减'
    return f'L{state["L"]}第{context["month"]}月；名义预测{changes}；按红线、晋升门槛与净值选择。'


def _policy_helpers() -> dict:
    return {'project': projected_status, 'guard': redline_candidates,
            'safety': redline_score, 'greedy': _greedy_guidance,
            'requirements': PROMOTION_REQUIREMENTS, 'low_thresholds': LOW_THRESHOLDS,
            'deadlock': semantic_deadlock}


def decision_confidence(state: dict, caps: dict, options: list[dict], context: dict,
                        selected: int | None) -> dict:
    """Zero means the numeric strategy abstains; it does not mean true loss is certain."""
    if context['kind'] != 'story' and selected is not None:
        return {'confidence': 1., 'reason': None, 'samples': 0}
    reason = semantic_deadlock(state, caps, options, context)
    if reason or selected is None:
        return {'confidence': 0., 'reason': reason or '符号策略无法推荐', 'samples': 0}
    result = POLICY['ERRORS']['decision_stability'](
        state, caps, options, selected, context, _policy_helpers(),
        POLICY['_choose_nominal'], POLICY['review'])
    # Independently configured cutoffs; weaker support hands off to the Leader.
    threshold = (RISK_CONFIDENCE_THRESHOLD if state['R'] is None or state['R'] > 1
                 else NORMAL_CONFIDENCE_THRESHOLD)
    if threshold-.125 <= result['confidence'] <= threshold+.125:
        initial = result['confidence']
        result = POLICY['ERRORS']['decision_stability'](
            state, caps, options, selected, context, _policy_helpers(),
            POLICY['_choose_nominal'], POLICY['review'], samples=128)
        result['initial_confidence'] = initial
    result['reason'] = '决策置信度低于纠偏阈值' if result['confidence'] < threshold else None
    return result


def decision_guidance(state: dict, caps: dict, options: list[dict], context: dict | None = None) -> dict:
    guidance = _greedy_guidance(state, caps, options)
    if context is not None:
        helpers = _policy_helpers()
        choice = POLICY['choose'](state, caps, options, context, helpers)
        if choice is not None:
            guidance['推荐选项'] = choice
        else:
            guidance.pop('推荐选项', None)
    return guidance


def decision_context(notebooks: Path, task: dict, translation: dict, observation: dict, risk):
    """Use public time/status and completed receipts, never engine-private counters."""
    def kind(choices):
        if not all(isinstance(c.get('status_updates'), dict) for c in choices):
            return 'story'
        return 'energy' if any(c.get('energy_cost', 0) > 0 for c in choices) else 'main'

    current = observation['current_state']
    month, status = current['time']['current_month'], current['status']
    context = {'month': month, 'kind': kind(task['choices']),
               'duration': status.get('duration_in_level', 1), 'energy': status['energy'],
               'story_actions': 0, 'energy_actions': 0, 'bad_reviews': 0, 'risk_bursts': 0}
    history, latest = [], None
    for number in range(int(task['event_id']) - 1, max(0, int(task['event_id']) - 10), -1):
        directory = translation['event_dir'](notebooks, {**task, 'event_id': f'{number:05d}'})
        if not (directory / 'action.json').exists():
            break
        receipt = translation['read'](directory / 'action.json')
        latest = latest or receipt
        public = translation['read'](directory / 'public-event.json')
        if public.get('current_month') != month:
            break
        previous = translation['read'](directory / 'task.json')
        selected = next(c for c in previous['choices'] if c['choice'] == receipt['choice'])
        event_kind = kind(previous['choices'])
        counter = {'story': 'story_actions', 'energy': 'energy_actions'}.get(event_kind)
        if counter:
            context[counter] += 1
        if event_kind == 'story':
            delta = translation['read'](directory / 'R/complete.json')['values'][str(receipt['choice'])]['R']
        else:
            delta = translation['official_delta'](selected, 'R')
        history.append((receipt, delta))
    last_burst, last_salary, reset_action = -1, -1, -1
    if latest and latest.get('log'):
        path = Path(latest['log']['file'])
        text = path.read_text(encoding='utf-8')
        if path.name != task['session_id'] + '.log' or not text.startswith('Log file: '):
            raise ValueError('policy log session mismatch')
        entries = list(re.finditer(r'^\[([a-z_]+)\] ', text, re.MULTILINE))
        for index, entry in enumerate(entries):
            message = text[entry.end():entries[index+1].start() if index+1<len(entries) else len(text)]
            if entry[1] == 'salary' and 'HR' in message:
                last_salary = index
            if entry[1] == 'warning':
                context['bad_reviews'] += int('不妙的半年绩效' in message)
                if '隐患好巧不巧一起炸了' in message:
                    context['risk_bursts'] += 1
                    last_burst = index
        reset_action = next((i for i, entry in enumerate(entries)
                             if i > last_burst and entry[1] in ('action', 'fixed_event')), last_burst)
    # Monthly HR feedback is the anchor. A later public burst resets risk to zero.
    reset = last_burst > last_salary
    if reset:
        risk = 0
    for receipt, delta in reversed(history):
        if risk is not None and (not reset or receipt.get('log', {}).get('entry', -1) > reset_action):
            risk = max(0, risk + delta)
    return context, risk


def read_context(event_id: str | None = None, *, notebooks: Path = NOTEBOOKS) -> dict:
    files = ("workflow-state.json", "redline-guardian-keeps.tsv", "review-context.json")
    snapshot = {name: (notebooks / name).read_bytes() for name in files}
    session = json.loads(snapshot["workflow-state.json"])
    session_id = session["session_id"]
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("workflow-state.json: session_id is not initialized")
    redline = last_row("redline-guardian-keeps.tsv", 3, notebooks)
    if not re.fullmatch(r"[0-9]{5}", redline[0]) or redline[0] == "99999":
        raise ValueError("redline: invalid previous event_key")
    event_key = f"{int(redline[0]) + 1:05d}"
    if not redline[2] or "待判定" in redline[2] or any(c in redline[2] for c in "<>"):
        raise ValueError("redline: observation check incomplete")
    helpers = REDLINE
    state = helpers["parse_expected"](redline[1])
    caps = helpers["fixed_stat_caps"]()
    decision_hints(state, caps)  # Validate the observed state before ranking.
    translation = runpy.run_path(str(Path(__file__).with_name("translation-state.py")))
    task = translation["current_task"](notebooks, event_id)
    if task["event_id"] != event_key:
        raise ValueError("translation event does not match current redline")
    options = [
        {key: option[key] for key in ("choice", "metrics", "energy_cost") if key in option}
        for option in translation["option_metrics"](notebooks, task["event_id"])
        if option["selectable"]
    ]
    official = {choice['choice']: choice.get('status_updates') or {} for choice in task['choices']}
    fixed_choices = {choice['choice'] for choice in task['choices'] if isinstance(choice.get('status_updates'), dict)}
    for option in options:
        option['certain'] = option['choice'] in fixed_choices
        dignity = official[option['choice']].get('Dignity', 0)
        if dignity:
            option['metrics']['D'] = dignity
    context, state['R'] = decision_context(notebooks, task, translation,
                                         json.loads(snapshot['review-context.json']), state['R'])
    hints = decision_hints(state, caps)
    guidance = decision_guidance(state, caps, options, context)
    event = None
    if any(not isinstance(choice.get('status_updates'), dict) for choice in task['choices']):
        assessment = decision_confidence(state, caps, options, context, guidance.get('推荐选项'))
        if assessment['reason']:
            public = translation['read'](translation['event_dir'](notebooks, task) / 'public-event.json')
            event = {
                'current_event': public['current_event'],
                **({'event_history': public['event_history']} if public.get('event_history') else {}),
                'options': public['choices'],
                'constraints': {
                    '守红线': hints['守红线'].replace('禁止选预测削减这些指标的选项','禁止选择会削减这些指标的行为'),
                    '晋升条件': hints['补短板'],
                },
            }
    if any((notebooks / name).read_bytes() != content for name, content in snapshot.items()):
        raise ValueError("notebooks changed during context read; refresh observation first")
    if event is not None:
        return event
    if '推荐选项' not in guidance:
        raise ValueError('所有选项均削减已过低指标，无法推荐；' + hints['守红线'])
    selected = next(option for option in options if option['choice']==guidance['推荐选项'])
    return {'推荐选项': guidance['推荐选项'], 'notes': recommendation_notes(state,context,selected)}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 1:
            raise ValueError("usage: read-context.py")
        packet = read_context()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"context_error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(packet, ensure_ascii=False))
