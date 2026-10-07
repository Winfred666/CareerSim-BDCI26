"""One all-option N question, with validation before saving answers."""
import json
import os
from pathlib import Path


def policy():
    question = json.loads(Path(__file__).with_suffix('.json').read_text())['questions']['question']
    return {'question': question['text'], 'allowed': question['options']}


def render(event, state, questions):
    options = '\n选项：' + '；'.join(
        f'{c["choice"]}. {c["action"]}' + (f'（{c["description"]}）' if c.get('description') else '')
        for c in event['choices'])
    return questions['question'] + options + '\n提交 JSON：{"选项编号":{"N":整数}}。'


def main(text, directory=None):
    root = Path(directory or Path.cwd())
    event = json.loads((root / '.event.json').read_text())
    path = root / '.answers.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    ids = {str(c['choice']) for c in event['choices']}
    if set(state) == ids:
        return '无问题，事件翻译结束。'
    questions = json.loads((root / '.policy.json').read_text())
    error = ''
    if text != 'ready':
        try:
            value = json.loads(text)
            if type(value) is not dict or set(value) != ids:
                raise ValueError('按选项编号一次提交全部选项。')
            if any(type(row) is not dict or set(row) != {'N'}
                   or type(row['N']) is not int or row['N'] not in questions['allowed']
                   for row in value.values()):
                raise ValueError('每项提交 {"N":-3至3的整数}。')
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False) + '\n')
            os.replace(temporary, path)
            return '无问题，事件翻译结束。'
        except (ValueError, TypeError) as exc:
            error = '答案格式错误：' + str(exc) + '\n'
    return error + render(event, state, questions)
