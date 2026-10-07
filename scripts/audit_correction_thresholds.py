#!/usr/bin/env python3
"""Offline audit of the completed, predeclared correction cutoff comparison."""
import argparse
from collections import Counter
import hashlib
import json
from itertools import combinations
from pathlib import Path
import random
import statistics

from scripts.compare_symbolic_scores import paired_summary, score_summary
from scripts.benchmark_bare_correction import canonical, request_for


def choice_diagnostics(rows):
    """Hidden outcomes are inspected only offline, never sent to the Leader."""
    counts = Counter()
    for row in rows:
        for step in row['trace']:
            if step['context']['kind'] != 'story':
                continue
            counts['ordinary_events'] += 1
            c = step.get('correction')
            if not c:
                continue
            counts['corrections'] += 1
            counts['changed'] += c['choice'] != c['numeric_choice']
            oracle = c.get('oracle_choice')
            if oracle is not None:
                counts['oracle_available'] += 1
                counts['numeric_matches_one_step_oracle'] += c['numeric_choice'] == oracle
                counts['leader_matches_one_step_oracle'] += c['choice'] == oracle
            deltas = step['actual_deltas']
            numeric_r = deltas[str(c['numeric_choice'])].get('R', 0) > 0
            leader_r = deltas[str(c['choice'])].get('R', 0) > 0
            counts['R_positive_corrected'] += numeric_r and not leader_r
            counts['R_positive_introduced'] += leader_r and not numeric_r
            if c['raw_guard_choices']:
                numeric_guard = c['numeric_choice'] not in c['raw_guard_choices']
                leader_guard = c['choice'] not in c['raw_guard_choices']
                counts['guard_violation_corrected'] += numeric_guard and not leader_guard
                counts['guard_violation_introduced'] += leader_guard and not numeric_guard
                counts['avoidable_leader_guard_violations'] += leader_guard
    return dict(counts)


def first_divergences(before, after, allow_routing_removal=False):
    """Compare identical states before the first intervention changes a choice."""
    counts = Counter()
    for i in sorted(before):
        a, b = before[i], after[i]
        diverged = False
        for left, right in zip(a['trace'], b['trace']):
            for key in ('state', 'context', 'options', 'actual_deltas'):
                if left[key] != right[key]:
                    raise ValueError(f'Unpaired trajectory before first choice divergence: seed {i}, {key}')
            if left['choice'] == right['choice']:
                continue
            diverged = True
            counts['games_with_changed_choices'] += 1
            if right['context']['kind'] != 'story':
                raise ValueError('Fixed actions changed before any routing intervention')
            left_c, right_c = left.get('correction'), right.get('correction')
            if bool(left_c) == bool(right_c):
                raise ValueError('First divergence did not switch between script and correction')
            if left_c and not allow_routing_removal:
                raise ValueError('A higher cutoff unexpectedly removed a correction')
            c = right_c or left_c
            numeric = left if right_c else right
            if c['numeric_choice'] != numeric['choice']:
                raise ValueError('Numeric choice changed independently of the routing cutoff')
            counts['correction_added_first' if right_c else 'correction_removed_first'] += 1
            if c['oracle_choice'] is not None:
                counts['oracle_available'] += 1
                counts['before_matches_one_step_oracle'] += left['choice'] == c['oracle_choice']
                counts['after_matches_one_step_oracle'] += right['choice'] == c['oracle_choice']
            counts['promotion_key_month'] += bool(c['promotion_key_month'])
            counts['risk_recovery_conflict'] += bool(c['risk_recovery_conflict'])
            positive_r = lambda choice: right['actual_deltas'][str(choice)].get('R', 0) > 0
            counts['R_positive_corrected'] += positive_r(left['choice']) and not positive_r(right['choice'])
            counts['R_positive_introduced'] += positive_r(right['choice']) and not positive_r(left['choice'])
            break
        if not diverged:
            if len(a['trace']) != len(b['trace']) or a['ending'] != b['ending']:
                raise ValueError('Identical choices produced different settled outcomes')
            counts['identical_trajectory_games'] += 1
    return dict(counts)


def read(path):
    return json.loads(path.read_text())


def audit(root):
    manifest = read(root / 'manifest.json')
    reference = root.parent / manifest['reference']
    summary = read(root / 'summary.json')
    grid_2d = any(a['normal'] != a['risk'] for a in manifest['arms'].values())
    if not summary['final']:
        raise ValueError('Wait for every complete 128-game arm before selecting a cutoff')
    paths = sorted((reference / 'games').glob('*.json'))
    groups = {'original': {int(p.stem): read(p)['candidate'] for p in paths}}
    for name in manifest['arms']:
        groups[name] = {int(p.stem): read(p) for p in (root / name / 'games').glob('*.json')}
    expected = set(range(manifest['seeds']))
    if any(set(rows) != expected for rows in groups.values()):
        raise ValueError('Every group must contain exactly the predeclared seeds')
    for i in expected:
        if len({(g[i]['seed'], g[i]['replicate']) for g in groups.values()}) != 1:
            raise ValueError('Seed or noise replicate mismatch')

    result = {'selection_rule': 'Maximum full-cohort quantitative median; ties: completion count, mean, fewer corrections.',
              'cohort': manifest['cohort'], 'seeds': manifest['seeds'], 'groups': {},
              'limitations': ['User-requested alternatives on one reused seed cohort; selection is exploratory.',
                              'Half-cohort checks are descriptive, not an independent validation set.',
                              'No cap-note enhancement; original bare-event prompt and constraints unchanged.']}
    for name, indexed in groups.items():
        rows = [indexed[i] for i in sorted(expected)]
        scores = score_summary(rows)
        failures = Counter('completed' if r['ending']['completed'] else r['final_state']['failure_reason'] for r in rows)
        halves = {}
        for label, indices in [('first_64', range(64)), ('last_64', range(64, 128))]:
            sample = [indexed[i] for i in indices]
            old = [groups['original'][i] for i in indices]
            halves[label] = dict(score_summary(sample), paired_vs_original=paired_summary(
                {(r['seed'], r['replicate']): r for r in old},
                {(r['seed'], r['replicate']): r for r in sample}))
        entry = dict(scores, halves=halves, failures=dict(failures),
                     choice_diagnostics=choice_diagnostics(rows))
        if name != 'original':
            a = [groups['original'][i]['ending']['quantitative_score'] for i in sorted(expected)]
            b = [indexed[i]['ending']['quantitative_score'] for i in sorted(expected)]
            rng = random.Random(20261005)
            draws = []
            for _ in range(4000):
                indices = [rng.randrange(len(a)) for _ in a]
                draws.append(statistics.median(b[i] for i in indices) - statistics.median(a[i] for i in indices))
            draws.sort()
            tail = .025 / len(manifest['arms'])
            entry['median_change_ci_familywise95'] = [draws[int(tail*len(draws))], draws[int((1-tail)*len(draws))]]
        result['groups'][name] = entry

    def rank(name):
        s = result['groups'][name]
        actions = summary['original']['actions'] if name == 'original' else summary['arms'][name]['actions']
        return s['median'], s['completed'], s['mean'], -actions['corrections']

    best = max(groups, key=rank)
    result['best_mean_arm'] = max(manifest['arms'], key=lambda name:(
        result['groups'][name]['mean'],result['groups'][name]['median'],result['groups'][name]['completed']))
    pairs = ([('original',name) for name in manifest['arms']] +
             [('cutoff_070',name) for name in manifest['arms'] if name!='cutoff_070'] +
             [('extreme','leader')]) if grid_2d else list(combinations(groups,2))
    result['first_divergence_diagnostics'] = {
        f'{after}_vs_{before}': first_divergences(groups[before], groups[after],
                                               allow_routing_removal=grid_2d and before=='cutoff_070')
        for before, after in pairs}
    comparison_baseline = 'cutoff_070' if grid_2d else 'extreme'
    paired_key = 'paired_vs_current_070' if grid_2d else 'paired_vs_current_075'
    result['comparison_baseline'] = comparison_baseline
    result[paired_key] = {
        name: paired_summary({(r['seed'],r['replicate']):r for r in groups[comparison_baseline].values()},
                             {(r['seed'],r['replicate']):r for r in rows.values()})
        for name,rows in groups.items() if name!=comparison_baseline}
    for name in manifest['arms']:
        if name==comparison_baseline:continue
        baseline=[groups[comparison_baseline][i]['ending']['quantitative_score'] for i in sorted(expected)]
        candidate=[groups[name][i]['ending']['quantitative_score'] for i in sorted(expected)]
        rng=random.Random(20261005)
        medians,means=[],[]
        for _ in range(4000):
            indices=[rng.randrange(len(baseline)) for _ in baseline]
            medians.append(statistics.median(candidate[i] for i in indices)-statistics.median(baseline[i] for i in indices))
            means.append(statistics.mean(candidate[i]-baseline[i] for i in indices))
        tail=.025/(len(manifest['arms'])-1)
        for label,draws in [('median',medians),('mean',means)]:
            draws.sort()
            result[paired_key][name][f'{label}_change_ci_familywise95']=[
                draws[int(tail*len(draws))],draws[int((1-tail)*len(draws))]]
    result['selected_arm'] = best
    result['selected_thresholds'] = manifest['arms'].get(best, dict(normal=7/16, risk=.5))
    result['cache_activity_this_controller'] = summary['cache_activity']
    result['answer_pool'] = manifest['answer_pool']
    pool = Path(manifest['answer_pool'])
    initial_keys = set(read(root / 'initial-cache-keys.json'))
    keys = set()
    accepted_answers = {}
    for name, indexed in groups.items():
        if name == 'original':
            continue
        for row in indexed.values():
            for step in row['trace']:
                c = step.get('correction')
                if not c:
                    continue
                key = c['cache_key']
                if key in accepted_answers:
                    if accepted_answers[key] != {k:c[k] for k in ('choice','notes')}:
                        raise ValueError('A shared context used inconsistent accepted answers')
                    continue
                record = read(pool / (key + '.json'))
                identity = {k: record[k] for k in ('version', 'request', 'public_cache_context')}
                if hashlib.sha256(canonical(identity).encode()).hexdigest() != key:
                    raise ValueError('Cache identity mismatch')
                if record.get('answer') != {k: c[k] for k in ('choice', 'notes')} or not record.get('accepted'):
                    raise ValueError('Replay did not preserve the accepted answer')
                packet = json.loads(record['request']['messages'][1]['content'])
                if packet['constraints']['晋升条件'].endswith('；封顶指标增加无效。'):
                    raise ValueError('Canceled cap-note arm leaked into threshold results')
                if record['request'] != request_for(packet, manifest['system'], manifest['model']):
                    raise ValueError('Prompt/model/request format changed')
                keys.add(key)
                accepted_answers[key] = record['answer']
    result['cache_integrity'] = dict(unique_used_answers=len(keys),
                                    from_initial_pool=len(keys & initial_keys),
                                    added_contexts=len(keys - initial_keys),
                                    identity_and_answer_preserved=True,
                                    canceled_cap_note_used=False)
    result['production_applied'] = False
    application_path = root / 'application.json'
    if application_path.exists():
        application = read(application_path)
        source = Path(application['source_path'])
        applied_arm = application.get('selected_arm', best)
        applied_thresholds = manifest['arms'].get(applied_arm)
        # Preserve the predeclared median ranking. An explicitly documented
        # deployment may instead retain the mean winner for survival stability.
        allowed_arms = {best, result['best_mean_arm']}
        if (source.exists() and applied_arm in allowed_arms
                and application['thresholds'] == applied_thresholds
                and hashlib.sha256(source.read_bytes()).hexdigest() == application['source_sha256']
                and set(application['fixed_policy_and_error_model_unchanged']) == {
                    'numeric-policy.py', 'translation-error.py', 'translation-errors.json'}
                and all((source.parent / name).exists() and
                        (source.parent / name).read_bytes() == (root / 'policy' / name).read_bytes()
                        for name in application['fixed_policy_and_error_model_unchanged'])):
            result['production_applied'] = True
            result['application_evidence'] = application
            result['production_arm'] = applied_arm
            result['production_thresholds'] = applied_thresholds
    (root / 'threshold-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    lines = ['# 纠偏阈值比较', '',
             '同一误差模型、原始纠偏包、128 种子；已接受的答案原样复用，封顶提示增强已取消。', '',
             '|支持率阈值：常规 / R偏高|量化中位数|中位数95%区间|均分|48月完成|纠偏占比|',
             '|---|---:|---|---:|---:|---:|']
    thresholds = {'original': dict(normal=7/16, risk=.5), **manifest['arms']}
    order=['original']+sorted(manifest['arms'],key=lambda name:(
        manifest['arms'][name]['normal'],manifest['arms'][name]['risk']),reverse=True)
    for name in order:
        g=result['groups'][name]
        t = thresholds[name]
        actions = summary['original']['actions'] if name == 'original' else summary['arms'][name]['actions']
        ci = g['median_ci95']
        lines.append(f"|{t['normal']:.4g} / {t['risk']:.4g}|{g['median']:.2f}|{ci[0]:.2f}～{ci[1]:.2f}|{g['mean']:.2f}|{g['completed']}/128|{100*actions['route_fraction']:.2f}%|")
    t = result['selected_thresholds']
    lines += ['', f"按预先登记的完整样本中位数排序，首位阈值为：**{t['normal']:.4g} / {t['risk']:.4g}**。完成率、均分和较少纠偏用于打破平局。", '',
              '支持率低于阈值时纠偏，因此阈值越大，接管门槛越低。普通推荐、红线、结构性纠偏、决策包与提示不变。', '',
              '这是一轮探索性比较，不能证明全局最优或纠偏越多越好；各区间与上下半组核对结果见 threshold-audit.json。',
              '仍沿用真实当前 R 已知、误差边际独立等模拟假设；结果是量化原分 /100，不是云端总成绩。', '',
              f"答案完整性检查通过；使用 {len(keys)} 个唯一上下文，其中 {len(keys & initial_keys)} 个已存在于本次运行开始时的共享池。"]
    if result['production_applied']:
        pt = result['production_thresholds']
        lines += ['', f"正式推荐脚本采用 **{pt['normal']:g}/{pt['risk']:g}**；支持率恰好等于阈值时继续脚本。应用记录与当前脚本及固定模型校验一致。",
                  application.get('selection_rationale', '采用中位数排序首位。'),
                  application['validation'] + '。']
    if 'extreme_vs_high' in summary['comparisons']:
        d = summary['comparisons']['extreme_vs_high']
        lines += ['', f"0.75 相对 0.5625/0.625 的中位数高 {d['median_change']:.3f} 分，配对差值95%区间 {d['median_change_ci95']}；均分高 {d['mean_change']:.3f} 分，区间 {d['mean_change_ci95']}。本批点估计领先，优势尚不能统计确认。"]
    if 'leader_vs_extreme' in summary['comparisons']:
        d = summary['comparisons']['leader_vs_extreme']
        lines += ['', f"0.9 相对 0.75 中位数变化 {d['median_change']:.2f} 分、均分变化 {d['mean_change']:.2f} 分、完成局数变化 {d['completion_change']} 局。"]
        v = result['first_divergence_diagnostics']['leader_vs_extreme']
        lines += [f"{v['games_with_changed_choices']} 局首次新增接管发生在完全相同的状态/事件下；0.75 原选择匹配真值单步脚本 {v['before_matches_one_step_oracle']} 次，0.9 为 {v['after_matches_one_step_oracle']} 次。新增接管首次选择消除实际 R+ {v['R_positive_corrected']} 次，同时引入 R+ {v['R_positive_introduced']} 次。单步脚本仅为固定策略代入真实六项增量所得，并非未来全局最优。"]
    if 'cutoff_060' in manifest['arms']:
        baseline_title = '0.7/0.7' if grid_2d else '0.75'
        lines += ['', f'各候选相对基线 {baseline_title} 的同种子配对差值；区间对{len(manifest["arms"])-1}项比较作同时95%调整：', '',
                  '|常规 / R门槛|中位数变化|中位数差值同时95%区间|均分变化|均分差值同时95%区间|完成局数变化|',
                  '|---|---:|---|---:|---|---:|']
        for name in order[1:]:
            if name == comparison_baseline:
                continue
            d = result[paired_key][name]
            med_ci = d['median_change_ci_familywise95']
            mean_ci = d['mean_change_ci_familywise95']
            lines.append(f"|{thresholds[name]['normal']:.4g}/{thresholds[name]['risk']:.4g}|{d['median_change']:+.2f}|"
                         f"{med_ci[0]:+.2f}～{med_ci[1]:+.2f}|{d['mean_change']:+.2f}|"
                         f"{mean_ci[0]:+.2f}～{mean_ci[1]:+.2f}|{d['completion_change']:+d}|")
        lines += ['', '配对bootstrap区间仅衡量本批种子抽样波动，不涵盖模型重问波动、正式事件池变化或误差模型错设。']
    if grid_2d:
        values = sorted({a['normal'] for a in manifest['arms'].values()},reverse=True)
        for metric,title in [('median','量化中位数'),('mean','量化均分')]:
            lines += ['', f'## {title}二维网格','',
                      '行=常规支持率门槛，列=当前R>1或R未知时的支持率门槛。','',
                      '|常规 / R|'+'|'.join(f'{x:g}' for x in values)+'|',
                      '|---|'+'---:|'*len(values)]
            for normal in values:
                cells=[]
                for risk in values:
                    name=next(n for n,a in manifest['arms'].items() if a==dict(normal=normal,risk=risk))
                    cells.append(f"{result['groups'][name][metric]:.3f}")
                lines.append(f'|{normal:g}|'+'|'.join(cells)+'|')
        mean_name=result['best_mean_arm']; mean_thresholds=thresholds[mean_name]
        lines += ['', f"均分最高组合：{mean_thresholds['normal']:g}/{mean_thresholds['risk']:g}，"
                  f"均分{result['groups'][mean_name]['mean']:.3f}，中位数{result['groups'][mean_name]['median']:.3f}。"]
    g = result['groups'][best]
    lines += ['', f"中位数排序首位的本批中位数 {g['median']:.2f} 的95%区间为 {g['median_ci95']}，尚不能证明长期稳定高于65。各档相对原档的多重比较调整区间另见 summary.json 和 threshold-audit.json。"]
    (root / 'threshold-audit.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    audit(parser.parse_args().root)


if __name__ == '__main__':
    main()
