
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = (
    "https://api.odcloud.kr/api/"
    "ApplyhomeInfoDetailSvc/v1/"
    "getAPTLttotPblancDetail"
)

REGIONS = ("서울", "경기", "인천")
PAGE_SIZE = 100


def get_date(value):
    if not value:
        return ""
    value = str(value)[:10]
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


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

        if not isinstance(payload.get("data"), list):
            raise RuntimeError(
                f"{region}: API response has no data list"
            )

        rows = payload["data"]
        results.extend(rows)

        total = int(payload.get("matchCount", 0))
        print(
            f"{region}: page={page}, "
            f"received={len(rows)}, total={total}"
        )

        if not rows or len(results) >= total:
            break

        page += 1

    return results


def convert(row, today):
    region = str(
        row.get("SUBSCRPT_AREA_CODE_NM") or ""
    ).strip()
    name = str(row.get("HOUSE_NM") or "").strip()

    start = get_date(row.get("RCEPT_BGNDE"))
    end = get_date(row.get("RCEPT_ENDDE"))
    announce = get_date(row.get("RCRIT_PBLANC_DE"))

    if region not in REGIONS or not name:
        return None

    # For this initial version, retain only listings
    # with a valid application end date.
    if not start or not end or end < today:
        return None

    house_no = str(
        row.get("HOUSE_MANAGE_NO") or ""
    ).strip()
    notice_no = str(
        row.get("PBLANC_NO") or ""
    ).strip()

    if not house_no or not notice_no:
        return None

    notice_id = f"apt-{house_no}-{notice_no}"
    address = str(
        row.get("HSSPLY_ADRES") or region
    ).strip()

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
        "market": None,
        "marketComparable": False,
        "requirements": None,
        "officialUrl": None,
        "notes": (
            "청약홈 API에서 수집한 공고입니다. "
            "분양가, 청약자격, 공급유형 및 "
            "접수 일정은 원문 공고문에서 확인하세요."
        ),
    }


def main():
    key = os.environ.get("DATA_GO_KR_API_KEY", "")

    if not key:
        raise RuntimeError(
            "DATA_GO_KR_API_KEY is missing"
        )

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.date().isoformat()

    session = requests.Session()
    projects = []
    seen = set()

    for region in REGIONS:
        rows = fetch_region(session, key, region)

        for row in rows:
            item = convert(row, today)
            if not item:
                continue

            if item["id"] in seen:
                continue

            seen.add(item["id"])
            projects.append(item)

    projects.sort(
        key=lambda p: (p["start"], p["name"])
    )

    result = {
        "updated_at": now.isoformat(),
        "projects": projects,
    }

    # Preview only: do not overwrite production data.json.
    Path("data.preview.json").write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("================================")
    print("Preview created successfully")
    print("Current KST date:", today)
    print("Active or upcoming listings:", len(projects))

    for region in REGIONS:
        count = sum(
            p["region"] == region for p in projects
        )
        print(f"{region}: {count}")

    for item in projects[:15]:
        print(
            item["region"],
            item["name"],
            item["start"],
            "~",
            item["end"],
        )


if __name__ == "__main__":
    main()
