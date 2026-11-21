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
MAX_GEOCODE_COMPLEXES = 45
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
    response = session.get(
        MODEL_URL,
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
    address = project.get("district", "")
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


def comparable_report(project, trades, session, kakao_key, geo_cache):
    """거래량 편향 방지: 단지별 중앙값 -> 단지 중앙값의 중앙값.

    실제 인접성 검증은 좌표가 모두 확보된 단지만 가능. 신축(2015+)
    등 기준은 휴리스틱이고, '확정 시세' 및 자동 로또 판정에는 사용하지 않는다.
    """
    models = project.get("housingTypes") or []
    if not models:
        return {"status": "no_models", "reason": "주택형 가격 정보가 없습니다.", "byType": []}
    site_address = site_parcel_address(project)
    site = geocode_address(session, kakao_key, site_address, geo_cache) if site_address else None
    legal_dong = identify_legal_dong(project.get("district", ""))
    scan = project.get("transactionScan") or {}
    district_code = identify_district(project)
    district_rows = trades.get(district_code[0], []) if district_code else []
    groups = []
    for model in models:
        model_type = str(model.get("type") or model.get("modelNo") or "")
        area = positive_number(model.get("exclusiveAreaSqm"))
        verified_area = area is not None
        if area is None:
            found = re.match(r"^(\d{2,3}(?:\.\d+)?)", model_type)
            area = positive_number(found.group(1)) if found else None
        if area is None:
            groups.append({"type": model_type, "status": "area_unknown", "reason": "면적 확인 불가"})
            continue
        eligible = []
        for row in district_rows:
            year = str(row.get("buildYear") or "")
            floor = str(row.get("floor") or "")
            if (abs(row["exclusiveAreaSqm"] - area) > COMPARISON_AREA_TOLERANCE
                    or not year.isdigit() or int(year) < 2015
                    or row.get("dealAmountManwon", 0) <= 0):
                continue
            # 저층 거래는 별도 특수성 때문에 제외 (1층, 지하층 등).
            if floor and floor.lstrip('-').isdigit() and int(floor) < 2:
                continue
            eligible.append(row)
        grouped = defaultdict(list)
        for row in eligible:
            grouped[complex_key(row)].append(row)
        # API 호출수를 과도하게 늘리지 않도록 거래 많은 최근 단지를 우선 선택.
        ranked = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), max(x["dealDate"] for x in kv[1])))
        shortlisted = ranked[:MAX_GEOCODE_COMPLEXES]
        output = []
        for key, rows in shortlisted:
            same_dong = bool(legal_dong and key[1] == legal_dong)
            dist = None
            if site:
                apt_addr = trade_address(project, rows[0])
                coords = geocode_address(session, kakao_key, apt_addr, geo_cache)
                if coords:
                    dist = round(haversine_km(site[0], site[1], coords[0], coords[1]), 2)
            # 좌표가 확보되면 2km 이내만 사용하고 미검증 주소는 제외.
            # 좌표가 없으면 같은 법정동 신축 거래만 '후보'로 보여준다.
            if site and dist is not None:
                if dist > MAX_DISTANCE_KM:
                    continue
                scope = "distance_checked"
            elif same_dong:
                scope = "same_dong_only"
            else:
                continue
            amounts = [x["dealAmountManwon"] for x in rows]
            output.append({
                "apartment": rows[0]["apartment"],
                "legalDong": rows[0]["legalDong"],
                "buildYear": int(rows[0]["buildYear"]),
                "distanceKm": dist,
                "scope": scope,
                "count": len(rows),
                "medianEok": round(median(amounts) / 10000, 4),
                "latestDate": max(x["dealDate"] for x in rows),
                "areaSqm": round(median(x["exclusiveAreaSqm"] for x in rows), 2),
            })
        # 거리 정보가 있을 경우 확인된 단지만 분석; 없으면 법정동만 표시
        verified = [x for x in output if x["scope"] == "distance_checked"]
        same = [x for x in output if x["scope"] == "same_dong_only"]
        selected = verified if verified else same
        selected.sort(key=lambda x: (x["distanceKm"] if x["distanceKm"] is not None else 999, -x["count"], x["apartment"]))
        count = sum(x["count"] for x in selected)
        enough = len(selected) >= MIN_COMPARABLE_COMPLEXES and count >= MIN_COMPARABLE_TRADES
        # 비교단지별 중앙값의 중앙값 사용: 거래 많은 1개 단지가 지표를 독점하지 않음.
        ref_price = round(median(x["medianEok"] for x in selected), 4) if enough else None
        if verified:
            scope = "distance_checked_review"
            label = "반경 2km·2015년 이후 준공 단지 거래 참고"
        elif same:
            scope = "same_dong_modern_review"
            label = "같은 법정동·2015년 이후 준공 단지 거래 참고"
        else:
            scope = "insufficient"
            label = "비교 단지 부족"
        groups.append({
            "type": model_type,
            "areaSqm": area,
            "areaSource": "api" if verified_area else "model_code_estimate",
            "status": scope if enough else "insufficient",
            "scopeLabel": label,
            "referencePriceEok": ref_price,
            "complexCount": len(selected),
            "transactionCount": count,
            "apartmentComparisons": selected[:12],
            "sitePositionChecked": site is not None,
            "distanceCheckedCount": len(verified),
            "referenceIsMarketValue": False,
            "reason": ("단지별 거래 중앙값의 중앙값. 거래량이 한 단지에 쏠리지 않게 계산합니다."
                       if enough else "인근·준공연도·면적 조건을 만족하는 거래가 부족합니다."),
        })
    representative = max(models, key=lambda x: positive_number(x.get("priceEok")) or 0)
    rep_name = str(representative.get("type") or representative.get("modelNo") or "")
    ref = next((x for x in groups if x["type"] == rep_name), None)
    return {
        "status": ref["status"] if ref else "insufficient",
        "modelType": rep_name,
        "referencePriceEok": ref.get("referencePriceEok") if ref else None,
        "scopeLabel": ref.get("scopeLabel") if ref else "비교 대상 없음",
        "transactionCount": ref.get("transactionCount", 0) if ref else 0,
        "apartmentCount": ref.get("complexCount", 0) if ref else 0,
        "distanceChecked": bool(ref and ref.get("distanceCheckedCount")),
        "geocodingEnabled": bool(kakao_key),
        "locationConfirmed": site is not None,
        "byType": groups,
        "marketComparable": False,
        "reason": "참고 거래 분석일 뿐 매물 시세·적정가·확정 수익을 의미하지 않습니다.",
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
    projects = []
    seen = set()
    for region in REGIONS:
        for row in fetch_region(session, key, region):
            item = convert(row, today)
            if not item or item["id"] in seen:
                continue
            seen.add(item["id"])
            projects.append(item)
    projects.sort(key=lambda p: (p["start"], p["name"]))

    for project in projects:
        # Fail the entire update if an API request fails: preserve existing data.json.
        types = fetch_housing_types(session, key, project)
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
            project, district_trades, session, kakao_key, geo_cache
        )
        ref = project["referenceMarket"]
        price_label = (f"{ref['referencePriceEok']}억" if ref.get("referencePriceEok") is not None else "비교 표본 부족")
        print(f"정밀 비교: {project['name']} | {ref.get('status')} | "
              f"{price_label} | {ref.get('apartmentCount', 0)}개 단지 | "
              f"{ref.get('transactionCount', 0)}건 | 거리 확인 {ref.get('distanceChecked')}")
        print(
            f"실거래 조회: {project['name']} | "
            f"{info['status']} | {info.get('transactionCount', 0)}건 | "
            f"동·면적 후보 {info.get('candidateCount', 0)}건 | "
            f"시군구 확장 탐색 {info.get('districtExplorationCount', 0)}건"
        )

    result = {"updated_at": now.isoformat(), "projects": projects}
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
