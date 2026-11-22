import json
import math
import os
import re
from collections import defaultdict
from statistics import median
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import xml.etree.ElementTree as ET

API_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/"
API_URL = API_BASE + "getAPTLttotPblancDetail"
MODEL_URL = API_BASE + "getAPTLttotPblancMdl"
REGIONS = ("서울", "경기", "인천")
PAGE_SIZE = 100
TRADE_MONTHS = 6  # 계약월 기준: 당월 포함 6개월
# 거리 확인은 선택사항. GitHub Secret KAKAO_REST_API_KEY가 없으면 지도좌표 미검증.
MAX_DISTANCE_KM = 2.0
MAX_GEOCODE_COMPLEXES = 65  # 단지 수 제한: 위치 확인한 후보를 우선 검토
MIN_COMPARABLE_TRADES = 5
MIN_COMPARABLE_COMPLEXES = 2
COMPARISON_AREA_TOLERANCE = 2.0  # 제곱미터
RIGHTS_API_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcSilvTrade/getRTMSDataSvcSilvTrade"
RIGHTS_MONTHS = 12
TRADE_API_URL = (
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/"
    "getRTMSDataSvcAptTrade"
)
# 법정동 앞 5자리: 국토교통부 실거래가 지역코드. 정확한 주소 접두어만 매칭합니다.
# 신규 지역은 검증된 코드만 추가하며, 매핑이 없는 지역은 조회 보류합니다.
VERIFIED_DISTRICTS = {
    ("서울", "서울특별시 영등포구"): ("11560", "서울 영등포구"),
    ("서울", "서울특별시 종로구"): ("11110", "서울 종로구"),
    ("경기", "경기도 시흥시"): ("41390", "경기 시흥시"),
    ("경기", "경기도 안산시 단원구"): ("41273", "경기 안산시 단원구"),
    ("경기", "경기도 안산시 상록구"): ("41271", "경기 안산시 상록구"),
    ("경기", "경기도 부천시 오정구"): ("41196", "경기 부천시 오정구"),
    ("경기", "경기도 부천시 소사구"): ("41194", "경기 부천시 소사구"),
    ("경기", "경기도 고양시 덕양구"): ("41281", "경기 고양시 덕양구"),
    ("경기", "경기도 남양주시"): ("41360", "경기 남양주시"),
    ("경기", "경기도 광주시"): ("41610", "경기 광주시"),
    ("서울", "서울특별시 송파구"): ("11710", "서울 송파구"),
    # 2026-07-01 행정구역 개편 후 코드 (법정동 코드 앞 5자리).
    ("인천", "인천광역시 서해구"): ("28275", "인천 서해구"),
    ("인천", "인천광역시 검단구"): ("28290", "인천 검단구"),
    # 신규 검단지구 공고 중 행정구 접두어가 없는 공식 사업지구 표기.
    ("인천", "인천 검단신도시"): ("28290", "인천 검단구"),
    ("인천", "인천광역시 부평구"): ("28237", "인천 부평구"),
    ("인천", "인천광역시 계양구"): ("28245", "인천 계양구"),
    ("인천", "인천광역시 연수구"): ("28185", "인천 연수구"),
    ("인천", "인천광역시 남동구"): ("28200", "인천 남동구"),
}


def get_date(value):
    if not value:
        return ""
    value = str(value)[:10]
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def positive_number(value):
    try:
        if value is None or str(value).strip() in ("", "-"):
            return None
        number = float(str(value).replace(",", ""))
        return number if 0 < number < float("inf") else None
    except (ValueError, TypeError):
        return None


def fetch_region(session, key, region):
    results = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                "serviceKey": key,
                "page": page,
                "perPage": PAGE_SIZE,
                "returnType": "JSON",
                "cond[SUBSCRPT_AREA_CODE_NM::EQ]": region,
            },
            timeout=40,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError(f"{region}: API response has no data list")
        total = int(payload.get("matchCount") or 0)
        results.extend(rows)
        print(f"{region}: page={page}, received={len(rows)}, total={total}")
        if not rows or len(results) >= total:
            break
        page += 1
    return results


def classify_supply(row):
    """확인 가능한 청약홈 코드/명칭을 우선 사용하고 자격은 추론하지 않습니다."""
    name = str(row.get("HOUSE_NM") or "")
    house_type = str(row.get("HOUSE_SECD_NM") or "")
    detail_type = str(row.get("HOUSE_DTL_SECD_NM") or "")
    if "신혼희망타운" in house_type or "신혼희망타운" in name:
        return "신혼희망타운", "청약홈 주택구분/단지명", detail_type
    if "공공분양" in name or "공공분양주택" in name:
        return "공공분양", "공고 명칭(공고문 검증 필요)", detail_type
    if detail_type == "국민":
        return "국민주택", "청약홈 주택구분(국민)", detail_type
    if detail_type == "민영":
        return "민영주택", "청약홈 주택구분(민영)", detail_type
    return "공급유형 확인 필요", "미확인", detail_type


def fetch_remaining(session, key):
    """청약홈 무순위·잔여세대 전용 공고 전체 페이지 조회.

    해당 엔드포인트의 접수일 필드가 APT와 다르므로 별도 처리.
    오류가 나면 정상인 것처럼 0건으로 넘기지 않고 업데이트를 중단합니다.
    """
    results = []
    page = 1
    while True:
        response = session.get(
            API_BASE + "getRemndrLttotPblancDetail",
            params={"serviceKey": key, "page": page, "perPage": PAGE_SIZE, "returnType": "JSON"},
            timeout=40,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("무순위 API 응답에 data 목록이 없습니다. 이전 data.json 유지")
        total = int(payload.get("matchCount") or 0)
        results.extend(rows)
        print(f"무순위 API: page={page}, received={len(rows)}, total={total}")
        if not rows or len(results) >= total:
            break
        page += 1
    return results


def official_facts(row):
    """Only echo explicit public API fields; unknown means unknown, not no restriction."""
    names = {
        "RCRIT_PBLANC_DE": "모집공고일",
        "PRZWNER_PRESNATN_DE": "당첨자 발표일",
        "CNTRCT_CNCLS_BGNDE": "계약 시작일",
        "CNTRCT_CNCLS_ENDDE": "계약 종료일",
        "MVN_PREARNGE_YM": "입주 예정월",
    }
    fields = {}
    for key, label in names.items():
        value = str(row.get(key) or "").strip()
        if not value or value in ("-", "null", "None"):
            continue
        if len(value) > 100:
            continue
        fields[key] = {"label": label, "value": value, "source": "청약홈 공고 API"}
    return fields


def verification_status(project):
    """Unknown restrictions/eligibility must never be interpreted as unrestricted."""
    return {
        "eligibility": "official_notice_required",
        "resaleRestriction": "official_notice_required",
        "residencyObligation": "official_notice_required",
        "depositSchedule": "official_notice_required",
        "additionalCosts": "not_included",
        "note": "청약홈 API 날짜·금액은 참고정보. 전매제한·거주의무·자격·계약금 비율은 공고문 원문 미검증.",
    }


def convert_remaining(row, today):
    region = str(row.get("SUBSCRPT_AREA_CODE_NM") or "").strip()
    name = str(row.get("HOUSE_NM") or "").strip()
    # 일부 공고는 특별공급/일반공급 필드만 있고 접수일이 비어 있을 수 있습니다.
    start = get_date(row.get("SUBSCRPT_RCEPT_BGNDE") or row.get("RCEPT_BGNDE")
                     or row.get("GNRL_RCEPT_BGNDE"))
    end = get_date(row.get("SUBSCRPT_RCEPT_ENDDE") or row.get("RCEPT_ENDDE")
                   or row.get("GNRL_RCEPT_ENDDE"))
    if region not in REGIONS or not name or not start or not end or end < today:
        return None
    house_no = str(row.get("HOUSE_MANAGE_NO") or "").strip()
    notice_no = str(row.get("PBLANC_NO") or "").strip()
    if not house_no or not notice_no:
        return None
    raw_type = " ".join(str(row.get(k) or "") for k in
                        ("HOUSE_SECD_NM", "HOUSE_DTL_SECD_NM", "RCRIT_PBLANC_NM", "HOUSE_NM"))
    kind = ("계약취소주택" if any(x in raw_type for x in ("계약취소", "불법행위재공급"))
            else "무순위")
    notice_id = f"rem-{house_no}-{notice_no}"
    return {
        "id": notice_id, "notice": notice_id, "name": name, "region": region,
        "district": str(row.get("HSSPLY_ADRES") or region).strip(),
        "kind": kind, "kindBasis": "청약홈 무순위·잔여세대 API; 세부 유형은 공고문 확인",
        "houseDetailType": str(row.get("HOUSE_DTL_SECD_NM") or ""),
        "announce": get_date(row.get("RCRIT_PBLANC_DE")),
        "start": start, "end": end, "price": None, "housingTypes": [],
        "market": None, "marketComparable": False, "requirements": None,
        "officialUrl": (str(row.get("PBLANC_URL") or "").strip() if str(row.get("PBLANC_URL") or "").strip().startswith("https://") else None),
        "officialFacts": official_facts(row),
        "notes": "무순위·잔여세대 전용 API 수집 공고. 계약취소 여부 및 거주·주택보유 자격은 공고문 확인 필수.",
        "dataCategory": "remaining",
    }


def convert(row, today):
    region = str(row.get("SUBSCRPT_AREA_CODE_NM") or "").strip()
    name = str(row.get("HOUSE_NM") or "").strip()
    start = get_date(row.get("RCEPT_BGNDE"))
    end = get_date(row.get("RCEPT_ENDDE"))
    announce = get_date(row.get("RCRIT_PBLANC_DE"))
    if region not in REGIONS or not name or not start or not end or end < today:
        return None
    house_no = str(row.get("HOUSE_MANAGE_NO") or "").strip()
    notice_no = str(row.get("PBLANC_NO") or "").strip()
    if not house_no or not notice_no:
        return None
    notice_id = f"apt-{house_no}-{notice_no}"
    address = str(row.get("HSSPLY_ADRES") or region).strip()
    official = str(row.get("PBLANC_URL") or "").strip()
    kind, kind_basis, house_detail = classify_supply(row)
    return {
        "id": notice_id,
        "notice": notice_id,
        "name": name,
        "region": region,
        "district": address,
        "kind": kind,
        "kindBasis": kind_basis,
        "houseDetailType": house_detail,
        "announce": announce,
        "start": start,
        "end": end,
        "price": None,
        "housingTypes": [],
        "market": None,
        "marketComparable": False,
        "requirements": None,
        "officialUrl": official if official.startswith("https://") else None,
        "officialFacts": official_facts(row),
        "notes": "청약홈 API에서 수집한 공고입니다. 가격은 주택형별 최고 공급금액이며, 자격과 일정은 모집공고문에서 확인하세요.",
    }


def fetch_housing_types(session, key, project):
    parts = project["id"].split("-")
    if len(parts) != 3:
        return []
    house_no, notice_no = parts[1], parts[2]
    model_url = (API_BASE + "getRemndrLttotPblancMdl"
                 if project.get("dataCategory") == "remaining" else MODEL_URL)
    response = session.get(
        model_url,
        params={
            "serviceKey": key,
            "page": 1,
            "perPage": 100,
            "returnType": "JSON",
            "cond[HOUSE_MANAGE_NO::EQ]": house_no,
            "cond[PBLANC_NO::EQ]": notice_no,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("Invalid housing model response")
    total = int(payload.get("matchCount") or 0)
    if total > len(rows):
        raise RuntimeError("Housing model response truncated; refusing partial prices")
    output = []
    for row in rows:
        amount = positive_number(row.get("LTTOT_TOP_AMOUNT"))
        if amount is None:
            continue
        output.append({
            "type": str(row.get("HOUSE_TY") or "").strip(),
            "modelNo": str(row.get("MODEL_NO") or "").strip(),
            "supplyAreaSqm": positive_number(row.get("SUPLY_AR")),
            "exclusiveAreaSqm": positive_number(row.get("EXCLUSE_AR")),
            "priceEok": round(amount / 10000, 4),
        })
    return output




def recent_months(today, count=3):
    year, month = today.year, today.month
    for _ in range(count):
        yield f"{year:04d}{month:02d}"
        month -= 1
        if month == 0:
            month = 12
            year -= 1


def target_area_types(project):
    """주택형 숫자에서 명목 전용면적형을 추출. 정확한 실측치 아님."""
    result = []
    for model in project.get("housingTypes", []):
        name = str(model.get("type") or model.get("modelNo") or "").strip()
        area = positive_number(model.get("exclusiveAreaSqm"))
        verified = area is not None
        if area is None:
            found = re.match(r"^(\d{2,3}(?:\.\d+)?)", name)
            area = positive_number(found.group(1)) if found else None
        if area is not None:
            result.append({"type": name, "area": area, "verifiedArea": verified})
    return result


def _administrative_aliases(prefix):
    """Only equivalent official-province abbreviations, never fuzzy district inference."""
    options = {prefix}
    for full, short in (("인천광역시 ", "인천 "),
                        ("서울특별시 ", "서울 "),
                        ("경기도 ", "경기 ")):
        if prefix.startswith(full):
            options.add(short + prefix[len(full):])
        if prefix.startswith(short):
            options.add(full + prefix[len(short):])
    return options


def identify_district(project):
    """Resolve verified LAWD_CD from explicit admin divisions anywhere in notice.

    Do not depend on an exact address prefix: official notices often begin with a
    project name and contain the real district in parentheses. Multiple distinct
    matching codes -> ambiguous -> do not scan. District-free project-area names
    are recognized only for explicitly reviewed geographical labels.
    """
    dynamic = project.get("resolvedDistrict")
    if (isinstance(dynamic, (list, tuple)) and len(dynamic) == 2 and
            re.fullmatch(r"\d{5}", str(dynamic[0]))):
        return (str(dynamic[0]), str(dynamic[1]))
    region = str(project.get("region") or "").strip()
    address = str(project.get("district") or "").strip()
    if region not in REGIONS or not address:
        return None
    normalized = re.sub(r"\s+", " ", address)
    found = {}
    for (mapped_region, prefix), district in VERIFIED_DISTRICTS.items():
        if mapped_region != region:
            continue
        # '인천 검단신도시' is not an administrative name; it is handled
        # separately only if it does not conflict with a known district.
        if prefix == '인천 검단신도시':
            continue
        for alias in _administrative_aliases(prefix):
            # Avoid incorrectly matching '서해구' in '서해구청', or a
            # longer place name sharing only a prefix.
            if re.search(r'(?<![가-힣])' + re.escape(alias)
                         + r'(?=$|[^가-힣])', normalized):
                found[district[0]] = district
                break
    if len(found) > 1:
        return None
    if len(found) == 1:
        return next(iter(found.values()))
    # Reviewed new-town alias; require explicit project-area text, not a
    # guess based on similar apartment marketing names.
    if region == '인천' and re.search(r'(?<![가-힣])(?:인천\s+)?검단신도시(?=$|[^가-힣])', normalized):
        return VERIFIED_DISTRICTS[("인천", "인천 검단신도시")]
    return None


# 자동 지역코드 판별: 카카오 로컬 API 법정동 코드의 앞 5자리 사용.
# 신규 구/시가 생겨도 확인된 원본 주소에서만 계산하며 지명 추정은 하지 않는다.
PROVINCE_PREFIXES = {
    "서울": ("서울특별시", "서울"), "경기": ("경기도", "경기"),
    "인천": ("인천광역시", "인천"),
}


def _province_matches(value, region):
    value = str(value or "").replace(" ", "")
    return any(value == x for x in PROVINCE_PREFIXES.get(region, ()))


def _administrative_query_candidates(project):
    """공고에 실제 적힌 지명·주소만 검색한다. 아파트명은 주소로 취급하지 않는다.

    사업지구/괄호/여러 주소를 병기한 공고에서 지명을 찾아낸다.
    결과가 여러 개이면 이후 '동일 코드'로 교차 확인하며 임의 선택하지 않는다.
    """
    region = str(project.get("region") or "").strip()
    raw = re.sub(r"\s+", " ", str(project.get("district") or "")).strip()
    if region not in PROVINCE_PREFIXES:
        return []
    official, abbreviated = PROVINCE_PREFIXES[region]
    # 도로명과 필지 주소 중 정확하게 읽을 수 있는 주소를 우선한다.
    # 괄호 앞의 사업지구 명칭은 뒤에 붙여 검색하지 않는다.
    candidates = []
    for match in re.finditer(r"(?<![가-힣])(?:" + re.escape(official) + "|" +
                             re.escape(abbreviated) + r")\s+", raw):
        fragment = raw[match.start():].split(')')[0].split('(')[0].strip()
        fragment = re.split(r"(?:\s+일원|\s+내\s+|\s+주변|\s+[A-Z]?-?\d+BL)", fragment)[0]
        fragment = re.sub(r"^(?:" + re.escape(abbreviated) + r")\s+", official + " ", fragment)
        # 인천광역시 검단구 이음4로 101, 경기도 성남시 분당구 판교역로 123 등.
        # 유효한 행정명(시/군/구) + 추가 구체정보가 필요.
        parts = fragment.split()
        if len(parts) >= 3 and any(t.endswith(("시", "군", "구")) for t in parts[1:]):
            candidates.append(fragment)
            # 주소 검색이 실패하면 행정구역+읍면동 단계까지 줄여 재시도.
            for i in range(len(parts)-1, 2, -1):
                if parts[i].endswith(("읍", "면", "동", "리", "가")):
                    candidates.append(" ".join(parts[:i+1]))
                    break
    # 축약된 주소(성남시 분당구 / 강남구 / 수지구)도 다룬다.
    # 주소에 실제로 존재하는 시/구만 사용하고 모델이 지명을 새로 생성하지 않는다.
    if not candidates:
        for match in re.finditer(
            r"(?<![가-힣])([가-힣]{2,}(?:시|군))\s+"
            r"([가-힣]{2,}구)(?:\s+([가-힣0-9]{2,}(?:읍|면|동|리)))?", raw):
            candidates.append(official + " " + match.group(0))
    if not candidates:
        for match in re.finditer(r"(?<![가-힣])([가-힣]{2,}구)\s+"
                                 r"([가-힣0-9]{2,}(?:읍|면|동|리))", raw):
            candidates.append(official + " " + match.group(0))
    return list(dict.fromkeys(candidates))[:6]


def _verified_b_code(document, region, query=None):
    address = document.get("address") or {}
    code = str(address.get("b_code") or "")
    if not re.fullmatch(r"\d{10}", code) or code[:5] == "00000":
        return None
    if not _province_matches(address.get("region_1depth_name"), region):
        return None
    expected_prefix = {"서울": "11", "경기": "41", "인천": "28"}.get(region)
    if expected_prefix and not code.startswith(expected_prefix):
        return None
    if query:
        # 행정구역명이 입력에 명시됐으면 결과 주소에도 포함되어야 한다.
        # 예: 용인시 수지구 -> 서울 수지구/경기 다른 구 매칭 금지.
        names = re.findall(r"[가-힣]{1,}(?:시|군|구)",
                           str(query).replace("서울특별시", "").replace("인천광역시", "").replace("경기도", ""))
        actual = str(address.get("region_2depth_name") or "").replace(" ", "")
        if names and not all(name in actual for name in names):
            return None
    # 실제 검색된 도로명 주소도 법정동 주소 객체가 있어야 확인된 것으로 취급.
    label = (str(address.get("region_1depth_name") or "") + " " +
             str(address.get("region_2depth_name") or "")).strip()
    return (code[:5], label)


def resolve_district_dynamic(project, session, kakao_key, cache=None):
    """Kakao 법정동 10자리 -> LAWD_CD 5자리. 불일치/미검증은 보류.

    완전히 동일한 좌표가 아니더라도, *지역코드만* 결정하는 함수.
    위치 검증을 받았다는 의미로 해석하면 안 된다.
    """
    if cache is None:
        cache = {}
    static = identify_district(project)
    if not kakao_key:
        return (static, "verified_static_mapping") if static else (None, "kakao_key_missing")
    candidates = _administrative_query_candidates(project)
    if not candidates:
        return (static, "verified_static_mapping") if static else (None, "no_explicit_address")
    confirmed = {}
    for query in candidates:
        if query not in cache:
            try:
                response = session.get(
                    "https://dapi.kakao.com/v2/local/search/address.json",
                    params={"query": query, "size": 10},
                    headers={"Authorization": "KakaoAK " + kakao_key},
                    timeout=15,
                )
                response.raise_for_status()
                cache[query] = response.json().get("documents") or []
            except (requests.RequestException, ValueError, AttributeError) as exc:
                print("행정구역 자동조회 보류:", type(exc).__name__)
                cache[query] = []
        # 같은 검색어에 대해 상이한 코드가 반환되면 모호한 주소: 사용하지 않음.
        matches = []
        for doc in cache[query]:
            verified = _verified_b_code(doc, project.get("region"), query)
            if verified:
                matches.append(verified)
        codes = {x[0] for x in matches}
        if len(codes) > 1:
            return None, "ambiguous_address_results"
        if len(codes) == 1:
            confirmed[next(iter(codes))] = matches[0]
    if len(confirmed) > 1:
        return None, "conflicting_address_codes"
    if not confirmed:
        return (static, "verified_static_mapping") if static else (None, "address_code_unavailable")
    return next(iter(confirmed.values())), "kakao_legal_dong_code"


def district_resolution_reason(project):
    """Safe diagnostics; no unverified 'nearby market price' fallback."""
    if identify_district(project):
        return "resolved"
    return ("공고 주소에서 관할 시군구를 확정하지 못했습니다. "
            "신규 행정구역·사업지구 표기는 공식 주소 확인이 필요합니다. "
            "주변 거래가격을 임의로 대입하지 않았습니다.")


def identify_legal_dong(address):
    # '문래동3가' / '대장동' 구분. 법정동 불확실 시 자동 시세 계산 금지.
    found = re.findall(r"([가-힣]+동(?:\d+가)?)", address)
    unique = list(dict.fromkeys(found))
    # 부천대장처럼 여러 동이 병기된 공고는 어느 동인지 확정할 수 없습니다.
    return unique[0] if len(unique) == 1 else None


def fetch_district_month(session, key, lawd_code, yyyymm):
    """실거래 API의 모든 페이지를 수집하고 취소 거래를 제외합니다."""
    trades = []
    page = 1
    while True:
        response = session.get(
            TRADE_API_URL,
            params={"serviceKey": key, "LAWD_CD": lawd_code,
                    "DEAL_YMD": yyyymm, "pageNo": page, "numOfRows": 1000},
            timeout=35,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        code = (root.findtext(".//header/resultCode") or "").strip()
        if code not in ("00", "000"):
            msg = root.findtext(".//header/resultMsg") or "unknown error"
            raise RuntimeError(f"Trade API {lawd_code}/{yyyymm}: {code} {msg}")
        items = root.findall(".//body/items/item")
        total = int((root.findtext(".//body/totalCount") or "0").strip())
        for item in items:
            def field(name):
                return (item.findtext(name) or "").strip()
            if field("cdealDay"):  # 취소된 거래 제외
                continue
            area = positive_number(field("excluUseAr"))
            amount = positive_number(field("dealAmount"))
            apt = field("aptNm")
            if not apt or area is None or amount is None:
                continue
            year, month, day = field("dealYear"), field("dealMonth"), field("dealDay")
            if not (year.isdigit() and month.isdigit() and day.isdigit()):
                continue
            trades.append({
                "apartment": apt,
                "legalDong": field("umdNm"),
                "exclusiveAreaSqm": round(area, 4),
                "dealAmountManwon": round(amount),
                "dealDate": f"{year}-{month.zfill(2)}-{day.zfill(2)}",
                "floor": field("floor"),
                "buildYear": field("buildYear"),
                "jibun": field("jibun"),
            })
        if not items or page * 1000 >= total:
            break
        page += 1
    return trades



def parse_rights_xml(content):
    """분양권/입주권 실거래만 파싱. 취소 건, 가격/면적 없는 건은 제외."""
    root = ET.fromstring(content)
    code = (root.findtext(".//header/resultCode") or "").strip()
    if code not in ("00", "000"):
        msg = root.findtext(".//header/resultMsg") or "unknown error"
        raise RuntimeError(f"Rights API: {code} {msg}")
    entries = []
    for item in root.findall(".//body/items/item"):
        def get(*keys):
            for key in keys:
                val = (item.findtext(key) or "").strip()
                if val:
                    return val
            return ""
        if get("cdealDay", "cdealDe") or get("cdealType") in ("O", "Y"):
            continue
        name = get("aptNm", "buildingName", "bldgNm", "aptName")
        amount = positive_number(get("dealAmount", "dealAmt"))
        area = positive_number(get("excluUseAr", "excluUseArea", "exclusiveArea"))
        year, month, day = get("dealYear"), get("dealMonth"), get("dealDay")
        if not name or amount is None or area is None or not (year.isdigit() and month.isdigit() and day.isdigit()):
            continue
        try:
            deal_date = datetime(int(year), int(month), int(day)).date().isoformat()
        except ValueError:
            continue
        kind_raw = get("slerGbn", "rightGbn", "tradeType", "type")
        kind = "입주권" if "입주" in kind_raw else ("분양권" if "분양" in kind_raw else "분양·입주권(세부 미확인)")
        entries.append({"apartment": name, "exclusiveAreaSqm": round(area, 3),
                        "dealAmountManwon": amount, "dealDate": deal_date,
                        "legalDong": get("umdNm"), "floor": get("floor"),
                        "rightType": kind, "jibun": get("jibun")})
    return entries, int(root.findtext(".//body/totalCount") or "0")


def fetch_rights_month(session, key, code, month):
    rows = []
    page = 1
    while True:
        response = session.get(RIGHTS_API_URL, params={"serviceKey":key, "LAWD_CD": code,
            "DEAL_YMD":month, "pageNo":page,"numOfRows":1000}, timeout=35)
        response.raise_for_status()
        batch, total = parse_rights_xml(response.content)
        rows.extend(batch)
        if page * 1000 >= total:
            break
        page += 1
    return rows


def rights_report(project, rights_by_district):
    """동일 단지명 + 같은 시군구 + 공고 주택형 면적으로 분양/입주권 별도 확인.

    지번/동 확인이 안 되는 경우에도 검토 신호는 남기지만 '동일 단지 확정'은 금지.
    """
    result = {"status":"no_matching_trades", "source":"국토교통부 아파트 분양권전매 실거래",
              "dataType":"sale_or_occupancy_right", "byType":[], "reviewOnly":True}
    district = identify_district(project)
    if not district:
        result["status"] = "district_unknown"
        return result
    rows = rights_by_district.get(district[0])
    if rows is None:
        result["status"] = "api_unavailable"
        return result
    name = project.get("name")
    project_dong = identify_legal_dong(project.get("district", ""))
    name_matches = [r for r in rows if _same_complex_name(name, r.get("apartment"))
                    and (not project_dong or not r.get("legalDong") or project_dong == r["legalDong"])]
    for model in target_area_types(project):
        if not model.get("verifiedArea"):
            continue  # 주택형 코드 유추 면적은 억대 가격 신호에 사용 금지
        matches = [r for r in name_matches if abs(r["exclusiveAreaSqm"]-model["area"]) <= 1.0]
        if not matches:
            continue
        matches.sort(key=lambda r:r["dealDate"], reverse=True)
        prices = [r["dealAmountManwon"] for r in matches]
        matched = (project_dong and all(r.get("legalDong") == project_dong for r in matches))
        result["byType"].append({
            "type": model["type"], "areaSqm": model["area"],
            "transactionCount":len(matches), "medianEok":round(median(prices)/10000,4),
            "minEok":round(min(prices)/10000,4),"maxEok":round(max(prices)/10000,4),
            "latestDate":matches[0]["dealDate"],
            "rightTypes":sorted(set(r["rightType"] for r in matches)),
            "sameDistrictConfirmed":True,"legalDongConfirmed":bool(matched),
            "addressVerified":False,"matchingMethod":"name_district_area_review",
            "transactions":[{"date":r["dealDate"],"amountEok":round(r["dealAmountManwon"]/10000,4),
                             "type":r["rightType"],"floor":r["floor"]} for r in matches[:5]],
        })
    result["status"] = "matched_for_review" if result["byType"] else "no_matching_trades"
    result["matchCount"] = sum(x["transactionCount"] for x in result["byType"])
    return result



def attach_reported_rights(project, catalogue, today):
    """검증 가능한 공개 보도 근거. 공공 API 데이터와 섞지 않음."""
    op = project.get("opportunity") or {}
    op["reportedRightsEvidence"] = []
    item = catalogue.get(project.get("id"))
    if not isinstance(item, dict) or item.get("name") != project.get("name"):
        return []
    if today > str(item.get("validThrough", "")):
        return []
    models = project.get("housingTypes") or []
    signals=[]
    for evidence in item.get("reportedTrades") or []:
        reported=positive_number(evidence.get("reportedRightsEok"))
        area=positive_number(evidence.get("areaBandSqm"))
        source=str(evidence.get("sourceUrl") or "")
        if not reported or not area or not source.startswith("https://"):
            continue
        for model in models:
            exact=positive_number(model.get("exclusiveAreaSqm"))
            if exact is None or abs(exact-area)>=1.8:
                continue
            price=positive_number(model.get("priceEok"))
            if price is None:
                continue
            difference=round(reported-price,4)
            pct=round(difference/reported*100,1)
            signals.append({"type":model.get("type"),"salePriceEok":price,
                "reportedRightsEok":reported,"referenceGapEok":difference,"referenceGapPct":pct,
                "dealDate":evidence.get("dealDate"),"areaNote":f"{area:g}㎡대 (공급 타입과 불일치 가능)",
                "sourceTitle":evidence.get("sourceTitle"),"sourceUrl":source,
                "verifiedAsOfficialApi":False,"profitVerified":False})
    op["reportedRightsEvidence"]=signals
    if any(x["referenceGapEok"]>=2 and x["referenceGapPct"]>=10 for x in signals):
        if op.get("tier") not in ("priority_review","rights_trade_review"):
            op["tier"]="rights_trade_review"
            op["label"]="입주권 보도 가격차 검토"
        op["reason"] = "언론에 보도된 동일 단지 전용면적대 입주권 거래가 확인되었습니다. 실제 분양권 가치·추가분담금·거래 취소 여부는 미검증입니다."
    return signals


def attach_rights_opportunity(project):
    """일반 아파트 실거래 판단과 별도로 입주권 기회 신호를 추가한다."""
    op = project.get("opportunity") or {}
    rights = project.get("rightsSales") or {}
    models = {str(x.get("type")):x for x in project.get("housingTypes") or []}
    signals=[]
    for item in rights.get("byType") or []:
        model=models.get(str(item.get("type")))
        price=positive_number((model or {}).get("priceEok"))
        reference=positive_number(item.get("medianEok"))
        if price is None or reference is None:
            continue
        gap=round(reference-price,4)
        percent=round(100*gap/reference,1)
        signals.append({"type":item["type"],"salePriceEok":price,"rightsMedianEok":reference,
                        "referenceGapEok":gap,"referenceGapPct":percent,
                        "transactionCount":item["transactionCount"],"latestDate":item["latestDate"],
                        "rightTypes":item["rightTypes"],"verification":"name_district_area_only",
                        "isInvestmentProfit":False})
    signals.sort(key=lambda x:x["referenceGapEok"],reverse=True)
    op["rightsEvidence"]=signals
    positive=[x for x in signals if x["referenceGapEok"]>=2 and x["referenceGapPct"]>=10]
    if positive and op.get("tier") not in ("priority_review",):
        op["tier"]="rights_trade_review"
        op["label"]="분양·입주권 가격 우위 검토"
        op["reason"]="동일 명칭·시군구·면적의 분양/입주권 거래 가격 신호입니다. 거래권리 종류·동호수·권리가액·추가분담금·계약 상태를 확인해야 합니다. 예상 순이익이 아닙니다."
    project["opportunity"]=op
    return signals




def attach_early_price_signals(project):
    """Small-sample *discovery* leads, never a verified valuation/profit.

    Each signal keeps the exact model, source, area validation, trade count and date.
    Private asking prices are deliberately excluded from server-side ranking.
    """
    op = project.get("opportunity") or {}
    types = {str(m.get("type") or m.get("modelNo") or ""): m
             for m in project.get("housingTypes") or []}
    signals = []

    def collect(rows, source, label, reference_key, count_key, date_key, condition):
        for row in rows:
            if not condition(row):
                continue
            typ = str(row.get("type") or "")
            model = types.get(typ)
            if not model:
                continue
            supply = positive_number(model.get("priceEok"))
            reference = positive_number(row.get(reference_key))
            if supply is None or reference is None:
                continue
            count = int(row.get(count_key) or 0)
            if count < 1:
                continue
            gap = round(reference - supply, 4)
            pct = round(100 * gap / reference, 1)
            if gap < 1.5 or pct < 10:
                continue
            signals.append({
                "type": typ, "source": source, "sourceLabel": label,
                "salePriceEok": round(supply, 4),
                "referenceEok": round(reference, 4),
                "referenceGapEok": gap, "referenceGapPct": pct,
                "transactionCount": count,
                "latestDate": row.get(date_key) or None,
                "verifiedArea": bool(model.get("exclusiveAreaSqm")),
                "reviewOnly": True, "profitVerified": False,
            })

    # Same-complex apartment sales: exact parcel or within 150m and same complex name.
    collect((project.get("sameComplexSales") or {}).get("byType") or [],
            "same_complex_sale", "동일 단지 매매 신고", "medianEok",
            "transactionCount", "latestDate",
            lambda r: r.get("areaSource") == "api")
    # Rights trades: name + legal district (+ sometimes legal dong) + official area.
    collect((project.get("rightsSales") or {}).get("byType") or [],
            "rights_trade", "분양·입주권 신고(단지명 확인 필요)", "medianEok",
            "transactionCount", "latestDate", lambda r: True)
    # Nearby: at least one distance-checked comparand and official area; not a district average.
    reference = project.get("referenceMarket") or {}
    collect(reference.get("byType") or [],
            "nearby_sale", "주변 유사 단지 매매(2km 검증)", "referencePriceEok",
            "transactionCount", "latestComparisonDate",
            lambda r: (reference.get("locationConfirmed") is True
                       and int(r.get("distanceCheckedCount") or 0) > 0
                       and r.get("areaSource") == "api"
                       and not r.get("coverageLimited")
                       and not reference.get("comparisonCoverageLimited")))
    # Source ordering is not a profit or valuation ranking.
    order = {"same_complex_sale": 0, "rights_trade": 1, "nearby_sale": 2}
    signals.sort(key=lambda s: (order.get(s["source"], 9), -s["referenceGapEok"]))
    op["earlyPriceSignals"] = signals[:20]
    op["earlySignalCount"] = len(signals)
    # Do not demote strong price tier, rights tier, or any special notice.
    if signals and op.get("tier") in ("watch", "manual_review", "same_complex_review", "special_supply"):
        op["tier"] = "early_price_review"
        op["label"] = "로또 조기 검토 · 적은 거래 표본"
        op["reason"] = ("공식 매매 또는 분양·입주권 신고거래에서 가격 차이 신호를 찾았습니다. "
                        "1~2건도 검토 대상으로 표시하지만 적정 시세나 수익은 미검증입니다.")
    project["opportunity"] = op
    return signals


def summarize_trades(rows):
    values = [x["dealAmountManwon"] for x in rows]
    buildings = {(x["apartment"], x["jibun"], x["legalDong"]) for x in rows}
    return {
        "count": len(rows),
        "apartmentCount": len(buildings),
        "medianEok": round(median(values) / 10000, 4) if values else None,
        "minEok": round(min(values) / 10000, 4) if values else None,
        "maxEok": round(max(values) / 10000, 4) if values else None,
        "latestDate": max((x["dealDate"] for x in rows), default=None),
    }


def scan_district_trades(project, district_trades, months):
    district = identify_district(project)
    if district is None:
        return {"status": "not_scanned", "reason": district_resolution_reason(project)}
    lawd_code, label = district
    target_dong = identify_legal_dong(project["district"])
    all_trades = district_trades[lawd_code]
    models = target_area_types(project)
    # 법정동이 맞으면 1차 후보, 동일 시군구의 다른 법정동은
    # '범위 확장 탐색 후보'로만 보관합니다. 지리적 인접성은 판단하지 않습니다.
    candidates = []
    by_type = defaultdict(list)
    expanded = []
    expanded_by_type = defaultdict(list)
    for item in all_trades:
        matches = [m for m in models if abs(item["exclusiveAreaSqm"] - m["area"]) <= COMPARISON_AREA_TOLERANCE]
        if not matches:
            continue
        tagged = {**item, "matchingTypes": [m["type"] for m in matches]}
        if target_dong and item["legalDong"] == target_dong:
            candidates.append(tagged)
            for model in matches:
                by_type[model["type"]].append(item)
        else:
            expanded.append(tagged)
            for model in matches:
                expanded_by_type[model["type"]].append(item)

    candidates.sort(key=lambda row: row["dealDate"], reverse=True)
    expanded.sort(key=lambda row: row["dealDate"], reverse=True)
    statistics = []
    for model in models:
        same = by_type.get(model["type"], [])
        recent_built = [x for x in same if x["buildYear"].isdigit() and int(x["buildYear"]) >= 2015]
        others = expanded_by_type.get(model["type"], [])
        statistics.append({
            "type": model["type"],
            "areaSqm": model["area"],
            "areaSource": "api" if model["verifiedArea"] else "model_code_estimate",
            "all": summarize_trades(same),
            "builtSince2015": summarize_trades(recent_built),
            "districtExploration": summarize_trades(others),
            "evaluation": "review_only",
        })
    return {
        "status": "same_dong_area_candidates_unverified" if target_dong else "district_only_unverified",
        "district": label,
        "legalDong": target_dong,
        "months": months,
        "transactionCount": len(all_trades),
        "candidateCount": len(candidates),
        "candidateTransactions": candidates[:80],
        "displayLimit": 80,
        "districtExplorationCount": len(expanded),
        "districtExplorationTransactions": expanded[:30],
        "typeStatistics": statistics,
        "distanceVerified": False,
        "warning": "같은 법정동·면적 비교는 검토 자료이고 다른 동의 거래는 시군구 탐색 자료입니다. 실제 거리, 준공연도, 층, 입지, 전용면적 확인 전에는 주변 시세·예상차익·로또 판정에 사용하지 않습니다.",
    }



def complex_key(row):
    return (str(row.get("apartment") or "").strip(),
            str(row.get("legalDong") or "").strip(),
            str(row.get("jibun") or "").strip())


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    a1, a2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    da = a2 - a1
    h = math.sin(da / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def site_parcel_address(project):
    """공고 주소에 단일 지번이 명시된 경우만 지오코딩에 사용."""
    address = str(project.get("district") or "")
    if any(x in address for x in (" 일원", "지구 내", "블록")):
        # '일원'이 붙어도 명확한 단일 지번이 기재된 경우는 허용.
        pass
    m = re.search(r"^((?:서울특별시|경기도|인천광역시)\s+.*?[가-힣]+동(?:\d+가)?\s+\d+(?:-\d+)?)\s*번지", address)
    if not m:
        return None
    if len(re.findall(r"[가-힣]+동(?:\d+가)?", m.group(1))) != 1:
        return None
    return m.group(1).strip()


def geocode_address(session, kakao_key, address, cache):
    if not kakao_key or not address:
        return None
    if address in cache:
        return cache[address]
    try:
        response = session.get(
            "https://dapi.kakao.com/v2/local/search/address.json",
            params={"query": address},
            headers={"Authorization": "KakaoAK " + kakao_key},
            timeout=15,
        )
        response.raise_for_status()
        docs = response.json().get("documents") or []
        item = docs[0] if docs else None
        result = (float(item["y"]), float(item["x"])) if item else None
        if result and not (33 < result[0] < 39 and 124 < result[1] < 132):
            result = None
    except (requests.RequestException, KeyError, TypeError, ValueError, IndexError) as exc:
        print("위치 조회 보류:", type(exc).__name__)
        result = None
    cache[address] = result
    return result



def site_location(session, kakao_key, project, geo_cache, place_cache):
    """공고의 위치를 보수적으로 확인한다.

    1) 정확한 '동 + 지번'을 우선 사용한다.
    2) 없으면 청약 단지명으로 카카오 장소검색을 수행한다. 장소 이름과
       관할 시군구가 모두 일치하고 서로 모순된 결과가 없는 경우에만 사용.
    검색 실패 또는 결과가 애매하면 좌표를 사용하지 않는다.
    """
    if not kakao_key:
        return None, "api_key_missing", None
    address = site_parcel_address(project)
    if address:
        point = geocode_address(session, kakao_key, address, geo_cache)
        if point:
            return point, "parcel_address", address
    district = identify_district(project)
    if not district:
        return None, "district_unknown", None
    name = str(project.get("name") or "").strip()
    # 긴 별칭·블록명은 장소 검색에서 잘못된 랜드마크와 매칭될 수 있음.
    simple_name = re.sub(r"\([^)]*\)", "", name).strip()
    simple_name = re.sub(r"\s+(?:공공분양주택|신혼희망타운).*$", "", simple_name).strip()
    if len(simple_name) < 5:
        return None, "place_name_unusable", None
    key = (district[0], simple_name)
    if key not in place_cache:
        try:
            resp = session.get(
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                params={"query": simple_name, "size": 10},
                headers={"Authorization": "KakaoAK " + kakao_key}, timeout=12,
            )
            resp.raise_for_status()
            place_cache[key] = resp.json().get("documents") or []
        except (requests.RequestException, ValueError, AttributeError) as exc:
            print("단지 장소조회 보류:", type(exc).__name__)
            place_cache[key] = []
    docs = place_cache[key]
    def norm(s):
        return re.sub(r"[^가-힣a-z0-9]", "", str(s or "").lower())
    wanted = norm(simple_name)
    # 작은 아파트명 공통 접두어만으로는 판정하지 않고 정확한 일치만 허용.
    expected_district = district[1].replace("서울 ", "").replace("경기 ", "").replace("인천 ", "")
    matching = []
    for d in docs:
        place_name = norm(d.get("place_name"))
        addr = str(d.get("road_address_name") or d.get("address_name") or "")
        district_tail = expected_district.split()[-1]
        if place_name != wanted or district_tail not in addr:
            continue
        try:
            lat, lon = float(d["y"]), float(d["x"])
        except (KeyError, ValueError, TypeError):
            continue
        if 33 < lat < 39 and 124 < lon < 132:
            matching.append((lat, lon))
    if len(matching) != 1:
        return None, "ambiguous_or_missing_place", None
    return matching[0], "exact_place_name", simple_name


def _safe_positive_int(v):
    try:
        n = int(str(v))
        return n if n >= 0 else 0
    except (ValueError, TypeError):
        return 0

def trade_address(project, trade):
    """실거래가의 법정동+지번으로 단지 위치를 찾는다. 지번 없으면 제외."""
    jibun = str(trade.get("jibun") or "").strip()
    dong = str(trade.get("legalDong") or "").strip()
    district = identify_district(project)
    if not (district and re.fullmatch(r"\d+(?:-\d+)?", jibun) and dong):
        return None
    prefix = district[1]
    if prefix.startswith("서울 "):
        prefix = prefix.replace("서울 ", "서울특별시 ", 1)
    elif prefix.startswith("경기 "):
        prefix = prefix.replace("경기 ", "경기도 ", 1)
    elif prefix.startswith("인천 "):
        prefix = prefix.replace("인천 ", "인천광역시 ", 1)
    return f"{prefix} {dong} {jibun}"


def comparable_report(project, trades, session, kakao_key, geo_cache, place_cache=None):
    """주택형별 *검토용* 비교: 위치 확인 우선, 동일 지역 거래의 엄격한 분리.

    수집 중 누락된 단지/거래가 있을 수 있으므로 referencePriceEok는
    반드시 참고값이며 market/marketComparable은 여전히 설정하지 않는다.
    """
    if place_cache is None:
        place_cache = {}
    models = project.get("housingTypes") or []
    if not models:
        return {"status": "no_models", "reason": "주택형 가격 정보가 없습니다.", "byType": [],
                "locationConfirmed": False, "locationMethod": "no_models"}
    site, site_method, site_evidence = site_location(
        session, kakao_key, project, geo_cache, place_cache
    )
    district = identify_district(project)
    district_rows = trades.get(district[0], []) if district else []
    legal_dong = identify_legal_dong(project.get("district", ""))
    # 동·지번+아파트명으로 위치 조회. 한 공고의 모든 주택형에서 동일 단지 좌표 재사용.
    groups = []
    district_candidates = defaultdict(list)
    for model in models:
        name = str(model.get("type") or model.get("modelNo") or "")
        area = positive_number(model.get("exclusiveAreaSqm"))
        area_verified = area is not None
        if area is None:
            match = re.match(r"^(\d{2,3}(?:\.\d+)?)", name)
            area = positive_number(match.group(1)) if match else None
        if area is None:
            groups.append({"type": name, "status": "area_unknown", "referencePriceEok": None,
                           "reason": "전용면적을 확인할 수 없습니다."})
            continue
        eligible = []
        for row in district_rows:
            year = _safe_positive_int(row.get("buildYear"))
            floor = _safe_positive_int(row.get("floor"))
            if abs(row["exclusiveAreaSqm"] - area) > COMPARISON_AREA_TOLERANCE:
                continue
            # 준공연도 없는 자료는 검증 불가, 2015 이전 구축은 별도 탐색표만 이용.
            if year < 2015 or floor < 2:
                continue
            eligible.append(row)
        for row in eligible:
            district_candidates[complex_key(row)].append((name, row))
        groups.append({"type": name, "areaSqm": area,
                       "areaSource": "api" if area_verified else "model_code_estimate",
                       "status": "pending"})

    # 우선순위에 거래량을 과도하게 쓰지 않는다: 동일 법정동 -> 최근 거래 ->
    # 비교 가능한 주택형 다양성 -> 거래건수. 실제 거리는 좌표 조회 이후 결정.
    # API 일일 호출을 무제한 쓰지 않기 위해 한 공고당 최대 65단지까지 검사하고
    # 한도에 닿으면 coverageLimited를 표시한다.
    ordered = sorted(district_candidates.items(),
        key=lambda kv: (
            0 if legal_dong and kv[0][1] == legal_dong else 1,
            -max(int(t[1]["dealDate"].replace("-", "")) for t in kv[1]),
            -len({t[0] for t in kv[1]}),
            -len(kv[1]),
            kv[0],
        ))
    inspected = ordered[:MAX_GEOCODE_COMPLEXES]
    limit_hit = len(ordered) > len(inspected)
    positions = {}
    if site:
        for key, tagged in inspected:
            address = trade_address(project, tagged[0][1])
            point = geocode_address(session, kakao_key, address, geo_cache) if address else None
            if point:
                dist = haversine_km(site[0], site[1], point[0], point[1])
                if dist <= MAX_DISTANCE_KM:
                    positions[key] = round(dist, 3)
    for group in groups:
        if group["status"] == "area_unknown":
            continue
        model_name = group["type"]
        comps = []
        for key, tagged in inspected:
            subset = [r for m, r in tagged if m == model_name]
            if not subset:
                continue
            if site:
                if key not in positions:
                    continue  # 좌표 없는 거래를 거리 검증된 것으로 섞지 않음
                distance, scope = positions[key], "distance_checked"
            elif legal_dong and key[1] == legal_dong:
                distance, scope = None, "same_dong_only"
            else:
                continue
            comps.append({
                "apartment": key[0], "legalDong": key[1],
                "buildYear": _safe_positive_int(subset[0].get("buildYear")),
                "distanceKm": distance, "scope": scope,
                "count": len(subset),
                "medianEok": round(median(r["dealAmountManwon"] for r in subset) / 10000, 4),
                "latestDate": max(r["dealDate"] for r in subset),
                "areaSqm": round(median(r["exclusiveAreaSqm"] for r in subset), 2),
            })
        comps.sort(key=lambda x: (x["distanceKm"] if x["distanceKm"] is not None else 999,
                                   -x["count"], x["apartment"]))
        n_trades = sum(x["count"] for x in comps)
        sufficiently_sampled = len(comps) >= MIN_COMPARABLE_COMPLEXES and n_trades >= MIN_COMPARABLE_TRADES
        # 지역코드/주소·면적 확실성 없는 자료는 메인 시세로 간주하지 않음.
        reference = round(median(x["medianEok"] for x in comps), 4) if sufficiently_sampled else None
        if site and comps:
            status, label = "distance_checked_review", "2km 이내·2015년 이후 준공 거래 참고"
        elif comps:
            status, label = "same_dong_modern_review", "같은 법정동·2015년 이후 준공 거래 참고"
        else:
            status, label = "insufficient", "비교 단지 부족"
        group.update({
            "status": status if sufficiently_sampled else "insufficient",
            "scopeLabel": label,
            "referencePriceEok": reference,
            "complexCount": len(comps), "transactionCount": n_trades,
            "apartmentComparisons": comps[:12], "sitePositionChecked": bool(site),
            "distanceCheckedCount": len(comps) if site else 0,
            "referenceIsMarketValue": False,
            "coverageLimited": limit_hit,
            "reason": ("좌표 검증·면적·준공연도 기준을 적용한 단지별 거래 중앙값의 중앙값입니다. "
                       "이는 적정 시세나 확정 차익이 아닙니다."
                       if reference is not None else "거리·면적·연식 조건을 만족하는 비교 단지와 거래가 부족합니다."),
        })
    representative = max(models, key=lambda x: positive_number(x.get("priceEok")) or 0)
    rep_name = str(representative.get("type") or representative.get("modelNo") or "")
    rep = next((x for x in groups if x.get("type") == rep_name), None)
    return {
        "status": rep.get("status") if rep else "insufficient",
        "modelType": rep_name,
        "referencePriceEok": rep.get("referencePriceEok") if rep else None,
        "scopeLabel": rep.get("scopeLabel") if rep else "비교 대상 없음",
        "transactionCount": rep.get("transactionCount", 0) if rep else 0,
        "apartmentCount": rep.get("complexCount", 0) if rep else 0,
        "distanceChecked": bool(rep and rep.get("distanceCheckedCount")),
        "geocodingEnabled": bool(kakao_key),
        "locationConfirmed": bool(site),
        "locationMethod": site_method,
        "siteGeocodingEvidence": site_evidence,
        "comparisonCoverageLimited": limit_hit,
        "candidateComplexesInspected": len(inspected),
        "candidateComplexesTotal": len(ordered),
        "byType": groups, "marketComparable": False,
        "reason": "공식 실거래 기반 검토 자료이며 적정 시세·투자 수익을 보장하지 않습니다.",
    }


def _complex_name_key(value):
    """엄격한 단지명 정규화: 공백·문장부호·'아파트' 접미사만 제거."""
    value = re.sub(r"\([^)]*\)", "", str(value or "")).lower()
    value = re.sub(r"[^가-힣a-z0-9]", "", value)
    return re.sub(r"아파트$", "", value)


def _same_complex_name(project_name, transaction_name):
    """짧거나 흔한 접두어 일치로 같은 단지를 결정하지 않는다."""
    project_key = _complex_name_key(project_name)
    trade_key = _complex_name_key(transaction_name)
    if min(len(project_key), len(trade_key)) < 7:
        return False
    return (project_key == trade_key or
            (min(len(project_key), len(trade_key)) >= 10 and
             (project_key in trade_key or trade_key in project_key)))


def same_complex_report(project, district_trades, session, kakao_key,
                        geo_cache, place_cache=None):
    """동일 단지 거래를 주변 시세와 별도로 수집·표시한다.

    매칭에 반드시 검증된 단지 위치 또는 공고/거래의 정확한 지번이 필요하다.
    단지명만 비슷하면 매칭하지 않으며, 아파트 *매매* 거래만 반환한다.
    신축 예정 단지나 데이터 부족은 정상적인 미확인 결과다.
    """
    district = identify_district(project)
    result = {"status": "not_verified", "source": "국토교통부 아파트 매매 실거래",
              "matchesAreApartmentSales": True, "byType": [],
              "transactionCount": 0, "latestDate": None,
              "warning": "동일 단지 아파트 매매 거래 참고자료입니다. 분양권·입주권 시세나 실제 수익을 뜻하지 않습니다."}
    if not district:
        result["reason"] = district_resolution_reason(project)
        return result
    rows = district_trades.get(district[0], [])
    if not rows:
        result["reason"] = "해당 시군구에 조회된 아파트 매매 거래가 없습니다."
        return result
    models = target_area_types(project)
    if not models:
        result["reason"] = "비교할 공급 주택형 면적이 없습니다."
        return result
    exact_site_parcel = site_parcel_address(project)
    project_dong = identify_legal_dong(project.get("district", ""))
    site, site_method, _ = site_location(
        session, kakao_key, project, geo_cache,
        place_cache if place_cache is not None else {})
    result["siteLocationMethod"] = site_method
    # 지번이 명시된 공고라면 주소 전체를 정규화하여 같은 지번인지 검증.
    parcel_match = None
    if exact_site_parcel:
        m = re.search(r"([가-힣]+동(?:\d+가)?)\s+(\d+(?:-\d+)?)$", exact_site_parcel)
        if m:
            parcel_match = (m.group(1), m.group(2))
    candidate_rows = []
    geo_ok = {}
    for row in rows:
        if not _same_complex_name(project.get("name"), row.get("apartment")):
            continue
        if _safe_positive_int(row.get("floor")) < 2:
            continue
        row_dong = str(row.get("legalDong") or "").strip()
        row_parcel = str(row.get("jibun") or "").strip()
        parcel_confirmed = bool(parcel_match and (row_dong, row_parcel) == parcel_match)
        # 주소가 일치하지 않는 거래는 실제 두 좌표가 충분히 가까워야만 동일 단지 후보.
        coordinate_confirmed = False
        if not parcel_confirmed and site and row_parcel:
            address = trade_address(project, row)
            if address:
                if address not in geo_ok:
                    geo_ok[address] = geocode_address(session, kakao_key, address, geo_cache)
                point = geo_ok[address]
                coordinate_confirmed = bool(point and
                    haversine_km(site[0], site[1], point[0], point[1]) <= 0.15)
        if not parcel_confirmed and not coordinate_confirmed:
            continue
        candidate_rows.append((row, "same_parcel" if parcel_confirmed else "within_150m_and_name"))
    result["matchMethods"] = sorted(set(method for _, method in candidate_rows))
    by_type = []
    for model in models:
        subset = [(r, method) for r, method in candidate_rows
                  if abs(r["exclusiveAreaSqm"] - model["area"]) <= 1.0]
        subset.sort(key=lambda pair: pair[0]["dealDate"], reverse=True)
        if not subset:
            continue
        prices = [pair[0]["dealAmountManwon"] for pair in subset]
        by_type.append({
            "type": model["type"], "areaSqm": model["area"],
            "areaSource": "api" if model["verifiedArea"] else "model_code_estimate",
            "transactionCount": len(subset), "medianEok": round(median(prices)/10000, 4),
            "minEok": round(min(prices)/10000, 4), "maxEok": round(max(prices)/10000, 4),
            "latestDate": subset[0][0]["dealDate"],
            "matchMethods": sorted(set(method for _, method in subset)),
            "recentTransactions": [{"dealDate": r["dealDate"],
              "apartment": r["apartment"], "exclusiveAreaSqm": r["exclusiveAreaSqm"],
              "dealAmountManwon": r["dealAmountManwon"], "floor": r.get("floor"),
              "matchMethod": method} for r, method in subset[:6]],
            "comparisonIsProfit": False,
        })
    result["byType"] = by_type
    result["transactionCount"] = len({id(r) for r, _ in candidate_rows})
    result["latestDate"] = max((r["dealDate"] for r, _ in candidate_rows), default=None)
    result["status"] = "same_complex_sales_found" if by_type else "not_verified"
    result["reason"] = ("동일 단지명과 지번 또는 150m 이내 좌표가 일치한 아파트 매매 실거래입니다."
                        if by_type else "동일 단지·면적·주소 검증을 통과한 아파트 매매 거래가 없습니다.")
    return result


def discovery_screen(project, today):
    """자료 부족과 무관하게 접수 기회(1차)를 모니터링. 이 결과는 가격 검증과 독립적."""
    special = project.get("kind") in ("무순위", "잔여세대", "계약취소주택")
    end, start = get_date(project.get("end")), get_date(project.get("start"))
    days = ((datetime.strptime(end, "%Y-%m-%d").date() -
             datetime.strptime(today, "%Y-%m-%d").date()).days) if end else None
    if days is None:
        phase = "date_unknown"
    elif days < 0:
        phase = "closed"
    elif start and start > today:
        phase = "upcoming"
    else:
        phase = "accepting_or_ending"
    urgent = phase == "accepting_or_ending" and days is not None and 0 <= days <= 3
    start_days = ((datetime.strptime(start, "%Y-%m-%d").date() -
                   datetime.strptime(today, "%Y-%m-%d").date()).days) if start else None
    start_soon = phase == "upcoming" and start_days is not None and 0 < start_days <= 3
    return {
        "stage": "stage1_discovery", "isSpecialSupply": special,
        "phase": phase, "daysToDeadline": days,
        "deadlineSoon": urgent, "startSoon": start_soon,
        "daysToStart": start_days,
        "priority": ("special_deadline" if special and urgent else
                     "special_notice" if special else
                     "deadline" if urgent else "ordinary_notice"),
        "label": ("무순위·재공급 마감 임박" if special and urgent else
                  "무순위·재공급 접수 예정" if special and phase == "upcoming" else
                  "무순위·재공급 기회" if special else
                  "접수 마감 임박" if urgent else
                  "접수 시작 임박" if start_soon else "일반 공고 포착"),
        "eligibilityVerified": False,
        "reason": "기회 포착은 모집공고와 일정에만 근거합니다. 자격·수익 판단과는 별개입니다.",
    }


def opportunity_screen(project):
    """감사 가능한 가격 신호. 거래중앙값은 시세/수익 인증이 아니다.

    우위 신호는 거리 확인+주택형 실측면적+충분한 분산 표본을 충족할 때만.
    무순위 공고의 기회는 가격 신호와 구분해 반드시 노출한다.
    """
    special = project.get("kind") in ("무순위", "잔여세대", "계약취소주택")
    report = project.get("referenceMarket") or {}
    same_complex = project.get("sameComplexSales") or {}
    verified_same_types = {str(row.get("type")): row
                           for row in same_complex.get("byType") or []}
    models = {str(m.get("type") or m.get("modelNo") or ""): m
              for m in project.get("housingTypes") or []}
    observations = []
    strong = []
    caution = []
    for group in report.get("byType") or []:
        model_name = str(group.get("type") or "")
        model = models.get(model_name)
        if not model:
            continue
        price = positive_number(model.get("priceEok"))
        reference = positive_number(group.get("referencePriceEok"))
        if price is None or reference is None:
            continue
        comps = group.get("apartmentComparisons") or []
        n_complex = int(group.get("complexCount") or 0)
        n_trades = int(group.get("transactionCount") or 0)
        distances_confirmed = bool(report.get("locationConfirmed")) and (
            int(group.get("distanceCheckedCount") or 0) >= 3)
        area_verified = group.get("areaSource") == "api"
        no_partial = not (group.get("coverageLimited") or report.get("comparisonCoverageLimited"))
        # 거래가 폭넓게 분포한다면 중앙값이 적정가격을 대표하기 어렵다.
        observed_medians = [positive_number(c.get("medianEok")) for c in comps]
        observed_medians = [x for x in observed_medians if x is not None]
        spread = (max(observed_medians) / min(observed_medians) - 1
                  if len(observed_medians) >= 2 else None)
        usable = bool(distances_confirmed and area_verified and no_partial
                      and n_complex >= 3 and n_trades >= 10
                      and spread is not None and spread <= 0.35)
        gap = round(reference - price, 4)
        pct = round(100 * gap / reference, 1)
        observation = {
            "type": model_name, "priceEok": round(price,4),
            "comparableReferenceEok": round(reference,4),
            "observedGapEok": gap, "observedGapPct": pct,
            "apartmentCount": n_complex, "transactionCount": n_trades,
            "distanceVerified": distances_confirmed, "areaSource": group.get("areaSource"),
            "coverageComplete": no_partial, "complexPriceDispersionPct": (
                round(100*spread,1) if spread is not None else None),
            "qualityGatePassed": usable, "scopeLabel": group.get("scopeLabel"),
            "latestComparisonDate": max((str(c.get("latestDate") or "") for c in comps), default=""),
        }
        observations.append(observation)
        if usable and pct >= 15:
            strong.append(observation)
        elif usable and pct <= -10:
            caution.append(observation)
    # 동일 단지의 직접 거래는 인근 비교보다 먼저 검토하되, 소수 표본과
    # 명목 면적 추정값을 가격 우위 확정 판정에 사용하지 않는다.
    same_evidence = []
    for model_name, group in verified_same_types.items():
        model = models.get(model_name)
        price = positive_number(model.get("priceEok")) if model else None
        ref = positive_number(group.get("medianEok"))
        if price is None or ref is None:
            continue
        delta = round(ref-price,4)
        same_evidence.append({
            "type": model_name, "priceEok": round(price,4),
            "sameComplexMedianEok": round(ref,4),
            "observedGapEok": delta,
            "observedGapPct": round(100*delta/ref,1),
            "transactionCount": int(group.get("transactionCount") or 0),
            "latestDate": group.get("latestDate"),
            "areaVerified": group.get("areaSource")=="api",
            "evidenceStatus": "same_complex_apartment_sales_review",
            "strongEnoughForAutomaticProfitClaim": False,
        })
    same_evidence.sort(key=lambda x: -x["observedGapPct"])
    # 강한 후보가 하나라도 있으면 우선 검토. 모든 비교는 '기회 추정'이 아닌 '실사 대상'.
    strong.sort(key=lambda x: (-x["observedGapPct"],-x["transactionCount"]))
    caution.sort(key=lambda x: (x["observedGapPct"],-x["transactionCount"]))
    observations.sort(key=lambda x: (-x["observedGapPct"],-x["transactionCount"]))
    positives = [x for x in observations if x["observedGapPct"] >= 10]
    if strong:
        tier, label, best = "priority_review", "가격 우위 정밀 검토", strong[0]
    elif special:
        tier, label, best = "special_supply", "무순위·재공급 기회 확인", (positives[0] if positives else None)
    elif caution:
        tier, label, best = "price_caution", "가격 열위 주의", caution[0]
    elif positives:
        tier, label, best = "manual_review", "가격 차이 근거 재확인", positives[0]
    elif same_evidence and same_evidence[0]["observedGapPct"] >= 10:
        tier, label, best = "same_complex_review", "동일 단지 거래 비교 필요", None
    else:
        tier, label, best = "watch", "공고·비교자료 확인", None
    issue_list = []
    if not report.get("locationConfirmed"):
        issue_list.append("청약 단지 위치 확인 필요")
    if report.get("comparisonCoverageLimited"):
        issue_list.append("비교 단지 탐색 한도 도달")
    if not observations:
        issue_list.append("주택형별 비교 거래 표본 부족")
    if observations and not any(x["areaSource"] == "api" for x in observations):
        issue_list.append("전용면적은 주택형 코드 추정치")
    if not project.get("requirements"):
        issue_list.append("모집공고문 청약 자격 미검증")
    if not project.get("risk"):
        issue_list.append("전매제한·거주의무·자금 일정 미검증")
    return {
        "tier": tier, "label": label, "isProfitVerified": False,
        "verifiedEligibility": False, "bestType": best,
        "candidateTypeCount": len(strong),
        "observationTypeCount": len(observations),
        "noticeTypePriority": special,
        "priceCautionCount": len(caution),
        "priceEvidence": observations,
        "sameComplexEvidence": same_evidence,
        "evidenceOrder": ["verified_same_complex_apartment_sales", "distance_checked_nearby_apartment_sales", "unverified_reference"],
        "stage": "stage2_price_verification",
        "verificationChecklist": issue_list,
        "screenVersion": "v13-two-stage-evidence",
        "reason": ("거리·전용면적·거래 표본·단지별 가격 분산 기준을 충족한 가격 차이 신호입니다. 수익 확정이 아니며 직접 확인해야 합니다."
                   if strong else "확정적인 가격 우위는 검증되지 않았습니다. 무순위 일정과 청약 자격 또는 비교자료를 우선 확인하세요."),
        "disclaimer": "실거래 비교는 분양권의 적정 시세·예상 차익·당첨 가능성을 입증하지 않습니다.",
    }



# v20: source-aware readiness summary. No unsupported eligibility/profit assertion.
def build_v20_review(project):
    opportunity = project.get("opportunity") or {}
    same = project.get("sameComplexSales") or {}
    nearby = project.get("referenceMarket") or {}
    discovery = project.get("discovery") or {}
    types = project.get("housingTypes") or []
    prices = [positive_number(x.get("priceEok")) for x in types]
    prices = [x for x in prices if x is not None]
    same_types = same.get("byType") or []
    same_count = sum(int(x.get("transactionCount") or 0) for x in same_types)
    nearby_count = int(nearby.get("transactionCount") or 0)
    near_confirmed = bool(nearby.get("locationConfirmed") and nearby.get("distanceChecked"))
    evidence = (
        "same_complex_apartment_sales" if same_types else
        "distance_checked_apartment_sales" if near_confirmed and nearby_count else
        "unverified_local_reference" if nearby_count else "missing")
    if same_types:
        reason = "동일 단지 매매 확인. 분양권 시세와 동일하다는 뜻은 아닙니다."
    elif near_confirmed and nearby_count:
        reason = "위치 검증한 주변 아파트 매매 비교. 같은 단지 거래는 아닙니다."
    elif nearby_count:
        reason = "거래는 있으나 실제 거리 검증이 없어 가격 판정에 사용하지 않습니다."
    else:
        reason = "검증할 거래가 부족합니다. 신규 택지·단지 주소·실거래 공개 시점 확인 필요."
    category = opportunity.get("tier", "watch")
    # Ranking is a review order, not a probability, expected return or financial recommendation.
    signal = 0
    if category == "priority_review": signal = 4
    elif category in ("manual_review", "same_complex_review"): signal = 2
    elif category == "price_caution": signal = -2
    special = bool(discovery.get("isSpecialSupply"))
    score = (max(-2, signal) * 10 + (15 if special else 0) +
             (10 if discovery.get("deadlineSoon") else 0) +
             (6 if discovery.get("startSoon") else 0) +
             (7 if same_types else 0) + (4 if near_confirmed else 0))
    # Do not pretend a downloaded API URL is the actual source PDF.
    return {
        "version": "v20-evidence-readiness", "reviewScore": score,
        "priceSignal": category, "evidenceLevel": evidence,
        "evidenceReason": reason,
        "sameComplexTradeCount": same_count,
        "nearbyTradeCount": nearby_count,
        "areaVerified": any(x.get("exclusiveAreaSqm") for x in types),
        "priceAvailable": bool(prices),
        "minTypePriceEok": min(prices) if prices else None,
        "maxTypePriceEok": max(prices) if prices else None,
        "deadlineSoon": bool(discovery.get("deadlineSoon")),
        "startSoon": bool(discovery.get("startSoon")),
        "special": special,
        "eligibilityStatus": "requires_official_notice_review",
        "requiredEvidence": [
            "모집공고문 원문 및 공급유형별 자격", "전매제한·거주의무",
            "실제 계약금/중도금/잔금 납부 일정", "취득세·옵션·금융 비용",
            "주택형·입지·거래시점의 비교 적합성",
        ],
        "investmentProfitVerified": False,
    }


def attach_review_evidence(project, catalogue):
    """User-reviewed notice evidence only; cannot silently set requirements.

    Catalogue is keyed by exact notice id, preventing same-named notices mixing.
    Only evidence explicitly marked reviewed is surfaced as reviewed; the raw
    excerpt is not converted into eligibility requirements automatically.
    """
    entry = catalogue.get(project.get("id"), {}) if isinstance(catalogue, dict) else {}
    if not isinstance(entry, dict):
        entry = {}
    source = entry.get("sourceUrl")
    if not isinstance(source, str) or not source.startswith("https://"):
        source = None
    excerpts = []
    for item in (entry.get("excerpts") or [])[:30]:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        body = item.get("text")
        category = item.get("category")
        if (isinstance(page, int) and page >= 1 and isinstance(body, str)
                and 0 < len(body) <= 700 and isinstance(category, str)
                and category in ("eligibility", "resale", "occupancy", "payments", "other")):
            excerpts.append({"category": category, "page": page, "text": body,
                             "reviewed": item.get("reviewed") is True})
    project["noticeEvidence"] = {
        "status": ("reviewed_excerpts" if excerpts and all(x["reviewed"] for x in excerpts)
                   else "excerpts_need_review" if excerpts else "no_source_pdf"),
        "sourceUrl": source, "excerpts": excerpts,
        "eligibilityAutomaticallyConfirmed": False,
        "note": "발췌문은 공고문 검토 보조 자료입니다. AI/키워드 추출만으로 청약 자격을 확정하지 않습니다.",
    }


def main():
    key = os.environ.get("DATA_GO_KR_API_KEY", "")
    if not key:
        raise RuntimeError("DATA_GO_KR_API_KEY is missing")
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.date().isoformat()
    session = requests.Session()
    kakao_key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    geo_cache = {}
    place_cache = {}
    projects = []
    seen = set()
    for region in REGIONS:
        for row in fetch_region(session, key, region):
            item = convert(row, today)
            if not item or item["id"] in seen:
                continue
            seen.add(item["id"])
            projects.append(item)
    # 무순위·잔여세대 전용 API (일반 아파트 API에 포함되지 않음).
    for row in fetch_remaining(session, key):
        item = convert_remaining(row, today)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        projects.append(item)
    print(f"수집된 활성 공고: {len(projects)}건 (무순위 포함)")
    projects.sort(key=lambda p: (p["start"], p["name"]))
    catalogue_path = Path("notice_evidence.json")
    try:
        evidence_catalogue = json.loads(catalogue_path.read_text(encoding="utf-8")) if catalogue_path.exists() else {}
        if not isinstance(evidence_catalogue, dict):
            raise ValueError("notice_evidence.json must be an object keyed by notice ID")
    except (ValueError, OSError) as exc:
        raise RuntimeError("Invalid notice_evidence.json; existing data preserved") from exc

    for project in projects:
        # Fail the entire update if an API request fails: preserve existing data.json.
        try:
            types = fetch_housing_types(session, key, project)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            if project.get("dataCategory") != "remaining":
                raise
            print(f"무순위 주택형 가격 미제공: {project['id']} | {type(exc).__name__}")
            types = []
        project["housingTypes"] = types
        project["price"] = max((x["priceEok"] for x in types), default=None)
        print(f"주택형 조회: {project['name']} | {len(types)}개 | 대표 최고가: {project['price']}")

    # 처음 보는 행정구역도 Kakao의 법정동 코드로 확인 후 실거래 조회에 포함.
    # 캐시는 한 번의 실행에서 여러 공고가 같은 도로·동을 참조할 때 중복 조회 방지.
    administrative_cache = {}
    for project in projects:
        resolved, method = resolve_district_dynamic(
            project, session, kakao_key, administrative_cache)
        if resolved:
            project["resolvedDistrict"] = list(resolved)
        # 앱이 행정구역만 확인한 사실을 실제 단지 좌표 확인으로 오인하지 않도록 분리.
        project["districtResolution"] = {
            "method": method, "code": resolved[0] if resolved else None,
            "label": resolved[1] if resolved else None,
        }
        print(f"지역코드 판별: {project['name']} | "
              f"{resolved[0] if resolved else 'unknown'} | "
              f"{resolved[1] if resolved else '주소 검증 필요'} | {method}")

    # 같은 시군구 API 응답을 공고별로 중복 요청하지 않고 캐시합니다.
    months = list(recent_months(now.date(), count=TRADE_MONTHS))
    district_trades = {}
    codes = sorted({d[0] for project in projects
                    if (d := identify_district(project)) is not None})
    for code in codes:
        combined = []
        for month in months:
            combined.extend(fetch_district_month(session, key, code, month))
        district_trades[code] = combined
        print(f"지역 실거래 수집: {code} | {len(combined)}건")

    # 분양/입주권 거래는 매매와 별도 API. 별도 활용 신청이 안 된 경우에는
    # 공고 업데이트를 멈추지 않고 가격 신호를 보류한다.
    rights_by_district = {}
    for code in codes:
        try:
            all_rights=[]
            for month in recent_months(now.date(), count=RIGHTS_MONTHS):
                all_rights.extend(fetch_rights_month(session, key, code, month))
            rights_by_district[code]=all_rights
            print(f"분양·입주권 실거래: {code} | {len(all_rights)}건")
        except (requests.RequestException, RuntimeError, ValueError, ET.ParseError) as exc:
            rights_by_district[code]=None
            print(f"분양·입주권 조회 보류: {code} | {type(exc).__name__} | 별도 API 활용 승인 확인")

    media_path = Path("media_rights_evidence.json")
    if media_path.exists():
        media_catalogue = json.loads(media_path.read_text(encoding="utf-8"))
        if not isinstance(media_catalogue, dict):
            raise RuntimeError("Invalid media_rights_evidence.json")
    else:
        # v22 two-file deployment: time-limited, explicitly unverified press reference.
        # Only used for the single exact notice key, never treated as official transaction data.
        media_catalogue = {'rem-2026930045-2026930045': {'name': '잠실 래미안아이파크', 'validThrough': '2026-11-26', 'reportedTrades': [{'areaBandSqm': 84, 'reportedRightsEok': 43.2, 'dealDate': '2026-06-30', 'sourceTitle': '한국경제 2026-11-18 보도 (전용 84㎡ 입주권 43억 2천만원)', 'sourceUrl': 'https://www.hankyung.com/article/2026111838756', 'notes': '보도된 입주권 거래는 동일 전용면적대의 사례이며 공급 84D형과 동일 동·층·타입인지 확인되지 않음'}]}}

    for project in projects:
        project["transactionScan"] = scan_district_trades(
            project, district_trades, months
        )
        info = project["transactionScan"]
        # 집계/후보 데이터는 시세 판정에 사용하지 않습니다.
        project["market"] = None  # 검증된 주변 시세는 확인 전
        project["marketComparable"] = False  # 자동 로또 판정 금지
        project["referenceMarket"] = comparable_report(
            project, district_trades, session, kakao_key, geo_cache, place_cache
        )
        project["sameComplexSales"] = same_complex_report(
            project, district_trades, session, kakao_key, geo_cache, place_cache
        )
        project["verification"] = verification_status(project)
        project["discovery"] = discovery_screen(project, today)
        project["opportunity"] = opportunity_screen(project)
        project["rightsSales"] = rights_report(project, rights_by_district)
        rights_signals = attach_rights_opportunity(project)
        media_signals = attach_reported_rights(project, media_catalogue, today)
        early_signals = attach_early_price_signals(project)
        if early_signals:
            print(f"조기 로또 신호: {project['name']} | {len(early_signals)}개 주택형/거래근거 · 표본 적음/추가 검증")
        if media_signals:
            print(f"입주권 보도 근거: {project['name']} | {len(media_signals)}개 주택형 · 공식 API 미검증")
        if rights_signals:
            print(f"입주권 가격 신호: {project['name']} | {len(rights_signals)}개 형 | 최고 참고차이 {rights_signals[0]['referenceGapEok']}억")
        attach_review_evidence(project, evidence_catalogue)
        project["reviewV20"] = build_v20_review(project)
        if project["opportunity"].get("tier") == "early_price_review":
            project["reviewV20"]["reviewScore"] += 22
            project["reviewV20"]["evidenceReason"] += " 소수 실거래 가격 차이: 조기 검토 후보(수익 미확정)."
        if project["opportunity"].get("tier") == "rights_trade_review":
            project["reviewV20"]["reviewScore"] += 45
            project["reviewV20"]["evidenceReason"] += " 분양·입주권 가격 우위 가능성 검토(추가분담금·거래 세부 확인 필요)."
        print(f"동일 단지 검증: {project['name']} | {project['sameComplexSales']['status']} "
              f"| 주택형 {len(project['sameComplexSales']['byType'])}개 "
              f"| 거래 {project['sameComplexSales']['transactionCount']}건")
        print(f"기회 포착: {project['name']} | {project['discovery']['label']} "
              f"| 마감까지 {project['discovery']['daysToDeadline']}일")
        print(f"돈되는 청약 탐색: {project['name']} | {project['opportunity']['label']} "
              f"| 엄격 기준 통과 주택형 {project['opportunity']['candidateTypeCount']}개 "
              f"| 가격 열위 주의 {project['opportunity']['priceCautionCount']}개")
        ref = project["referenceMarket"]
        price_label = (f"{ref['referencePriceEok']}억" if ref.get("referencePriceEok") is not None else "비교 표본 부족")
        print(f"위치 검증: {project['name']} | {ref.get('locationMethod')} | "
              f"거래단지 탐색 {ref.get('candidateComplexesInspected', 0)}/{ref.get('candidateComplexesTotal', 0)} "
              f"| 탐색 제한 {ref.get('comparisonCoverageLimited', False)}")
        print(f"정밀 비교: {project['name']} | {ref.get('status')} | "
              f"{price_label} | {ref.get('apartmentCount', 0)}개 단지 | "
              f"{ref.get('transactionCount', 0)}건 | 거리 확인 {ref.get('distanceChecked')}")
        print(
            f"실거래 조회: {project['name']} | "
            f"{info['status']} | {info.get('transactionCount', 0)}건 | "
            f"동·면적 후보 {info.get('candidateCount', 0)}건 | "
            f"시군구 확장 탐색 {info.get('districtExplorationCount', 0)}건"
        )

    quality_summary = {
        "activeNotices": len(projects),
        "remainingNotices": sum(p.get("dataCategory") == "remaining" for p in projects),
        "preciseSiteLocations": sum(bool((p.get("referenceMarket") or {}).get("locationConfirmed")) for p in projects),
        "priceReviewNotices": sum(p["opportunity"]["tier"] == "priority_review" for p in projects),
        "priceCautionNotices": sum(p["opportunity"]["tier"] == "price_caution" for p in projects),
        "unverifiedProfitNoticeCount": len(projects),
        "tradeMonths": months,
        "sameComplexVerifiedNotices": sum(bool(p["sameComplexSales"]["byType"]) for p in projects),
        "deadlineSoonNotices": sum(p["discovery"]["deadlineSoon"] for p in projects),
        "specialDeadlineSoonNotices": sum(p["discovery"]["isSpecialSupply"] and p["discovery"]["deadlineSoon"] for p in projects),
        "startsSoonNotices": sum(p["discovery"]["startSoon"] for p in projects),
        "verificationPendingNotices": sum(p["verification"]["eligibility"] == "official_notice_required" for p in projects),
        "rightsReviewNotices": sum(bool(p["opportunity"].get("rightsEvidence")) for p in projects),
        "reportedRightsReviewNotices": sum(bool(p["opportunity"].get("reportedRightsEvidence")) for p in projects),
        "rightsApiUnavailableDistricts": sum(v is None for v in rights_by_district.values()),
        "earlyPriceReviewNotices": sum(bool((p.get("opportunity") or {}).get("earlyPriceSignals")) for p in projects),
        "algorithm": "v23-official-small-sample-discovery",
        "reviewedPdfNotices": sum(p["noticeEvidence"]["status"] == "reviewed_excerpts" for p in projects),
        "noticePdfPending": sum(p["noticeEvidence"]["status"] != "reviewed_excerpts" for p in projects),
    }
    print("분석 품질 요약:", json.dumps(quality_summary, ensure_ascii=False))
    result = {"updated_at": now.isoformat(), "projects": projects,
              "analysisQuality": quality_summary}
    Path("data.preview.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("================================")
    print("Preview created successfully")
    print("Current KST date:", today)
    print("Active or upcoming listings:", len(projects))
    for region in REGIONS:
        print(f"{region}: {sum(p['region'] == region for p in projects)}")


if __name__ == "__main__":
    main()
