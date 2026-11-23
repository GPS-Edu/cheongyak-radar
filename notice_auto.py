"""Automatic official notice PDF evidence extraction.

Best effort only: this module extracts verbatim cited passages and never infers legal eligibility.
Requires: requests, PyMuPDF (pip install pymupdf).
"""
from __future__ import annotations
import io
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import ipaddress
import socket

import requests

CATEGORIES = {
    'resale': (r'전매\s*제한', r'전매\s*행위', r'전매제한기간'),
    'occupancy': (r'거주\s*의무', r'실거주\s*의무', r'의무\s*거주'),
    'eligibility': (r'신청\s*자격', r'청약\s*자격', r'무주택\s*세대', r'거주\s*요건', r'재당첨\s*제한', r'청약\s*통장'),
    'payments': (r'계약금', r'중도금', r'잔금'),
}
MAX_PDF_BYTES = 18 * 1024 * 1024
MAX_PAGES = 180

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []; self.current = None
    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a': self.current = {'href': dict(attrs).get('href',''), 'text':''}
    def handle_data(self, data):
        if self.current is not None: self.current['text'] += data
    def handle_endtag(self, tag):
        if tag.lower()=='a' and self.current is not None:
            self.links.append(self.current); self.current = None

def public_https(url):
    p=urlparse(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password: return False
    host=p.hostname.lower()
    if host=='localhost' or host.endswith(('.local','.internal')): return False
    try:
        ips=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        return bool(ips) and all(ipaddress.ip_address(x[4][0]).is_global for x in ips)
    except (OSError, ValueError): return False

def fetch_response(session, url, *, stream=False):
    if not public_https(url): return None
    try:
        response=session.get(url,timeout=(4,12),stream=stream,allow_redirects=False,
                             headers={'User-Agent':'CheongyakRadar/1.0 (official notice evidence)'})
        if response.status_code in (301,302,303,307,308):
            nxt=urljoin(url,response.headers.get('Location',''))
            response.close()
            if not public_https(nxt): return None
            response=session.get(nxt,timeout=(4,12),stream=stream,allow_redirects=False)
        if response.status_code != 200:
            response.close(); return None
        return response
    except requests.RequestException:
        return None

def find_pdf_urls(session, official_url, notice_name):
    if not public_https(official_url): return []
    if urlparse(official_url).path.lower().endswith('.pdf'): return [official_url]
    response=fetch_response(session, official_url)
    if response is None: return []
    try:
        if 'pdf' in response.headers.get('Content-Type','').lower(): return [official_url]
        if 'html' not in response.headers.get('Content-Type','').lower(): return []
        if len(response.content)>2_000_000: return []
        parser=LinkParser();parser.feed(response.text)
        result=[]
        for item in parser.links:
            url=urljoin(response.url,item['href'])
            hint=item['text']+' '+item['href']
            if not re.search(r'\.pdf(?:\?|$)|모집\s*공고|공고문|입주자\s*모집',hint,re.I):continue
            if not public_https(url):continue
            if urlparse(url).hostname != urlparse(response.url).hostname:continue
            result.append(url)
        # Avoid mistaking arbitrary project brochures for official notice PDFs.
        return list(dict.fromkeys(result))[:3]
    finally:response.close()

def extract_pdf(session,url):
    try:
        import fitz
    except ImportError:
        return None,'pymupdf_dependency_missing'
    response=fetch_response(session,url,stream=True)
    if response is None:return None,'download_failed'
    try:
        data=bytearray()
        for part in response.iter_content(chunk_size=65536):
            data.extend(part)
            if len(data)>MAX_PDF_BYTES:return None,'pdf_too_large'
        if not bytes(data[:5])==b'%PDF-':return None,'not_a_pdf'
        doc=fitz.open(stream=bytes(data),filetype='pdf')
        if doc.page_count>MAX_PAGES:return None,'pdf_too_long'
        pages=[(i+1,p.get_text('text',sort=True)) for i,p in enumerate(doc)]
        return pages,None
    except Exception:return None,'pdf_unreadable'
    finally:response.close()

def passage(text,pos,limit=540):
    start=max(0,pos-145);end=min(len(text),pos+390)
    # Capture neighboring lines as context but preserve original words verbatim.
    out=' '.join(text[start:end].split())
    return out[:limit]

def analyze_notice(session,project):
    evidence={'status':'no_source_pdf','sourceUrl':None,'excerpts':[],
              'eligibilityAutomaticallyConfirmed':False,
              'note':'공식 PDF의 자동 발췌문입니다. 추출은 법적 판정이 아닙니다.'}
    url=project.get('officialUrl')
    if not isinstance(url,str) or not public_https(url):
        evidence['reason']='official_pdf_link_missing';return evidence
    pdfs=find_pdf_urls(session,url,project.get('name',''))
    if not pdfs:
        evidence['reason']='official_pdf_not_discovered';return evidence
    for pdf_url in pdfs:
        pages,error=extract_pdf(session,pdf_url)
        if pages is None:
            evidence['reason']=error;continue
        full=' '.join(t for _,t in pages[:8])
        # Identity gate: never attribute a PDF to another named complex.
        name=re.sub(r'[^가-힣A-Za-z0-9]','',project.get('name',''))
        intro=re.sub(r'[^가-힣A-Za-z0-9]','',full)
        if len(name)>=4 and name not in intro:
            evidence['reason']='pdf_project_name_mismatch';continue
        excerpts=[]
        for category,patterns in CATEGORIES.items():
            matches=[]
            for page,text in pages:
                for pattern in patterns:
                    for m in re.finditer(pattern,text,re.I):
                        value=passage(text,m.start())
                        if len(value)<25:continue
                        if value in [x['text'] for x in matches]:continue
                        matches.append({'category':category,'page':page,'text':value,
                                        'reviewed':False,'extractionMethod':'automatic_pdf_text'})
                        if len(matches)>=4:break
                    if len(matches)>=4:break
                if len(matches)>=4:break
            excerpts.extend(matches)
        evidence.update({'status':'auto_extracted_unreviewed' if excerpts else 'pdf_read_no_matching_text',
                         'sourceUrl':pdf_url,'excerpts':excerpts,
                         'pdfPageCount':len(pages), 'reason':None if excerpts else 'no_relevant_text_found'})
        return evidence
    return evidence

def enrich_projects(session,projects,limit=15):
    count=0
    for p in projects[:max(0,limit)]:
        try:p['noticeEvidence']=analyze_notice(session,p)
        except Exception as exc:
            p['noticeEvidence']={'status':'no_source_pdf','sourceUrl':None,'excerpts':[],
                'reason':'unexpected_extraction_error','eligibilityAutomaticallyConfirmed':False}
        if p['noticeEvidence']['status']=='auto_extracted_unreviewed':count+=1
    return count
