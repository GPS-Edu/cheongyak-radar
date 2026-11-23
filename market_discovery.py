"""v47 broad opportunity discovery using reported apartment sales.

These are comparable *market cohorts*, not same-complex valuations. Candidates
are retained even with no comps. Never label any amount as realizable profit.
"""
from collections import defaultdict
from datetime import date, datetime
from statistics import median
import re


def num(v):
    try:
        x = float(str(v).replace(',', ''))
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def model_area(m):
    n = num(m.get('exclusiveAreaSqm'))
    if n and 10 <= n <= 300:
        return n, 'api'
    t = str(m.get('type') or m.get('modelNo') or '')
    x = re.match(r'^(\d{2,3}(?:\.\d+)?)', t)
    n = num(x.group(1)) if x else None
    return (n, 'model_code_estimate') if n and 10 <= n <= 300 else (None, 'unknown')


def analyze(project, rows, today=None):
    today = today or date.today()
    if isinstance(today, datetime): today = today.date()
    if isinstance(today, str): today = date.fromisoformat(today)
    result = []
    # The API's LAWD_CD is county/district-level. Same legal-dong is NOT required.
    for model in project.get('housingTypes') or []:
        typ = str(model.get('type') or model.get('modelNo') or '')
        sale = num(model.get('priceEok'))
        area, source = model_area(model)
        if not typ or not sale or not area: continue
        cohorts = defaultdict(lambda: defaultdict(list))
        for row in rows:
            trade_area = num(row.get('exclusiveAreaSqm'))
            if trade_area is None or abs(trade_area-area) > max(3.0, area*.07): continue
            rawdate = str(row.get('dealDate') or '')[:10]
            try: age = (today-date.fromisoformat(rawdate)).days
            except ValueError: continue
            if age < 0 or age > 540: continue
            amt = num(row.get('dealAmountManwon'))
            year = num(row.get('buildYear'))
            name = str(row.get('apartment') or '').strip()
            parcel = str(row.get('jibun') or '').strip()
            dong = str(row.get('legalDong') or '').strip()
            if not amt or not name: continue
            # API buildYear is sometimes omitted. Missing year is a separate
            # evidence group, NOT a reason to discard a reported sale.
            if year is not None and (year < 1970 or year > today.year + 2): year = None
            # Modern is not a location claim. Permit new and 10-20 year comps,
            # separately, and use older transactions as supplementary context.
            group = ('unknown_age' if year is None else 'newer_2015' if year >= 2015 else 'established_2005' if year >= 2005 else 'older')
            cohorts[group][(name, dong, parcel)].append((amt/10000, rawdate, int(year) if year is not None else None))
        bands=[]
        for cohort, complexes in cohorts.items():
            # Per-complex medians, not all transactions pooled (large blocks bias).
            medians = [median(r[0] for r in v) for v in complexes.values()]
            if not medians: continue
            order=sorted(complexes.items(), key=lambda item: (-len(item[1]), -median(r[0] for r in item[1])))
            low, middle, high = sorted(medians)[max(0,int(.20*(len(medians)-1)))],median(medians),sorted(medians)[int(.80*(len(medians)-1))]
            count = sum(len(v) for v in complexes.values())
            gap=round(middle-sale,4)
            bands.append({
                'cohort':cohort,'referenceEok':round(middle,4),
                'lowEok':round(low,4),'highEok':round(high,4),
                'saleEok':round(sale,4),'gapEok':gap,
                'gapPct':round(100*gap/middle,1) if middle else 0,
                'complexCount':len(complexes),'transactionCount':count,
                'latestDate':max(d for v in complexes.values() for _,d,_ in v),
                'comparables':[{'name':k[0],'legalDong':k[1], 'jibun':k[2],
                    'transactionCount':len(v),'medianEok':round(median(r[0] for r in v),4),
                    'latestDate':max(r[1] for r in v),'buildYear':v[0][2]}
                    for k,v in order[:8]],
                'note':'동일 시군구 내 유사 면적 거래군. 대상 단지의 실제 시세가 아닙니다.' + (' 준공연도 미확인 거래군.' if cohort == 'unknown_age' else ''),
            })
        bands.sort(key=lambda b: (b['cohort']!='newer_2015',b['cohort']=='unknown_age',-b['complexCount'],-b['transactionCount']))
        # Broad signal: no strict distance/count gate, but a single weak source
        # cannot become a verified market price.
        promising=[b for b in bands if b['gapEok']>=1.0 and b['gapPct']>=8]
        promising.sort(key=lambda b:(-b['gapEok'],-b['complexCount']))
        result.append({'type':typ,'areaSqm':round(area,2),'areaSource':source,
          'saleEok':round(sale,4),'bands':bands,'discovery':promising[0] if promising else None})
    # Always expose a *context reference* when transactions exist, even when
    # the distribution shows no positive upside. Keep it distinct from lotto signal.
    contextual=[]
    for r in result:
        observed=sorted(r['bands'],key=lambda b:(b['cohort']=='unknown_age',b['cohort']!='newer_2015',-b['complexCount'],-b['transactionCount']))
        if observed: contextual.append({'type':r['type'],**observed[0]})
    contextual.sort(key=lambda b:(-b['complexCount'],-b['transactionCount'], -b['gapEok']))
    hits=[{'type':r['type'],**r['discovery']} for r in result if r['discovery']]
    hits.sort(key=lambda x:(-x['gapEok'],-x['complexCount']))
    return {'version':'v47-broad-district-cohorts','status':'observed' if any(r['bands'] for r in result) else 'no_comparable_sales',
      'regionScope':'same_lawd_cd_not_distance_verified','byType':result,'candidate':bool(hits),
      'bestSignal':hits[0] if hits else None,'contextReference':contextual[0] if contextual else None,'candidateCount':len(hits),
      'evidenceCount':sum(len(r['bands']) for r in result), 'transactionCount':sum(b['transactionCount'] for r in result for b in r['bands']),
      'disclaimer':'거래군은 시군구 단위 탐색이며 입지·층·연식 차이를 완전히 반영하지 않습니다. 가격 차이는 예상 수익이 아닙니다.'}
