"""v49 general purpose new-supply opportunity discovery.

Reported transaction *observations*, not valuation or investment returns.
Never mix supply unit types/areas or rights prices into ordinary apartment prices.
No coordinate or legal-dong equality requirement. Avoid old stock dominating new stock.
"""
from collections import defaultdict
from datetime import date, datetime
from statistics import median
import re


def positive(value):
    try:
        x = float(str(value).replace(',', ''))
        return x if 0 < x < 1_000_000_000 else None
    except (ValueError, TypeError):
        return None


def area_of(model):
    n = positive(model.get('exclusiveAreaSqm'))
    if n and 10 <= n <= 300: return n, 'api'
    # The model code can represent a supply sub-type, e.g. '084.0000D'.
    raw = str(model.get('type') or model.get('modelNo') or '')
    m = re.match(r'^0*(\d{2,3}(?:\.\d+)?)', raw)
    n = positive(m.group(1)) if m else None
    if n and 10 <= n <= 300: return n, 'type_estimate'
    return None, 'missing'


def project_name(value):
    s = re.sub(r'\([^)]*\)', '', str(value or ''))
    s = re.sub(r'[^가-힣a-zA-Z0-9]', '', s).lower()
    return s


def match_project(name, complex_name):
    a, b = project_name(name), project_name(complex_name)
    return bool(a and b and (a == b or (len(a) >= 7 and len(b) >= 7 and (a in b or b in a))))


def quantile(nums, fraction):
    arr = sorted(nums)
    if not arr: return None
    if len(arr)==1: return arr[0]
    idx = (len(arr)-1)*fraction
    lo, hi = int(idx), min(int(idx)+1,len(arr)-1)
    return arr[lo]*(hi-idx)+arr[hi]*(idx-lo)


def analyse(project, apartment_rows, rights_rows=None, today=None):
    today = today or date.today()
    if isinstance(today, datetime): today = today.date()
    if isinstance(today,str): today = date.fromisoformat(today)
    output = []
    all_signals = []
    for model in project.get('housingTypes') or []:
        kind = str(model.get('type') or model.get('modelNo') or '').strip()
        sale = positive(model.get('priceEok'))
        area, area_basis = area_of(model)
        if not kind or not sale: continue
        # Incomplete area: preserve unit and supply price, but do not match to
        # another type's transactions or invent its market price.
        if not area:
            output.append({'type':kind,'saleEok':round(sale,4),'areaSqm':None,'status':'area_unknown', 'bands':[]})
            continue
        grouped=defaultdict(lambda:defaultdict(list))
        for row in apartment_rows or []:
            trade_area=positive(row.get('exclusiveAreaSqm'))
            if not trade_area or abs(trade_area-area)>max(3.0,area*.08):continue
            rawdate=str(row.get('dealDate') or '')[:10]
            try: days=(today-date.fromisoformat(rawdate)).days
            except ValueError:continue
            if not 0<=days<=540:continue
            manwon=positive(row.get('dealAmountManwon'))
            if not manwon:continue
            name=str(row.get('apartment') or '').strip()
            if not name:continue
            year=positive(row.get('buildYear'))
            year=int(year) if year and 1970<=year<=today.year+2 else None
            age=(today.year-year) if year else None
            # Real age (relative year) rather than an obsolete hard-coded 2015 cutoff.
            cohort='age_unknown' if age is None else 'new_0_5' if age<=5 else 'recent_6_12' if age<=12 else 'mid_13_20' if age<=20 else 'old_21_plus'
            if match_project(project.get('name'),name):cohort='same_name'
            key=(name,str(row.get('legalDong') or ''),str(row.get('jibun') or ''))
            grouped[cohort][key].append((manwon/10000,rawdate,year))
        bands=[]
        for cohort, complexes in grouped.items():
            records=[]
            for key, vals in complexes.items():
                v=median(x[0] for x in vals)
                records.append({'name':key[0], 'legalDong':key[1], 'jibun':key[2],
                    'medianEok':round(v,4),'count':len(vals),
                    'latestDate':max(x[1] for x in vals),'buildYear':vals[0][2]})
            per_complex=[r['medianEok'] for r in records]
            # q75 is a screening reference for new-supply opportunity: it is
            # NOT a new-build appraisal, never a fabricated premium multiplier.
            center=median(per_complex)
            upper=quantile(per_complex,.75)
            measured=upper if cohort in ('same_name','new_0_5','recent_6_12') else center
            count=sum(len(v) for v in complexes.values())
            gap=measured-sale
            band={'cohort':cohort,'referenceEok':round(measured,4), 'medianEok':round(center,4),
              'upperQuartileEok':round(upper,4),'minEok':round(min(per_complex),4),
              'maxEok':round(max(per_complex),4),'saleEok':round(sale,4),
              'gapEok':round(gap,4),'gapPct':round(gap/measured*100,1),
              'complexCount':len(records),'transactionCount':count,
              'latestDate':max(x['latestDate'] for x in records),
              'comparables':sorted(records,key=lambda x:(-x['medianEok'],-x['count']))[:12],
              'basis':'신축·준신축 단지별 거래 중 상위 25% 가격' if cohort in ('same_name','new_0_5','recent_6_12') else '단지별 거래 중앙값',
              'confidence':'review' if len(records)<2 or count<3 else 'observed',
              'notValuation':True}
            bands.append(band)
        order={'same_name':0,'new_0_5':1,'recent_6_12':2,'mid_13_20':3,'age_unknown':4,'old_21_plus':5}
        bands.sort(key=lambda x:(order.get(x['cohort'],7),-x['complexCount']))
        # Rights and apartment trades are never averaged or merged.
        rights=[]
        for r in rights_rows or []:
            if not match_project(project.get('name'),r.get('apartment')):continue
            ra=positive(r.get('exclusiveAreaSqm'))
            if not ra or abs(ra-area)>max(1.8,area*.025):continue
            dt=str(r.get('dealDate') or '')[:10]
            try:days=(today-date.fromisoformat(dt)).days
            except ValueError:continue
            if not 0<=days<=540:continue
            amount=positive(r.get('dealAmountManwon'))
            if amount: rights.append({'referenceEok':round(amount/10000,4),'date':dt,'kind':str(r.get('rightType') or '분양·입주권'),'areaSqm':round(ra,2)})
        rights.sort(key=lambda x:x['date'],reverse=True)
        # Import separately validated official right records and newspaper
        # leads supplied by the existing ledger. Always exact type only.
        extra=[]
        op=project.get('opportunity') or {}
        for key,source,price_key in [('rightsEvidence','신고 분양·입주권','rightsMedianEok'),('reportedRightsEvidence','보도 입주권','reportedRightsEok')]:
            for item in op.get(key) or []:
                if str(item.get('type'))!=kind: continue
                value=positive(item.get(price_key))
                if value:extra.append({'kind':source,'referenceEok':round(value,4),
                    'date':str(item.get('dealDate') or item.get('latestDate') or ''),
                    'url':str(item.get('sourceUrl') or ''),'confidence':'review'})
        potential=[]
        for b in bands:
            if b['gapEok']>0 and b['gapPct']>=5:
                # Old inventory can be shown but is not representative of the new supply.
                score={'same_name':100,'new_0_5':90,'recent_6_12':70,'mid_13_20':40,'age_unknown':24,'old_21_plus':10}[b['cohort']]
                potential.append({'type':kind,'saleEok':round(sale,4),'referenceEok':b['referenceEok'],
                   'gapEok':b['gapEok'],'gapPct':b['gapPct'],'source':b['cohort'],
                   'sourceLabel':b['basis'],'confidence':b['confidence'],'importance':score+min(28,b['gapEok']*2),
                   'count':b['transactionCount'],'date':b['latestDate'],'category':'apartment'})
        for item in rights+extra:
            ref=item['referenceEok'];gap=ref-sale
            if gap>0 and gap/ref>=.05:
                official=item in rights
                potential.append({'type':kind,'saleEok':round(sale,4),'referenceEok':ref,
                    'gapEok':round(gap,4),'gapPct':round(gap/ref*100,1),
                    'source':'rights_api' if official else 'rights_report',
                    'sourceLabel':item.get('kind','입주권'),'confidence':'review',
                    'importance':(110 if official else 90)+min(35,gap*2),'count':1,
                    'date':item['date'],'category':'rights','url':item.get('url','')})
        # Keep widest financially interesting discovery signals, but the displayed
        # comparison MUST be chosen by relevance first (not the largest amount).
        potential.sort(key=lambda x:(-x['importance'],-x['gapEok']))
        all_signals.extend(potential)
        output.append({'type':kind,'areaSqm':round(area,2),'areaBasis':area_basis,
            'saleEok':round(sale,4),'bands':bands,'rights':rights[:12],
            'extraRights':extra[:12],'signals':potential[:12],
            'status':'observed' if bands or rights or extra else 'no_matched_trades'})
    all_signals.sort(key=lambda x:(-x['importance'],-x['gapEok']))
    lead=all_signals[0] if all_signals else None
    # If a high-value rights gap is present, it is surfaced even when the apartment
    # average is lower, but cannot be used as the apartment 'current price'.
    return {'version':'v49-market-opportunity','status':'observed' if any(x.get('status')=='observed' for x in output) else 'no_matched_trades',
        'byType':output,'bestSignal':lead,'candidate':bool(lead),
        'signalCount':len(all_signals),
        'marketCohortCount':sum(len(x.get('bands',[])) for x in output),
        'rightsSignalCount':sum(1 for x in all_signals if x['category']=='rights'),
        'notes':'모든 공고에 적용하는 주택형별 탐색 엔진. 신축 단지별 거래의 상위 사분위값은 참고 관측가이며, 입주권은 별도입니다. 추정 수익이 아닙니다.'}
