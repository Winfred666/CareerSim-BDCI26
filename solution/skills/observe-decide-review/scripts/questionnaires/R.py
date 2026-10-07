"""R questions: increased risk, protection, then remaining changes."""
import json,os,re
from pathlib import Path
import runpy
decode = runpy.run_path(str(Path(__file__).with_name('R-integer-json.py')))['decode']

def save(p,x):
 t=p.with_suffix('.tmp');t.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');os.replace(t,p)
def load(root):
 p=root/'.answers.json';return json.loads((root/'.event.json').read_text()),json.loads(p.read_text()) if p.exists() else {}
def is_complete(event,state):return all('R' in state.get(str(c['choice']),{}) for c in event['choices'])
def current(event,state,conf):
 if is_complete(event,state):return 0,[]
 if state:return max(v.get('pending',2) for v in state.values() if 'R' not in v),[c for c in event['choices'] if 'R' not in state.get(str(c['choice']),{})]
 return 1,event['choices']
def render(event,state,conf):
 phase,choices=current(event,state,conf)
 if not phase:return '无问题，事件翻译结束。'
 t=conf['templates'][phase-1];values={'title':event['current_event']['title']}
 m=re.search(r'\[\[each_option\]\](.*?)\[\[/each_option\]\]',t,re.S)
 options='\n'.join(m.group(1).strip().format_map({**values,'choice':c['choice'],'option':c['action']+(f'（{c["description"]}）' if c.get('description') else '')}) for c in choices)
 body=t[:m.start()].format_map(values)+options+t[m.end():].format_map(values)
 return body

def apply(event,state,conf,answers):
 phase,choices=current(event,state,conf)
 if not phase or not isinstance(answers,dict) or set(answers)!={str(c['choice']) for c in choices}:raise ValueError('按当前选项编号一次提交全部答案。')
 allowed=conf['allowed'][str(phase)]
 if any(type(v)is not int or v not in allowed for v in answers.values()):raise ValueError('每项提交本题允许的整数：'+','.join(map(str,allowed))+'。')
 defer=phase<3
 return {**state,**{k:({'pending':phase+1} if defer and v==0 else {'R':v}) for k,v in answers.items()}}
def main(text,directory=None):
 root=Path(directory or Path.cwd());event,state=load(root);conf=json.loads((root/'.policy.json').read_text())
 if text!='ready' and not is_complete(event,state):
  try:state=apply(event,state,conf,decode(text))
  except (ValueError,TypeError) as e:return '答案格式错误：'+str(e)+'\n'+render(event,state,conf)
  save(root/'.answers.json',state)
 return render(event,state,conf)

def policy():
 questions=json.loads(Path(__file__).with_suffix('.json').read_text())['questions']
 return {'mode':'tri','templates':[questions[str(i)]['text'] for i in (1,2,3)],
         'allowed':{k:v['options'] for k,v in questions.items()}}

if __name__=='__main__':
 import sys
 print(main(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else None))
