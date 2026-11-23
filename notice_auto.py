"""Best-effort public 모집공고 PDF discovery & evidence extraction.

Only public HTTPS sources; no login/CAPTCHA bypass.  Extracted snippets are
unreviewed and must not be interpreted as legal eligibility decisions.
"""
from __future__ import annotations
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urldefrag, quote
import xml.etree.ElementTree as ET

import requests
from rule_engine import interpret

MAX_PDF_BYTES = 18 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PAGES = 180
MAX_CANDIDATES = 16
CATEGORIES = {
    'resale': (r'전매\s*제한', r'전매\s*행위', r'전매제한기간'),
    'occupancy': (r'거주\s*의무', r'실거주\s*의무', r'의무\s*거주'),
    'eligibility': (r'신청\s*자격', r'청약\s*자격', r'무주택\s*세대', r'거주\s*요건', r'재당첨\s*제한', r'청약\s*통장'),
    'payments': (r'계약금', r'중도금', r'잔금'),
}
DOC_HINT = re.compile(r'(?:\.pdf(?:[?#]|$)|입주자\s*모집|모집\s*공고|공고문|download|첨부파일|fileDown|fileId)', re.I)
BINARY_HINT = re.compile(r'(?:\.pdf(?:[?#]|$)|download|filedown|attach|fileId|첨부|공고문)', re.I)

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.active=[]
    def handle_starttag(self, tag, attrs):
        d=dict(attrs)
        for attr in ('href','src','data-url','data-href','data-src','data-file','data-download'):
            if d.get(attr):self.links.append((d[attr],d.get('title','')+' '+d.get('alt','')))
        if tag=='a':self.active.append(len(self.links)-1)
    def handle_data(self, data):
        if self.active and self.active[-1]>=0:
            i=self.active[-1]
            if i<len(self.links):
                url,hint=self.links[i];self.links[i]=(url,(hint+' '+data)[:250])
    def handle_endtag(self,tag):
        if tag=='a' and self.active:self.active.pop()


def public_https(url):
    if not isinstance(url,str) or len(url)>1800:return False
    parsed=urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:return False
    host=parsed.hostname.lower()
    if host=='localhost' or host.endswith(('.local','.internal')):return False
    try:
        ips=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        return bool(ips) and all(ipaddress.ip_address(x[4][0]).is_global for x in ips)
    except (OSError,ValueError):return False


def fetch_response(session,url,*,stream=False):
    """Bounded redirects, strict validation of each URL, no internal hosts."""
    for _ in range(4):
        if not public_https(url):return None
        try:
            response=session.get(url,timeout=(5,13),stream=stream,allow_redirects=False,
                headers={'User-Agent':'Mozilla/5.0 (compatible; CheongyakRadar/1.0; official PDF evidence)',
                         'Accept':'text/html,application/pdf,*/*'})
        except requests.RequestException:return None
        if response.status_code in (301,302,303,307,308):
            nxt=urljoin(url,response.headers.get('Location',''))
            response.close();url=nxt;continue
        if response.status_code!=200:
            response.close();return None
        return response
    return None


def plausible_link(base,raw):
    raw=html.unescape(str(raw or '')).replace('\\/','/').strip().strip('\"\'')
    if not raw or raw.startswith(('javascript:','data:','blob:','#','mailto:')):return None
    url=urldefrag(urljoin(base,raw))[0]
    return url if public_https(url) else None


def urls_in_page(base,markup):
    parser=LinkParser()
    try:parser.feed(markup)
    except Exception:pass
    found=[]
    for raw,label in parser.links:
        url=plausible_link(base,raw)
        if url and DOC_HINT.search(raw+' '+label):found.append((url,label))
    # Finds JS and JSON strings containing .pdf paths, including escaped JSON slashes.
    markup=html.unescape(markup).replace('\\/','/')
    for raw in re.findall(r'''["']([^"'<>\s]{3,700}\.pdf(?:\?[^"'<>\s]{0,200})?)["']''',markup,re.I):
        url=plausible_link(base,raw)
        if url:found.append((url,'embedded pdf'))
    return list(dict.fromkeys(found))


def extract_pdf(session,url):
    try:import fitz
    except ImportError:return None,'pymupdf_dependency_missing'
    response=fetch_response(session,url,stream=True)
    if response is None:return None,'download_failed'
    try:
        content_length=int(response.headers.get('Content-Length') or 0)
        if content_length>MAX_PDF_BYTES:return None,'pdf_too_large'
        data=bytearray()
        for part in response.iter_content(chunk_size=65536):
            data.extend(part)
            if len(data)>MAX_PDF_BYTES:return None,'pdf_too_large'
        if bytes(data[:5])!=b'%PDF-':return None,'not_a_pdf'
        with fitz.open(stream=bytes(data),filetype='pdf') as doc:
            if doc.page_count>MAX_PAGES:return None,'pdf_too_long'
            return [(i+1,page.get_text('text',sort=True)) for i,page in enumerate(doc)],None
    except Exception:return None,'pdf_unreadable'
    finally:response.close()


def normalize_name(value):
    return re.sub(r'[^가-힣a-zA-Z0-9]','',str(value or '')).lower()


def identity_matches(pages,project):
    text=''.join(t for _,t in pages[:min(12,len(pages))])
    norm=normalize_name(text)
    name=normalize_name(project.get('name'))
    # Do not attribute PDFs to other developments on a fuzzy partial match.
    if len(name)<4 or name not in norm:return False,'pdf_project_name_mismatch'
    # Where a notice date appears on the cover page, require exact date.
    announce=str(project.get('announce') or '')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',announce):
        date_variants={announce,announce.replace('-','.'),announce.replace('-','/'),
                       announce.replace('-',''),announce.replace('-','년',1).replace('-', '월',1)+'일'}
        date_compact=re.sub(r'\D','',announce)
        cover=re.sub(r'\s','',text[:16000])
        if re.search(r'모집공고일|입주자모집공고',cover):
            dates=re.findall(r'20\d{2}\s*[.년/-]\s*\d{1,2}\s*[.월/-]\s*\d{1,2}',cover)
            if dates and not any(re.sub(r'\D','',d)==date_compact for d in dates):
                return False,'pdf_notice_date_mismatch'
    return True,None


def passage(text,pos,limit=540):
    start=max(0,pos-170);end=min(len(text),pos+390)
    return ' '.join(text[start:end].split())[:limit]


def discover_sources(session,project):
    """Public notice URL + public website URL, limited to same-domain subpages.

    A bare applyhome home URL is not evidence of the notice's PDF.
    """
    urls=[]
    for key in ('officialUrl','officialNoticeUrl','developerUrl','projectHomepage'):
        url=project.get(key)
        if isinstance(url,str) and public_https(url) and url not in urls:urls.append(url)
    documents=[];visited=[];failure=[]
    for root in urls[:3]:
        root_host=urlparse(root).hostname
        queue=[(root,0)]
        while queue and len(visited)<9 and len(documents)<MAX_CANDIDATES:
            current,depth=queue.pop(0)
            if current in visited:continue
            visited.append(current)
            if current.lower().split('?')[0].endswith('.pdf'):
                documents.append(current);continue
            r=fetch_response(session,current,stream=True)
            if r is None:failure.append('source_unreachable');continue
            try:
                content_type=r.headers.get('Content-Type','').lower()
                if 'pdf' in content_type:
                    documents.append(current);continue
                if 'html' not in content_type and 'text/' not in content_type:
                    if BINARY_HINT.search(current):documents.append(current)
                    continue
                raw=bytearray()
                for chunk in r.iter_content(chunk_size=65536):
                    raw.extend(chunk)
                    if len(raw)>MAX_HTML_BYTES:break
                if len(raw)>MAX_HTML_BYTES:failure.append('html_too_large');continue
                try:r.encoding=r.apparent_encoding or r.encoding or 'utf-8'
                except Exception:pass
                markup=raw.decode(r.encoding or 'utf-8',errors='replace')
                for link,label in urls_in_page(r.url,markup):
                    host=urlparse(link).hostname
                    # Do not crawl arbitrary external sites from unrelated advertisements.
                    if host!=root_host:continue
                    hint=link+' '+label
                    if not DOC_HINT.search(hint):continue
                    if BINARY_HINT.search(hint) or re.search(r'모집|공고',hint):
                        if link not in documents:documents.append(link)
                    # Only follow 1 layer for a relevant HTML subpage.
                    if depth==0 and link not in visited and len(queue)<5:
                        queue.append((link,1))
            finally:r.close()
    return documents[:MAX_CANDIDATES],visited, failure




def search_indexed_pdfs(session,project):
    """Fallback: publicly indexed notice PDFs; these are UNVERIFIED sources.

    Public Bing RSS only. Fail closed when the service blocks automated access.
    No claim that a search-result host is an official issuer.
    """
    name=str(project.get('name') or '').strip()
    if len(normalize_name(name))<4:return []
    query='"'+name+'" "입주자모집공고" pdf'
    url='https://www.bing.com/search?format=rss&q='+quote(query)
    response=fetch_response(session,url,stream=True)
    if response is None:return []
    try:
        raw=bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            raw.extend(chunk)
            if len(raw)>400000:return []
        try:root=ET.fromstring(bytes(raw))
        except ET.ParseError:return []
        out=[]
        for item in root.findall('.//item')[:12]:
            link=(item.findtext('link') or '').strip()
            if public_https(link) and '.pdf' in urlparse(link).path.lower():
                out.append(link)
        return out[:5]
    finally:response.close()

def analyze_notice(session,project):
    evidence={'status':'no_source_pdf','sourceUrl':None,'excerpts':[],
              'eligibilityAutomaticallyConfirmed':False,
              'ruleEngine': interpret([],source_class='',source_url=None),
              'note':'공고문 출처와 명시적 문구 기반 사전 판정. 최종 법적 적격 판정은 아님.'}
    urls,visited,failures=discover_sources(session,project)
    source_class='linked_from_notice_or_issuer_page'
    if not urls:
        urls=search_indexed_pdfs(session,project)
        if urls:source_class='indexed_pdf_unverified_issuer'
    if not urls:
        evidence['reason']='official_pdf_not_discovered'
        evidence['discovery']={'pagesChecked':len(visited),'candidatesChecked':0,'failures':failures[:5]}
        return evidence
    reasons=[]
    for url in urls:
        pages,error=extract_pdf(session,url)
        if pages is None:
            reasons.append(error);continue
        matches,why=identity_matches(pages,project)
        if not matches:reasons.append(why);continue
        excerpts=[]
        for category,patterns in CATEGORIES.items():
            group=[]
            for page,text in pages:
                for pattern in patterns:
                    for match in re.finditer(pattern,text,re.I):
                        value=passage(text,match.start())
                        if len(value)<25 or any(value==x['text'] for x in group):continue
                        group.append({'category':category,'page':page,'text':value,
                             'reviewed':False,'extractionMethod':'automatic_pdf_text'})
                        if len(group)>=4:break
                    if len(group)>=4:break
                if len(group)>=4:break
            excerpts.extend(group)
        evidence.update({'status':'auto_extracted_unreviewed' if excerpts else 'pdf_read_no_matching_text',
             'sourceUrl':url,'excerpts':excerpts,'pdfPageCount':len(pages),
             'sourceClass':source_class,
             'ruleEngine':interpret(pages,source_class=source_class,source_url=url),
             'reason':None if excerpts else 'no_relevant_text_found',
             'discovery':{'pagesChecked':len(visited),'candidatesChecked':len(urls),'failures':failures[:5]}})
        return evidence
    # If notice/issuer links exist but yield no usable PDF, indexed documents
    # remain a last resort; their issuer is UNVERIFIED, so interpret() never decides.
    if source_class != 'indexed_pdf_unverified_issuer':
        indexed = search_indexed_pdfs(session,project)
        for url in indexed:
            if url in urls:continue
            pages,error=extract_pdf(session,url)
            if pages is None:
                reasons.append(error);continue
            matches,why=identity_matches(pages,project)
            if not matches:
                reasons.append(why);continue
            excerpts=[]
            for category,patterns in CATEGORIES.items():
                group=[]
                for page,text in pages:
                    for pattern in patterns:
                        match=re.search(pattern,text,re.I)
                        if match:
                            group.append({'category':category,'page':page,
                                          'text':passage(text,match.start()),
                                          'reviewed':False,'extractionMethod':'automatic_pdf_text'})
                            break
                    if group:break
                excerpts.extend(group)
            evidence.update({'status':'auto_extracted_unreviewed' if excerpts else 'pdf_read_no_matching_text',
                'sourceUrl':url,'sourceClass':'indexed_pdf_unverified_issuer',
                'excerpts':excerpts,'pdfPageCount':len(pages),
                'ruleEngine':interpret(pages,source_class='indexed_pdf_unverified_issuer',source_url=url),
                'reason':'unverified_issuer',
                'discovery':{'pagesChecked':len(visited),'candidatesChecked':len(urls)+len(indexed),'failures':reasons[:8]}})
            return evidence
    evidence['reason']=reasons[-1] if reasons else 'official_pdf_not_discovered'
    evidence['discovery']={'pagesChecked':len(visited),'candidatesChecked':len(urls),
                           'failures':list(dict.fromkeys(failures+reasons))[:8]}
    return evidence


def enrich_projects(session,projects,limit=15):
    succeeded=0
    for project in projects[:max(0,limit)]:
        try:project['noticeEvidence']=analyze_notice(session,project)
        except Exception as error:
            project['noticeEvidence']={'status':'no_source_pdf','sourceUrl':None,
                'excerpts':[],'reason':'unexpected_extraction_error','eligibilityAutomaticallyConfirmed':False,
                'ruleEngine':interpret([],source_class='',source_url=None)}
            print(f"PDF 탐색 예외: {project.get('id')} {type(error).__name__}")
        ev=project['noticeEvidence'];succeeded+=(ev['status']=='auto_extracted_unreviewed')
        print(f"PDF 탐색: {project.get('id')} {ev['status']} / {ev.get('reason')} "
              f"/ 근거 {len(ev['excerpts'])} / 진단 {ev.get('discovery',{})}")
    return succeeded
