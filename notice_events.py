"""Change detection for official notices; durable event list in data.json."""
import json,hashlib
from pathlib import Path


def digest(p):
    fields={k:p.get(k) for k in ('name','announce','start','end','kind','region','supplyPriceRange')}
    fields['housingTypes']=[{k:x.get(k) for k in ('type','priceEok','exclusiveAreaSqm')}
                            for x in (p.get('housingTypes') or [])]
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:16]


def update_events(projects, previous_path='data.json', generated_at=''):
    old={}
    try:
        content=json.loads(Path(previous_path).read_text(encoding='utf-8'))
        old={str(x.get('id')):x for x in content.get('projects',[]) if isinstance(x,dict)}
    except (FileNotFoundError,ValueError,TypeError,KeyError):
        pass
    events=[]
    for p in projects:
        pid=str(p.get('id') or '')
        if not pid: continue
        oldp=old.get(pid)
        if oldp is None:
            if old: events.append({'id':pid,'kind':'new','name':p.get('name'), 'at':generated_at, 'fingerprint':digest(p)})
        elif digest(oldp)!=digest(p):
            events.append({'id':pid,'kind':'changed','name':p.get('name'), 'at':generated_at,'fingerprint':digest(p)})
    return events
