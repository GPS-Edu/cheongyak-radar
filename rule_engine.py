"""Conservative, page-cited draft rules extracted from a single identity-matched notice.

No positive legal eligibility verdict is generated. Explicit general requirements
are reported as PRELIMINARY matches for a self-reported profile only.
"""
import re

VERSION = 'v37.1'
SCOPE_MARKERS = re.compile(r'특별공급|신혼부부|다자녀|노부모|생애최초|기관추천|일반공급|1순위|2순위|우선공급|추첨제|가점제|청년|신혼희망타운')
EXCEPTIONS = re.compile(r'다만|예외|제외|불구하고|해당하지 아니|할 수 있|완화|별도|단,|유의|경과규정')
NEGATION = re.compile(r'없음|해당\s*없음|적용\s*안\s*함|비대상|미적용|해당되지\s*않')
YEARS = re.compile(r'(\d{1,2})\s*년(?:\s*(\d{1,2})\s*개월)?')
MONTHS = re.compile(r'(\d{1,2})\s*개월')


def snippet(s, maxlen=400):
    return re.sub(r'\s+', ' ', s).strip()[:maxlen]


def relevant_lines(pages, keyword):
    found = []
    for page, full in pages:
        lines = [re.sub(r'\s+', ' ', line).strip() for line in full.splitlines()]
        for idx, line in enumerate(lines):
            if re.search(keyword, line):
                ctx = ' '.join(lines[max(0,idx-1):min(len(lines),idx+2)])
                if len(ctx) < 5: continue
                found.append({'page': page, 'line': line, 'context': ctx[:500]})
    return found


def _restriction(pages, kind):
    label = '전매제한' if kind == 'resale' else '거주의무'
    regex = (r'전매\s*(?:제한|행위\s*제한)|전매제한기간' if kind=='resale'
             else r'(?:실)?거주\s*의무|거주의무기간|의무거주')
    raw = relevant_lines(pages, regex)
    assessments=[]
    for match in raw:
        line=match['line']; ctx=match['context']
        # Only narrow, explicit property:value phrases, NOT a discussion of regulations.
        named = re.search(regex + r'\s*(?:[:：\-]|은|는)?\s*', line)
        if not named: continue
        tail = line[named.end():].lstrip(' :：-').strip()
        if not tail: continue
        prefix = line[:named.start()]
        if len(prefix)>85 or '예시' in line or '관련 법령' in line: continue
        if EXCEPTIONS.search(tail[:95]) or re.search(r'따라|참조|확인|별표|별도',tail):continue
        status=None; value=None
        m = YEARS.search(tail[:35])
        if m and not re.search(r'까지|이내의\s*범위|최대|최소',tail[:55]):
            value=int(m.group(1))*12+int(m.group(2) or 0);status='period_found'
        elif (m:=MONTHS.search(tail[:30])) and not re.search(r'까지|이내의\s*범위|최대|최소',tail[:55]):
            value=int(m.group(1));status='period_found'
        elif re.match(r'^\s*(?:없음|해당\s*없음|미적용|적용\s*안\s*함|비대상)',tail):
            status='not_applicable_stated'
        if status:
            assessments.append({'status':status,'months':value,'page':match['page'],'text':snippet(ctx)})
    unique={(x['status'],x['months']) for x in assessments}
    if not assessments:
        return {'status':'needs_review','label':label,'reason':'명시적인 기간·적용 여부를 안전하게 확정할 수 없음','evidence':raw[:4]}
    if len(unique)!=1:
        return {'status':'needs_review','label':label,'reason':'문서에 서로 다른 기간·대상 조건이 존재함','evidence':assessments[:6]}
    chosen=assessments[0]
    return {'status':chosen['status'],'months':chosen['months'], 'label':label,
            'reason':'문구 자동 식별(예외·정정공고·주택형별 차이는 별도 검증 필요)',
            'evidence':assessments[:3]}


def _general_eligibility(pages):
    """Only extract direct requirements from obvious '공통 신청자격' headings.
    Special supply paragraphs are excluded, and no positive eligibility verdict is made.
    """
    candidates={}
    ambiguous=[]
    for page,text in pages:
        lines=[snippet(x,350) for x in text.splitlines()]
        scope=False
        for i,line in enumerate(lines):
            if re.search(r'(?:공통|전체)\s*(?:신청|청약)\s*자격|(?:신청|청약)\s*자격\s*\(공통\)',line):
                scope=True
                continue
            if SCOPE_MARKERS.search(line) or re.search(r'^(?:\d+\.|[①②③④⑤])\s*(?:공급|유의사항|자격)',line):
                scope=False
            if not scope or not line or EXCEPTIONS.search(line):continue
            ctx=' '.join(lines[max(i-1,0):i+2]); base={'page':page,'text':snippet(ctx)}
            patterns={
                'householdNoHouse': r'무주택\s*세대\s*구성원(?:이어야|에\s*해당|만\s*신청|일\s*것)',
                'head':r'무주택\s*세대주(?:이어야|에\s*해당|만\s*신청|일\s*것)',
                'account':r'청약\s*통장\s*(?:가입|보유)\s*(?:자|필수|요건)',
            }
            for key,pattern in patterns.items():
                if re.search(pattern,line):candidates.setdefault(key,[]).append({**base,'value':True})
            age=re.search(r'만\s*(19|20|21)\s*세\s*이상',line)
            if age:candidates.setdefault('minAge',[]).append({**base,'value':int(age.group(1))})
            if re.search(r'거주자|해당\s*지역\s*거주',line):ambiguous.append(base)
    rules={}
    for key,items in candidates.items():
        vals={x['value'] for x in items}
        if len(vals)==1:rules[key]={'value':items[0]['value'],'evidence':items[:3]}
        else:ambiguous.extend(items)
    return {'rules':rules,'unknownRequirements':['공급유형별 요건','세대 전체 주택 소유','재당첨 제한','소득·자산·특별공급','우선공급 지역·기간','정정공고'],
            'ambiguous':ambiguous[:5], 'mode':'explicit_common_rules_only',
            'positiveVerdictAllowed':False}


def interpret(pages, *, source_class, source_url):
    if not pages or not source_url:
        return {'version':VERSION,'status':'unavailable','resale':{'status':'needs_review'},
                'occupancy':{'status':'needs_review'},'eligibility':{'rules':{},'positiveVerdictAllowed':False}}
    if source_class=='indexed_pdf_unverified_issuer':
        return {'version':VERSION,'status':'unverified_source','resale':{'status':'needs_review'},
                'occupancy':{'status':'needs_review'},'eligibility':{'rules':{},'positiveVerdictAllowed':False},
                'note':'검색에서 찾은 PDF는 발행처 검증 전이므로 자동 판정에 사용하지 않음'}
    result={'version':VERSION,'status':'preliminary','sourceUrl':source_url,
            'resale':_restriction(pages,'resale'),
            'occupancy':_restriction(pages,'occupancy'),
            'eligibility':_general_eligibility(pages)}
    return result
