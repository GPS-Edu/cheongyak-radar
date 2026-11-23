"""Location-API-independent, *administrative-area* transaction observations.

These are not spatially verified comps, valuations or projected profits. Exact
legal-dong equality, size match and dated MOTIE/MOLIT transaction evidence only.
"""
from collections import defaultdict
from datetime import date, datetime
from statistics import median
import re


def as_area(value):
    try:
        n = float(value)
        return n if 10 <= n <= 300 else None
    except (TypeError, ValueError):
        return None


def recent_days(value, today):
    try:
        d = date.fromisoformat(str(value)[:10]); age=(today-d).days
        return age if 0 <= age <= 365 else None
    except (TypeError, ValueError):
        return None


def observe(project, district_rows, legal_dong, today=None):
    """For each sale model, list stable, source-separated same-dong price bands.

    Prefer >=2015 built condos; when unavailable use older condos as *older*
    context, never relabel them as new-build comps.
    """
    today=today or date.today()
    if isinstance(today, datetime): today=today.date()
    if isinstance(today, str): today=date.fromisoformat(today)
    if not legal_dong or not district_rows:
        return {'status':'no_legal_dong' if not legal_dong else 'no_transactions','byType':[]}
    result=[]
    for model in project.get('housingTypes') or []:
        typ=str(model.get('type') or model.get('modelNo') or '')
        area=as_area(model.get('exclusiveAreaSqm'))
        if area is None:
            m=re.match(r'^(\d{2,3}(?:\.\d+)?)',typ)
            area=as_area(m.group(1)) if m else None
        if not area: continue
        groups=defaultdict(list)
        for row in district_rows:
            if str(row.get('legalDong') or '').strip() != legal_dong: continue
            tradearea=as_area(row.get('exclusiveAreaSqm'))
            if tradearea is None or abs(tradearea-area)>2: continue
            age=recent_days(row.get('dealDate'),today)
            if age is None: continue
            try:
                amount=float(row.get('dealAmountManwon'))
                built=int(row.get('buildYear') or 0)
                floor=int(row.get('floor') or 0)
            except (ValueError,TypeError): continue
            if amount <= 0 or built<1980 or floor<2:continue
            name=str(row.get('apartment') or '').strip()
            parcel=str(row.get('jibun') or '').strip()
            if not name or not parcel:continue
            groups[(name, parcel, built>=2015)].append((amount/10000,str(row.get('dealDate')),built,tradearea))
        # At least 2 separate buildings where possible, but a single well-sampled
        # building remains a labelled narrow observation, not a verified market.
        for modern in (True, False):
            cohort=[(key,vals) for key,vals in groups.items() if key[2] == modern]
            cohort=[(key, vals) for key,vals in cohort if len(vals)>=1]
            if not cohort:continue
            sample=sum(len(v) for _,v in cohort)
            if sample<3:continue
            apartment_medians=[median(x[0] for x in v) for _,v in cohort]
            ordered=sorted(cohort, key=lambda kv: -len(kv[1]))
            latest=max(v[1] for _,vals in cohort for v in vals)
            result.append({'type':typ,'areaSqm':area,'legalDong':legal_dong,
              'cohort':'2015년 이후 준공' if modern else '2014년 이전 준공',
              'medianEok':round(median(apartment_medians),4),
              'lowEok':round(min(apartment_medians),4),
              'highEok':round(max(apartment_medians),4),
              'transactionCount':sample,'complexCount':len(cohort),
              'latestDate':latest,'locationVerified':False,
              'method':'same_legal_dong_area_and_build_cohort',
              'comparables':[{'name':k[0],'jibun':k[1], 'count':len(v),
                'medianEok':round(median(x[0] for x in v),4),
                'latestDate':max(x[1] for x in v)} for k,v in ordered[:5]],
              'warning':'같은 법정동 거래 관측값이며 대상 단지와의 거리는 검증되지 않았습니다. 거래가격 차이는 예상 수익이 아닙니다.'})
    return {'status':'observed' if result else 'not_enough_same_dong_trades','byType':result}
