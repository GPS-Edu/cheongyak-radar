"""Display-ready, source-separated opportunity analysis.

Transforms *already collected* official apartment/rights observations into a stable
UI contract. Never invents comparable prices or combines unit types.
"""
from datetime import date, datetime
from math import isfinite

ORDER = {'same_name':0, 'new_0_5':1,'recent_6_12':2,'mid_13_20':3,
         'age_unknown':4,'old_21_plus':5}


def amount(v):
    try:
        x=float(v)
        return x if isfinite(x) and 0 < x < 1000 else None
    except (ValueError, TypeError):
        return None


def summarize(project, market, today=None):
    """A single canonical card result, with opportunity leads separate from baseline."""
    today = today or date.today()
    if isinstance(today, datetime): today=today.date()
    if isinstance(today, str): today=date.fromisoformat(today)
    models={str(m.get('type') or m.get('modelNo') or ''):m for m in (project.get('housingTypes') or [])}
    comparisons=[]
    leads=[]
    for unit in market.get('byType') or []:
        typ=str(unit.get('type') or '')
        sale=amount(unit.get('saleEok'))
        official=amount(models.get(typ,{}).get('priceEok'))
        # Reject a market row if type or sale no longer matches its official model.
        if not typ or official is None or sale is None or abs(official-sale)>.001:
            continue
        for band in unit.get('bands') or []:
            reference=amount(band.get('referenceEok'))
            if reference is None:continue
            cohort=band.get('cohort')
            count=int(band.get('transactionCount') or 0)
            complexes=int(band.get('complexCount') or 0)
            date_str=str(band.get('latestDate') or '')
            try: age=(today-date.fromisoformat(date_str)).days
            except ValueError: age=10000
            if age<0 or age>540:continue
            gap=round(reference-sale,4)
            row={'type':typ,'saleEok':sale,'referenceEok':reference,'gapEok':gap,
                 'cohort':cohort,'category':'apartment','source':cohort,
                 'transactionCount':count,'complexCount':complexes,
                 'latestDate':date_str,'comparables':(band.get('comparables') or [])[:8],
                 'confidence':'medium' if count>=3 and complexes>=2 and cohort in ('new_0_5','recent_6_12') else 'limited',
                 'sameComplexVerified':False,
                 'kind':'거래 관측', 'note':'동일 명칭만으로 동일 단지 주소 검증이 완료된 것은 아닙니다.' if cohort=='same_name' else '생활권·연식·층 차이가 반영되지 않은 참고 거래입니다.'}
            comparisons.append(row)
            if gap>0 and gap/reference>=.05:
                leads.append(row)
        # Rights signals are a DISTINCT evidence class and are never apartment baseline.
        for entry in (unit.get('rights') or [])+(unit.get('extraRights') or []):
            reference=amount(entry.get('referenceEok'))
            if reference is None:continue
            gap=round(reference-sale,4)
            if gap<=0:continue
            src='rights_api' if entry in (unit.get('rights') or []) else 'rights_report'
            lead={'type':typ,'saleEok':sale,'referenceEok':reference,'gapEok':gap,
                  'source':src,'category':'rights','confidence':'limited',
                  'latestDate':str(entry.get('date') or ''),'kind':str(entry.get('kind') or '권리 거래'),
                  'url':str(entry.get('url') or ''), 'note':'권리 거래는 추가분담금과 조건이 달라 일반 매매 가격과 동일하지 않습니다.'}
            leads.append(lead)
    comparisons.sort(key=lambda c:(ORDER.get(c['cohort'],9),-c['complexCount'],-c['transactionCount'],-c['referenceEok']))
    baseline=comparisons[0] if comparisons else None
    # Large leads must not disappear just because a lower old-stock baseline exists.
    # Still never conflate an exploratory lead with the conservative observation.
    leads.sort(key=lambda c:(-c['gapEok'],0 if c['category']=='rights' else 1))
    headline=leads[0] if leads else None
    # A price comparison is not required for the notice to remain visible.
    return {'version':'v52-opportunity-summary','status':'observed' if baseline else 'no_apartment_comparison',
            'baseline':baseline,'headline':headline,'candidate':bool(headline),
            'leadCount':len(leads),'comparisonCount':len(comparisons),
            'comparableTypes':len({c['type'] for c in comparisons}),
            'generatedAt':today.isoformat(),
            'note':'공식 매매 신고 거래 관측과 분양·입주권, 보도 가격을 구분한 사전 탐색 자료. 실제 현재 시세 및 수익 확정 아님.'}
