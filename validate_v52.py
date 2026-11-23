"""Validate and atomically publish fresh, self-consistent official notices."""
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def validate(source='data.preview.json', target='data.json'):
    payload=json.loads(Path(source).read_text(encoding='utf-8'))
    projects=payload.get('projects')
    if not isinstance(projects,list) or not projects:
        raise RuntimeError('공고가 0건이거나 유효하지 않습니다. 기존 데이터 유지.')
    if not payload.get('updated_at'):
        raise RuntimeError('갱신 시각 누락')
    from datetime import datetime
    updated=datetime.fromisoformat(payload['updated_at'])
    if updated.tzinfo is None or abs((datetime.now(timezone.utc)-updated.astimezone(timezone.utc)).total_seconds())>86400:
        raise RuntimeError('수집 데이터 갱신 시각 이상 또는 24시간 이상 지남')
    ids=set();errors=[];covered=leads=bands=0
    for idx,p in enumerate(projects):
        name=str(p.get('name') or f'#{idx}')
        for key in ('id','name','region','start','end'):
            if not p.get(key):errors.append(f'{name}: {key} 누락')
        pid=str(p.get('id') or '')
        if pid in ids:errors.append(f'{name}: 공고 ID 중복')
        ids.add(pid)
        mv=p.get('marketV49'); card=p.get('priceSummaryV52')
        if not isinstance(mv,dict) or mv.get('version')!='v49-market-opportunity' or not isinstance(mv.get('byType'),list):
            errors.append(f'{name}: 신축 가격 엔진 데이터 누락')
            continue
        if not isinstance(card,dict) or card.get('version')!='v52-opportunity-summary':
            errors.append(f'{name}: 통합 가격 카드 데이터 누락')
            continue
        models={str(m.get('type')):m for m in p.get('housingTypes') or []}
        if not models and p.get('priceSummaryV52',{}).get('baseline'):
            errors.append(f'{name}: 주택형 없는 비교 가격')
        for label in ('baseline','headline'):
            r=card.get(label)
            if not r:continue
            typ=str(r.get('type'))
            if typ not in models:errors.append(f'{name}: {label} 주택형 불일치')
            else:
                try:
                    sale=float(r.get('saleEok'))
                    ref=float(r.get('referenceEok'))
                    off=float(models[typ].get('priceEok'))
                    gap=float(r.get('gapEok'))
                    if not all(math.isfinite(x) for x in (sale,ref,off,gap)) or abs(sale-off)>.001 or abs(gap-(ref-sale))>.001:
                        errors.append(f'{name}: {label} 금액 비교 오류')
                except (TypeError,ValueError):errors.append(f'{name}: {label} 숫자 오류')
            if label=='baseline' and r.get('category')!='apartment':
                errors.append(f'{name}: 권리 가격을 매매 기준으로 사용')
        covered+=bool(card.get('baseline'));leads+=bool(card.get('headline'))
        bands+=sum(len(x.get('bands') or []) for x in mv['byType'])
    if errors:raise RuntimeError('가격 데이터 계약 검증 실패. 기존 data.json 보존.\n'+'\n'.join(errors[:30]))
    payload['dataSchemaVersion']=52
    q=payload.setdefault('analysisQuality',{})
    q.update(marketEngineVersion='v52-validated', v52PriceCardNotices=len(projects),
             v52BaselineNotices=covered,v52LeadNotices=leads,v52PriceBands=bands)
    target_path=Path(target)
    tmp=target_path.with_name(target_path.name+'.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(tmp,target_path)
    print(f'[v52 publish] PASS notices={len(projects)} baseline={covered} leads={leads} bands={bands}')
    if covered==0:print('[v52 publish] WARNING: 거래수집 매칭 결과가 0건입니다. UI에는 가격 근거 부족으로 표시합니다.')
    return payload

if __name__=='__main__':validate()
