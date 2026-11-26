"""Server-only GitHub Action. Sends alerts from current live data.json."""
import json, os, sys
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote
import requests
from pywebpush import webpush, WebPushException

BASE = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com").strip()
WEB_URL = os.environ.get("RADAR_WEB_URL", "").strip()

if not all([BASE, KEY, PRIVATE]) or "example.com" in SUBJECT:
    print("Push disabled: Supabase or VAPID configuration missing.")
    sys.exit(0)
if not BASE.startswith("https://") or ".supabase.co" not in BASE:
    raise RuntimeError("SUPABASE_URL must be your project's https://...supabase.co URL")
if KEY.startswith("sb_publishable_"):
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY contains a publishable key. Use sb_secret_... or legacy service_role.")
if not (KEY.startswith("sb_secret_") or KEY.startswith("eyJ")):
    raise RuntimeError("Unexpected Supabase key format. Check the GitHub Secret, not this file.")

raw = json.loads(Path("data.json").read_text(encoding="utf-8"))
projects = raw.get("projects", [])
if not raw.get("updated_at") or raw.get("error") or not isinstance(projects, list) or not projects:
    print("No verified live project data; no alerts sent.")
    sys.exit(0)
try:
    updated = datetime.fromisoformat(raw["updated_at"].replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)) > timedelta(hours=24):
        print("Stale dataset; skip all pushes.")
        sys.exit(0)
except Exception as exc:
    print("Invalid updated_at:", type(exc).__name__)
    sys.exit(0)

session = requests.Session()
headers = {"apikey": KEY, "Content-Type": "application/json"}
# Supabase's new sb_secret_ keys are API keys, NOT Bearer JWTs.
# Only the legacy service_role JWT belongs on the Authorization header.
if KEY.startswith("eyJ"):
    headers["Authorization"] = "Bearer " + KEY
session.headers.update(headers)
print("Supabase auth mode:", "secret apikey" if KEY.startswith("sb_secret_") else "legacy service_role JWT")

def check_response(response, label):
    if response.ok:
        return
    code = "unknown"
    message = "no detail"
    try:
        payload = response.json()
        if isinstance(payload, dict):
            code = str(payload.get("code", payload.get("error_code", "unknown")))[:70]
            message = str(payload.get("message", payload.get("error", "no detail")))[:160]
    except ValueError:
        pass
    # Never print the request headers, response request object or credentials.
    print(f"Supabase {label} failed: HTTP {response.status_code}; code={code}; message={message}")
    response.raise_for_status()

def table_get(name, query=""):
    response = session.get(f"{BASE}/rest/v1/{name}", params=query, timeout=25)
    check_response(response, name)
    return response.json()

def record_exists(user_id, project_id, kind, event_date):
    q = (f"user_id=eq.{quote(user_id, safe='')}"
         f"&project_id=eq.{quote(project_id, safe='')}"
         f"&kind=eq.{kind}&event_date=eq.{event_date}&select=user_id&limit=1")
    return bool(table_get("push_deliveries", q))

def log_delivery(user_id, project_id, kind, event_date):
    response = session.post(
        f"{BASE}/rest/v1/push_deliveries",
        json={"user_id":user_id,"project_id":project_id,"kind":kind,"event_date":event_date},
        timeout=25,
        headers={"Prefer":"resolution=ignore-duplicates"},
    )
    check_response(response, "push_deliveries write")

def is_unsold(p):
    return any(tag in str(p.get("kind","")) for tag in ("무순위","잔여세대","계약취소"))

def safe_eligible(p, profile):
    if p.get("region") not in (profile.get("regions") or []):
        return False
    price = p.get("price")
    if price is not None and str(profile.get("budget","999")) != "999":
        try:
            if float(price) > float(profile["budget"]): return False
        except (TypeError, ValueError):
            return False
    r = p.get("requirements") or {}
    if r.get("noHouse") is True and str(profile.get("homes","unknown")) != "0": return False
    if r.get("householdNoHouse") is True and profile.get("householdHomes") == "yes": return False
    if r.get("head") is True and profile.get("head") != "yes": return False
    if r.get("account") is True and profile.get("account") != "yes": return False
    if r.get("residence") and r["residence"] != profile.get("residence"): return False
    if r.get("minYears") is not None:
        try:
            if profile.get("years") != "unknown" and float(profile["years"]) < float(r["minYears"]): return False
        except (TypeError, ValueError):
            pass
    return True

def lotto_verified(p):
    if p.get("marketComparable") is not True or not p.get("marketSource") or not p.get("marketDate"):
        return False
    try:
        m = float(p["market"])
        price = float(p["price"])
        return m > 0 and price >= 0 and (m-price)/m >= .15
    except (ValueError, TypeError, KeyError):
        return False

preferences = table_get("user_preferences", "select=user_id,profile,settings")
subscriptions = table_get("push_subscriptions", "select=user_id,endpoint,p256dh,auth")
by_user = {}
for sub in subscriptions:
    by_user.setdefault(sub["user_id"], []).append(sub)

today = datetime.now(ZoneInfo("Asia/Seoul")).date()
sent = 0
for pref in preferences:
    uid = pref["user_id"]
    prof = pref.get("profile") or {}
    settings = pref.get("settings") or {}
    devices = by_user.get(uid, [])
    if not devices:
        continue
    for p in projects:
        pid = str(p.get("id") or "")
        if not pid or not safe_eligible(p, prof):
            continue
        if not (is_unsold(p) or lotto_verified(p)):
            continue
        end = p.get("end")
        try:
            days = (date.fromisoformat(str(end)[:10]) - today).days
        except (TypeError, ValueError):
            days = None
        if days is not None and days < 0:
            continue
        new_candidate = (is_unsold(p) and settings.get("unsold",True)
                         or lotto_verified(p) and settings.get("lotto",True))
        kind = None
        if new_candidate:
            try:
                announced = date.fromisoformat(str(p["announce"])[:10])
                age = (today-announced).days
            except (KeyError,TypeError,ValueError):
                age = 999
            if 0 <= age <= 1:
                kind = "new"
        if not kind and days == 1 and settings.get("deadline",True):
            kind = "deadline"
        if not kind:
            continue
        day = str(today if kind == "deadline" else str(p.get("announce",""))[:10])
        if not day or record_exists(uid,pid,kind,day):
            continue
        title = ("내일 마감: " if kind=="deadline" else "새 청약 후보: ") + str(p.get("name","공고"))[:40]
        body = str(p.get("region",""))+" · "+str(p.get("kind",""))+" · 신청 전 공고 확인"
        delivered = False
        for device in devices:
            try:
                webpush(
                    subscription_info={"endpoint":device["endpoint"],
                                       "keys":{"p256dh":device["p256dh"],"auth":device["auth"]}},
                    data=json.dumps({"title":title,"body":body,"project_id":pid},ensure_ascii=False),
                    vapid_private_key=PRIVATE,
                    vapid_claims={"sub":SUBJECT},
                    ttl=3600,
                )
                sent += 1
                delivered = True
            except WebPushException as err:
                status = getattr(getattr(err,"response",None),"status_code",None)
                print("push failed", str(uid)[:8], status)
                if status in (404,410):
                    response = session.delete(f"{BASE}/rest/v1/push_subscriptions",
                                              params={"user_id":"eq."+str(uid),"endpoint":"eq."+device["endpoint"]},
                                              timeout=25)
                    check_response(response, "expired subscription deletion")
        if delivered:
            log_delivery(uid,pid,kind,day)
print("Push notifications delivered:", sent)
