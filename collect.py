import json, os, urllib.parse, urllib.request, datetime
from pathlib import Path
KEY = os.environ.get("DATA_GO_KR_KEY", "").strip()
if not KEY:
    raise SystemExit("GitHub Secrets에 DATA_GO_KR_KEY를 등록하세요")
BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/"
TODAY = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
SINCE = (TODAY - datetime.timedelta(days=100)).isoformat()

def fetch(endpoint):
    rows=[]
    for page in range(1,11):
        q={"page":page,"perPage":100,"returnType":"JSON","serviceKey":KEY,"cond[RCRIT_PBLANC_DE::GTE]":SINCE}
        url=BASE+endpoint+"?"+urllib.parse.urlencode(q)
        request=urllib.request.Request(url,headers={"Accept":"application/json","User-Agent":"CheongyakRadar/1.0"})
        with urllib.request.urlopen(request,timeout=30) as r:
            payload=json.load(r)
        data=payload.get("data")
        if not isinstance(data,list):
            raise RuntimeError("API 응답 오류 (data 항목 없음): "+str(payload)[:400])
        rows+=data
        if len(data)<100:break
    return rows

def region(row):
    s=str(row.get("SUBSCRPT_AREA_CODE_NM") or "")+" "+str(row.get("HSSPLY_ADRES") or "")
    for code, aliases in {"서울":("서울","서울특별시"),"경기":("경기","경기도"),"인천":("인천","인천광역시")}.items():
        if any(a in s for a in aliases):return code
    return None

def date_value(row,*keys):
    for k in keys:
        v=row.get(k)
        if v:return str(v)[:10]
    return ""

out={}
for endpoint, default_kind in [("getAPTLttotPblancDetail","일반분양"),("getRemndrLttotPblancDetail","무순위")]:
    for r in fetch(endpoint):
        area=region(r)
        if not area:continue
        end=date_value(r,"SUBSCRPT_RCEPT_ENDDE","RCEPT_ENDDE")
        if end and end < TODAY.isoformat():continue
        start=date_value(r,"SUBSCRPT_RCEPT_BGNDE","RCEPT_BGNDE")
        kind=("계약취소주택" if str(r.get("HOUSE_SECD"))=="06" else default_kind)
        name=str(r.get("HOUSE_NM") or "이름 미상").strip()
        notice=str(r.get("PBLANC_NO") or "")
        manage=str(r.get("HOUSE_MANAGE_NO") or "")
        token=endpoint+":"+manage+":"+notice
        out[token]={"name":name,"region":area,"district":str(r.get("HSSPLY_ADRES") or r.get("SUBSCRPT_AREA_CODE_NM") or area),"kind":kind,"price":None,"announce":date_value(r,"RCRIT_PBLANC_DE"),"start":start,"end":end,"deadline":("마감 "+end if end else "접수 일정 확인"),"notice":notice}
result={"updated_at":datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST"),"projects":sorted(out.values(),key=lambda r:r.get("announce") or "",reverse=True)}
Path("data.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
print("수집 완료:",len(result["projects"]),"개 공고")
