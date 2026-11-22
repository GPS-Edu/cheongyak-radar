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
    ("인천", "인천광역시 서구"): ("28260", "인천 서구"),
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
        "officialUrl": None,
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


def identify_district(project):
    address = str(project.get("district") or "").strip()
    for (region, prefix), district in VERIFIED_DISTRICTS.items():
        if project["region"] == region and address.startswith(prefix):
            return district
    return None


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
        return {"status": "not_scanned", "reason": "시군구 코드 확인 전"}
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


def opportunity_screen(project):
    """감사 가능한 가격 신호. 거래중앙값은 시세/수익 인증이 아니다.

    우위 신호는 거리 확인+주택형 실측면적+충분한 분산 표본을 충족할 때만.
    무순위 공고의 기회는 가격 신호와 구분해 반드시 노출한다.
    """
    special = project.get("kind") in ("무순위", "잔여세대", "계약취소주택")
    report = project.get("referenceMarket") or {}
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
        "verificationChecklist": issue_list,
        "screenVersion": "v12-evidence-first",
        "reason": ("거리·전용면적·거래 표본·단지별 가격 분산 기준을 충족한 가격 차이 신호입니다. 수익 확정이 아니며 직접 확인해야 합니다."
                   if strong else "확정적인 가격 우위는 검증되지 않았습니다. 무순위 일정과 청약 자격 또는 비교자료를 우선 확인하세요."),
        "disclaimer": "실거래 비교는 분양권의 적정 시세·예상 차익·당첨 가능성을 입증하지 않습니다.",
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
        project["opportunity"] = opportunity_screen(project)
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
        "algorithm": "v12-evidence-first",
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
