"""c19/run_guard: public-state routing, then the selected joint H/W question."""
import json
import os
from pathlib import Path


def policy():
    questions = json.loads(Path(__file__).with_suffix('.json').read_text())['questions']
    return {'mode': 'hw_guard', **{k: v['text'] for k, v in questions.items()},
            'allowed': questions['ordinary']['options']}


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False) + '\n')
    os.replace(temporary, path)


def main(text, directory=None):
    root = Path(directory or Path.cwd())
    event = json.loads((root / '.event.json').read_text())
    config = json.loads((root / '.policy.json').read_text())
    path = root / '.answers.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    ids = {str(c['choice']) for c in event['choices']}
    if set(state) == ids:
        return '无问题，事件翻译结束。'
    error = ''
    if text != 'ready':
        try:
            value = json.loads(text)
            if '_route' not in state:
                if (not isinstance(value, dict)
                        or set(value) != {'existing_H_load', 'physical_risk'}
                        or any(type(v) is not bool for v in value.values())):
                    raise ValueError('前置题只提交 existing_H_load、physical_risk 两个布尔字段。')
                state = {'_route': value}
                save(path, state)
            elif '_joint' not in state:
                if not isinstance(value, dict) or set(value) != ids:
                    raise ValueError('按当前选项编号一次提交全部 H/W。')
                if any(not isinstance(row, dict) or set(row) != {'H', 'W'}
                       or any(type(v) is not int or v not in config['allowed'] for v in row.values())
                       for row in value.values()):
                    raise ValueError('每个选项的 H/W 必须是 -3 到 3 的整数。')
                route = state['_route']
                if route['existing_H_load'] or route['physical_risk']:
                    save(path, value)
                    return '无问题，事件翻译结束。'
                state['_joint'] = value
                save(path, state)
            else:
                if not isinstance(value, dict) or set(value) != ids or any(type(v) is not bool for v in value.values()):
                    raise ValueError('按当前选项编号一次提交全部 H 变化布尔值。')
                result = {key: {'H': state['_joint'][key]['H'] if value[key] else 0,
                                'W': state['_joint'][key]['W']} for key in ids}
                save(path, result)
                return '无问题，事件翻译结束。'
        except (ValueError, TypeError) as exc:
            error = '答案格式错误：' + str(exc) + '\n'
    if '_route' not in state:
        question = config['route']
    elif '_joint' not in state:
        route = state['_route']
        branch = ('physical_' if route['physical_risk'] else '')
        branch += 'existing' if route['existing_H_load'] else 'ordinary'
        question = config[branch]
    else:
        question = config['confirm_history' if event.get('event_history') else 'confirm']
    options = '\n选项：' + '；'.join(
        f'{c["choice"]}. {c["action"]}' + (f'（{c["description"]}）' if c.get('description') else '')
        for c in event['choices'])
    return error + question + options
