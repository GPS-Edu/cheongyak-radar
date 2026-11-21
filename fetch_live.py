import json
import os
import re
from collections import defaultdict
from statistics import median
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import xml.etree.ElementTree as ET
from calendar import monthrange

API_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/"
API_URL = API_BASE + "getAPTLttotPblancDetail"
MODEL_URL = API_BASE + "getAPTLttotPblancMdl"
REGIONS = ("서울", "경기", "인천")
PAGE_SIZE = 100
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
    return {
        "id": notice_id,
        "notice": notice_id,
        "name": name,
        "region": region,
        "district": address,
        "kind": "일반분양",
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
    # '문래동3가'처럼 숫자 가지번이 있는 동도 포착합니다.
    match = re.search(r"([가-힣]+동(?:\d+가)?)", address)
    return match.group(1) if match else None


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


def scan_district_trades(project, district_trades, months):
    district = identify_district(project)
    if district is None:
        return {"status": "not_scanned", "reason": "시군구 코드 확인 전"}
    lawd_code, label = district
    target_dong = identify_legal_dong(project["district"])
    all_trades = district_trades[lawd_code]
    models = target_area_types(project)
    candidates = []
    by_type = defaultdict(list)
    for item in all_trades:
        if not target_dong or item["legalDong"] != target_dong:
            continue
        matches = [m for m in models if abs(item["exclusiveAreaSqm"] - m["area"]) <= 2]
        if not matches:
            continue
        candidates.append({**item, "matchingTypes": [m["type"] for m in matches]})
        for model in matches:
            by_type[model["type"]].append(item)

    candidates.sort(key=lambda row: row["dealDate"], reverse=True)
    # 통계는 화면에 저장하는 최대 80건이 아니라 전체 후보를 기준으로 산출합니다.
    # 구축/신축의 가격 차이를 단지별로 판별할 수 없으므로, 2015년 이후 준공이라는
    # 간단한 필터를 별도로 명시하고 '시세'나 '예상 차익'으로 확정하지 않습니다.
    statistics = []
    for model in models:
        group = by_type.get(model["type"], [])
        recent = [x for x in group if x["buildYear"].isdigit()
                  and int(x["buildYear"]) >= 2015]
        def summarize(rows):
            values = [x["dealAmountManwon"] for x in rows]
            buildings = sorted({(x["apartment"], x["jibun"]) for x in rows})
            return {
                "count": len(rows),
                "apartmentCount": len(buildings),
                "medianEok": round(median(values) / 10000, 4) if values else None,
                "minEok": round(min(values) / 10000, 4) if values else None,
                "maxEok": round(max(values) / 10000, 4) if values else None,
                "latestDate": max((x["dealDate"] for x in rows), default=None),
            }
        statistics.append({
            "type": model["type"],
            "areaSqm": model["area"],
            "areaSource": "api" if model["verifiedArea"] else "model_code_estimate",
            "all": summarize(group),
            "builtSince2015": summarize(recent),
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
        "typeStatistics": statistics,
        "warning": "법정동·면적 ±2㎡ 참고 통계입니다. 준공연도 2015년 이상 거래도 신축 분양단지와 직접 비교 가능하다고 검증되지 않았습니다. 실제 거리·층·단지 상품성 미검증으로 시세·차익·로또 판정을 보류합니다.",
    }


def main():
    key = os.environ.get("DATA_GO_KR_API_KEY", "")
    if not key:
        raise RuntimeError("DATA_GO_KR_API_KEY is missing")
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.date().isoformat()
    session = requests.Session()
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
    months = list(recent_months(now.date(), count=3))
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
        project["market"] = None
        project["marketComparable"] = False
        print(
            f"실거래 조회: {project['name']} | "
            f"{info['status']} | {info.get('transactionCount', 0)}건 | "
            f"동·면적 후보 {info.get('candidateCount', 0)}건"
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
