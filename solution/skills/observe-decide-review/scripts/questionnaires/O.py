"""O direction first; no event identity, labels, lookup, or authored context."""
import json,os,sys
from pathlib import Path
def policy():
 return json.loads(Path(__file__).with_suffix('.json').read_text())['questions']
QUESTIONS={k:v['text'] for k,v in policy().items()}
ALLOWED={k:set(v['options']) for k,v in policy().items()}
def save(p,v):
 t=p.with_suffix('.tmp');t.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n');os.replace(t,p)
def stage(v):
 if 'O' in v:return None
 if 'kind' not in v:return 'kind'
 k='business' if v['kind']==1 else 'other'
 if k not in v:return k
 positive=v[k]==(1 if k=='business' else 2)
 return 'scale_plus' if positive else 'scale_minus'
def current(event,state):
 for c in event['choices']:
  key=stage(state.get(str(c['choice']),{}))
  if key:return c,key
 return None,None
def pending(event,state):
 _,key=current(event,state)
 return ([c for c in event['choices'] if stage(state.get(str(c['choice']),{}))==key] if key else []),key
def is_complete(event,state,checks=None):return current(event,state)[1] is None
def render(event,state):
 choices,key=pending(event,state)
 if key is None:return '无问题，事件翻译结束。'
 menu='\n'.join(f'选项{c["choice"]}：{c["action"]}'+(f'（{c["description"]}）' if c.get('description') else '') for c in choices)
 return menu+'\n'+QUESTIONS[key]
def main(text,directory=None):
 root=Path(directory or Path.cwd());event=json.loads((root/'.event.json').read_text());p=root/'.answers.json';state=json.loads(p.read_text()) if p.exists() else {};choices,key=pending(event,state)
 if text!='ready' and key is not None:
  try:
   if text.strip() in {str(i) for i in ALLOWED[key]}:
    values={str(choices[0]['choice']):int(text.strip())}  # Original scalar interface remains valid.
   else:
    values=json.loads(text)
    if (type(values) is not dict or set(values)!={str(c['choice']) for c in choices}
        or any(type(a) is not int or a not in ALLOWED[key] for a in values.values())):
     raise ValueError('按当前模板提交全部所列选项的编号。')
  except (ValueError,TypeError):
   return '答案格式错误：按当前模板填写所列编号。\n'+render(event,state)
  for choice,a in values.items():
   v=state.setdefault(choice,{});v[key]=a
   if key in {'business','other'} and a==0:v['O']=0
   elif key=='scale_minus':v['O']=-a
   elif key=='scale_plus':v['O']=a
  save(p,state)
 return render(event,state)
if __name__=='__main__':
 if len(sys.argv)!=2:raise SystemExit('用法：python3 O.py "ready或答案"')
 print(main(sys.argv[1]))
