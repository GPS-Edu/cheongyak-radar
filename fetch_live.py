import json
import os
import re
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
# Only districts reliably identified from the two Seoul listing addresses.
# Other places remain unscanned until legal-dong code mapping is validated.
VERIFIED_DISTRICTS = {
    ("서울", "서울특별시 영등포구"): ("11560", "서울 영등포구"),
    ("서울", "서울특별시 종로구"): ("11110", "서울 종로구"),
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


def scan_district_trades(session, key, project, today):
    """Fetch an *unmatched district-wide* sample; never treat as a comparable."""
    address = project.get("district", "")
    match = None
    for (region, prefix), district in VERIFIED_DISTRICTS.items():
        if project["region"] == region and address.startswith(prefix):
            match = district
            break
    if match is None:
        return {"status": "not_scanned", "reason": "시군구 코드 확인 전"}

    lawd_code, district_label = match
    fetched_count = 0
    samples = []
    candidates = []
    # 주택형 코드의 숫자는 실측 전용면적이 아닌 면적형 표기일 수 있습니다.
    target_areas = []
    for model in project.get("housingTypes", []):
        raw_area = model.get("exclusiveAreaSqm")
        if raw_area is None:
            found = re.match(r"^(\d{2,3})(?:\.\d+)?", str(model.get("type", "")))
            raw_area = float(found.group(1)) if found else None
        area = positive_number(raw_area)
        if area is not None:
            target_areas.append(area)
    # 같은 법정동의 유사 면적 거래만 '비교 검토 후보'로 보관
    dong_match = re.search(r"([가-힣]+동(?:\d+가)?)", address)
    target_dong = dong_match.group(1) if dong_match else None
    for yyyymm in recent_months(today, count=3):
        page = 1
        while True:
            response = session.get(
                TRADE_API_URL,
                params={
                    "serviceKey": key,
                    "LAWD_CD": lawd_code,
                    "DEAL_YMD": yyyymm,
                    "pageNo": page,
                    "numOfRows": 1000,
                },
                timeout=35,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            result_code = (root.findtext(".//header/resultCode") or "").strip()
            if result_code not in ("00", "000"):
                detail = root.findtext(".//header/resultMsg") or "unknown error"
                raise RuntimeError(f"Trade API returned {result_code}: {detail}")
            items = root.findall(".//body/items/item")
            total = int((root.findtext(".//body/totalCount") or "0").strip())
            fetched_count += len(items)
            for item in items:
                def field(name):
                    return (item.findtext(name) or "").strip()
                row = {
                    "apartment": field("aptNm"),
                    "legalDong": field("umdNm"),
                    "exclusiveAreaSqm": field("excluUseAr"),
                    "dealAmountManwon": field("dealAmount"),
                    "dealYear": field("dealYear"),
                    "dealMonth": field("dealMonth"),
                    "dealDay": field("dealDay"),
                }
                if len(samples) < 3:
                    samples.append(row)
                area = positive_number(row["exclusiveAreaSqm"])
                amount = positive_number(row["dealAmountManwon"])
                cancelled = bool(field("cdealDay"))
                if (target_dong and row["legalDong"] == target_dong
                    and area is not None and amount is not None
                    and any(abs(area - t) <= 2 for t in target_areas)
                    and not cancelled and row["apartment"]):
                    candidates.append({
                        "apartment": row["apartment"],
                        "legalDong": row["legalDong"],
                        "exclusiveAreaSqm": round(area, 2),
                        "dealAmountManwon": round(amount),
                        "dealDate": (f'{row["dealYear"]}-'
                                     f'{row["dealMonth"].zfill(2)}-'
                                     f'{row["dealDay"].zfill(2)}'),
                    })
            if page * 1000 >= total or not items:
                break
            page += 1

    candidates.sort(key=lambda x: x["dealDate"], reverse=True)
    return {
        "status": "same_dong_area_candidates_unverified" if target_dong else "district_only_unverified",
        "district": district_label,
        "legalDong": target_dong,
        "months": list(recent_months(today, count=3)),
        "transactionCount": fetched_count,
        "sampleTransactions": samples,
        "candidateCount": len(candidates),
        "candidateTransactions": candidates[:15],
        "warning": "같은 법정동·유사 면적(±2㎡) 거래 후보입니다. 면적형 표기는 실측치와 다를 수 있고, 거리·연식·단지 특성·거래 해제 여부 등을 완전히 검증하지 않아 시세·차익 계산에는 쓰지 않습니다.",
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

    # Pilot: scan only 2 known Seoul districts. Do not set project.market.
    for project in projects:
        project["transactionScan"] = scan_district_trades(
            session, key, project, now.date()
        )
        info = project["transactionScan"]
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
