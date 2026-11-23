"""Price discovery ledger v41.
Never call a sale/asking/rights headline a certified present market value.
Types must match before calculating a price gap; rights remain signals only.
"""
from datetime import datetime, date
import re


def positive(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, 4) if 0 < x < 1000 else None


def as_date(s):
    s = str(s or '').strip()
    m = re.search(r'(20\d\d)[./-]?(0[1-9]|1[0-2])[./-]?([0-2]\d|3[01])', s)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def build_market_analysis(project, today=None):
    """Produce per-type source-separated comparisons and discovery ranking.

    This is intentionally tolerant of partial data for opportunity discovery,
    but never assigns a valuation or a projected investment profit.
    """
    today = today or date.today()
    if isinstance(today, datetime):
        today = today.date()
    elif isinstance(today, str):
        today = as_date(today) or date.today()
    models = {}
    for m in project.get('housingTypes') or []:
        typ = str(m.get('type') or m.get('modelNo') or '').strip()
        value = positive(m.get('priceEok'))
        if typ and value:
            models[typ] = (m, value)
    o = project.get('opportunity') or {}
    ledger = []
    specs = [
        ('same_apartment', o.get('sameComplexEvidence'), 'sameComplexMedianEok', 'priceEok', 'latestDate', '동일 단지 매매 실거래', 4),
        ('nearby_apartment', o.get('priceEvidence'), 'comparableReferenceEok', 'priceEok', 'latestComparisonDate', '주변 유사 단지 매매 실거래', 2),
        ('rights_reported', o.get('rightsEvidence'), 'rightsMedianEok', 'salePriceEok', 'latestDate', '분양·입주권 신고 거래', 2),
        ('early_observation', o.get('earlyPriceSignals'), 'referenceEok', 'salePriceEok', 'latestDate', '소수 실거래 탐색 신호', 1),
        ('press_rights', o.get('reportedRightsEvidence'), 'reportedRightsEok', 'salePriceEok', 'dealDate', '언론 보도 입주권 거래', 1),
    ]
    seen = set()
    for kind, observations, marketkey, pricekey, datekey, label, starting_grade in specs:
        for x in observations or []:
            if not isinstance(x, dict):
                continue
            typ = str(x.get('type') or '').strip()
            if typ not in models:
                continue
            model, price = models[typ]
            reference = positive(x.get(marketkey))
            claimed_price = positive(x.get(pricekey))
            if not reference or not claimed_price or abs(price - claimed_price) > 0.02:
                continue
            count = max(0, int(x.get('transactionCount') or 0))
            grade = starting_grade
            notes = []
            if kind == 'same_apartment':
                if x.get('areaVerified') is not True:
                    grade = 1
                    notes.append('전용면적 검증 필요')
                if count < 3:
                    grade = min(grade, 3)
                    notes.append('거래 표본 3건 미만')
            elif kind == 'nearby_apartment':
                if x.get('qualityGatePassed') is True:
                    grade = 3
                else:
                    notes.append('위치·거래 표본·단지 차이 검토 필요')
            elif kind == 'rights_reported':
                notes.append('권리·추가분담금·주소 확인 전')
            elif kind == 'press_rights':
                notes.append('언론 보도, 공적 거래원장 미대조')
            else:
                notes.append('소수 거래로 시장 대표성 미확인')
            dated = as_date(x.get(datekey))
            age = (today - dated).days if dated else None
            if age is None:
                grade = min(grade, 2)
                notes.append('거래 기준일 미확인')
            elif age > 365:
                grade = min(grade, 2)
                notes.append('1년 초과 과거 거래')
            elif age < -7:
                grade = min(grade, 1)
                notes.append('거래일 확인 필요')
            elif age > 180:
                notes.append('6개월 초과 거래')
            gap = round(reference - price, 4)
            key = (kind, typ, reference, price)
            if key in seen:
                continue
            seen.add(key)
            ledger.append({
                'kind': kind, 'label': label, 'type': typ,
                'saleEok': price, 'referenceEok': reference,
                'gapEok': gap, 'gapPct': round(100 * gap / reference, 1),
                'grade': grade, 'date': str(x.get(datekey) or ''), 'ageDays': age,
                'count': count, 'notes': notes,
                'url': str(x.get('sourceUrl') or ''),
                'useAsValuation': False,
            })
    # Never treat an untyped quote/asking price as a unit-specific valuation.
    # Authorized/private quotes are displayed separately, not mixed with reported trades.
    quotes = []
    for q in project.get('privateMarketQuotes') or []:
        if not isinstance(q, dict) or not positive(q.get('priceEok')):
            continue
        quotes.append({
            'source': str(q.get('source') or '외부 가격'),
            'kind': str(q.get('kind') or '호가/시세'),
            'priceEok': positive(q.get('priceEok')),
            'checkedAt': str(q.get('checkedAt') or ''),
            'area': str(q.get('area') or ''),
            'unitTypeMatched': q.get('unitTypeMatched') is True,
            'url': str(q.get('evidenceUrl') or ''),
        })
    priority = {'same_apartment': 0, 'nearby_apartment': 1, 'rights_reported': 2, 'early_observation': 3, 'press_rights': 4}
    ledger.sort(key=lambda x: (-x['grade'], priority[x['kind']], -x['count'], -x['gapEok']))
    # Strongest representative reference must come from comparable apartment
    # transaction evidence; rights and articles may never displace it.
    anchors = [x for x in ledger if x['kind'] in ('same_apartment', 'nearby_apartment') and x['grade'] >= 3]
    baseline = anchors[0] if anchors else None
    # Missed-upside prevention: do not bury promising rights/article signals.
    upside = [x for x in ledger if x['gapEok'] >= 1.5 and x['gapPct'] >= 10]
    upside.sort(key=lambda x: (-x['gapEok'], -x['grade']))
    lead = upside[0] if upside else None
    return {
        'version': 'v41-source-separated', 'rows': ledger, 'quotes': quotes[:12],
        'baseline': baseline, 'upsideLead': lead,
        'candidate': bool(upside), 'priceStatus': 'reference_available' if baseline else 'reference_pending',
        'maxObservedGapEok': lead['gapEok'] if lead else None,
        'note': '매매 실거래·분양/입주권·보도·호가·시세는 서로 다른 자료입니다. 단순 가격 차이는 수익이 아닙니다.',
    }
