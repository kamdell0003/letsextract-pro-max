# streamlit_app_v4_4.py
"""
LetsExtract Pro Max v4.4 - Refined Unified Edition (full)
- English UI
- Per-domain isolated cache
- Parallel batch processing (multi-URL)
- Crawl depth default 3
- Email priority logic:
    1) emails matching exact domain (highest)
    2) emails found on contact/about/team pages (medium)
    3) other emails found anywhere (low)
- Combined export: single Excel (sheets per-domain + summary) and combined JSON
- Optional libraries: phonenumbers, email_validator, dnspython (graceful fallback)
- Fixes (v4.4):
    * Reduced global request timeout to 15s for responsiveness
    * Safe batch concurrency: batch Process threadpool limited to 3 to avoid nested thread explosion
    * Per-domain future timeout (600s) to avoid silent hangs
    * Progress bar for batch processing
    * FETCH_CACHE cleared before running a batch to avoid excessive memory usage
"""

import streamlit as st
import re
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import datetime, timedelta
import time
import random
import json
import io
import logging
import pandas as pd
from typing import List, Dict, Tuple, Optional, Set
from collections import defaultdict
import warnings

# ============================================================
# --- Intelligent Parser Wrapper (HTML or XML auto-detection) ---
# ============================================================
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

def make_soup(html: str):
    """
    Automatically detect whether the content is HTML or XML and use the correct parser.
    Keeps original logic intact while removing XMLParsedAsHTMLWarning.
    """
    if not html or not isinstance(html, str):
        return BeautifulSoup("", "html.parser")
    snippet = html.strip()[:200].lower()
    if snippet.startswith("<?xml") or snippet.startswith("<rss") or "<urlset" in snippet or "<feed" in snippet:
        # Use lxml-xml if available, otherwise fall back to built-in xml parser
        try:
            return BeautifulSoup(html, "lxml-xml")
        except Exception:
            return BeautifulSoup(html, "xml")
    else:
        return BeautifulSoup(html, "html.parser")

# ============================================================

# Optional libs
try:
    import phonenumbers
except Exception:
    phonenumbers = None

try:
    from email_validator import validate_email, EmailNotValidError
except Exception:
    validate_email = None
    EmailNotValidError = Exception

try:
    import dns.resolver
except Exception:
    dns = None

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LetsExtract-v4.4")

# ---------------- Config ----------------
class CFG:
    APP_NAME = "LetsExtract Pro Max v4.4 - Refined Unified Edition"
    MAX_THREADS = 20
    REQUEST_TIMEOUT = 15  # reduced from 30 to 15 for faster fallback
    DEFAULT_CRAWL_DEPTH = 3
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    COMMON_PATHS = [
        '/contact', '/about', '/about-us', '/team', '/support', '/sitemap', '/sitemap.xml'
    ]
    CONTEXT_KEYWORDS = ['contact', 'email', 'e-mail', 'info', 'support', 'sales', 'hr', 'jobs', 'careers', 'admin']

# ---------------- Utilities ----------------
def random_headers(referer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        'User-Agent': random.choice(CFG.USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
    }
    if referer:
        headers['Referer'] = referer
    return headers

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')

def normalize_email(email: str) -> str:
    e = email.strip().lower()
    e = e.rstrip('.,;:')
    if '@' in e:
        local, domain = e.split('@', 1)
        local = re.sub(r'\.+', '.', local)
        domain = domain.strip('.')
        e = f"{local}@{domain}"
    return e

def is_email_valid_syntax(email: str) -> bool:
    if validate_email:
        try:
            validate_email(email)
            return True
        except EmailNotValidError:
            return False
    else:
        return EMAIL_RE.fullmatch(email) is not None

def get_domain_from_url(url: str) -> str:
    return urlparse(url).netloc.lower()

def get_domain_from_email(email: str) -> str:
    return email.split('@')[-1].lower() if '@' in email else ''

def check_mx(domain: str) -> bool:
    if dns is None:
        return False
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        return len(answers) > 0
    except Exception:
        return False

def extract_emails_from_html(html: str) -> List[str]:
    found = EMAIL_RE.findall(html or "")
    normalized = [normalize_email(e) for e in found]
    seen = set(); out=[]
    for e in normalized:
        if e not in seen:
            seen.add(e); out.append(e)
    return out

def extract_phones_from_text(text: str) -> List[str]:
    phones = []
    if phonenumbers:
        for match in phonenumbers.PhoneNumberMatcher(text, None):
            try:
                num = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
                phones.append(num)
            except Exception:
                continue
    else:
        phones = re.findall(r'(\+?\d[\d\-\s]{6,}\d)', text or "")
        phones = [re.sub(r'[\s\-]', '', p) for p in phones]
    seen=set(); out=[]
    for p in phones:
        if p not in seen:
            seen.add(p); out.append(p)
    return out

def find_mailtos(soup: BeautifulSoup, base_url: str) -> List[str]:
    mails=[]
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.lower().startswith('mailto:'):
            m = href.split(':',1)[1].split('?')[0]
            mails.append(normalize_email(m))
    return list(dict.fromkeys(mails))

def context_score(soup: BeautifulSoup, email: str) -> float:
    default = 0.1
    try:
        nodes = soup.find_all(string=re.compile(re.escape(email), re.I))
        if not nodes:
            return default
        best = 0.0
        for node in nodes:
            parent_text = node.parent.get_text(separator=' ').lower() if node.parent else ''
            score = 0.0
            for kw in CFG.CONTEXT_KEYWORDS:
                if kw in parent_text:
                    score += 0.5
            if len(parent_text) < 300 and any(kw in parent_text for kw in CFG.CONTEXT_KEYWORDS):
                score += 0.3
            best = max(best, min(score, 1.0))
        return max(best, default)
    except Exception:
        return default

# ---------------- Per-domain isolated cache ----------------
# Structure: FETCH_CACHE[domain][url] = (html_text_or_None, status_code_or_None, timestamp)
FETCH_CACHE: Dict[str, Dict[str, Tuple[Optional[str], Optional[int], datetime]]] = {}
CACHE_TTL = timedelta(minutes=10)

def domain_cached_fetch(domain: str, url: str, timeout: int = CFG.REQUEST_TIMEOUT, referer: Optional[str] = None) -> Tuple[Optional[str], Optional[int]]:
    now = datetime.now()
    domain_store = FETCH_CACHE.setdefault(domain, {})
    entry = domain_store.get(url)
    if entry:
        html, status, ts = entry
        if now - ts < CACHE_TTL:
            logger.debug(f"cache hit {domain} {url}")
            return html, status
    try:
        resp = requests.get(url, headers=random_headers(referer), timeout=timeout, allow_redirects=True, verify=True)
        html = resp.text if resp.status_code == 200 else None
        domain_store[url] = (html, resp.status_code, now)
        return html, resp.status_code
    except requests.exceptions.RequestException as e:
        logger.warning(f"fetch error {url}: {e}")
        domain_store[url] = (None, None, now)
        return None, None

# ---------------- Page parsing ----------------
def parse_page_for_data(html: str, url: str) -> Dict:
    soup = make_soup(html)  # ✅ replaced
    text = soup.get_text(separator=' ')
    emails = extract_emails_from_html(html)
    mailtos = find_mailtos(soup, url)
    # include mailtos at front (they are explicit)
    combined_emails = list(dict.fromkeys(mailtos + emails))
    phones = extract_phones_from_text(text)
    social = {}
    for platform in ['twitter.com','facebook.com','linkedin.com','instagram.com','youtube.com','tiktok.com']:
        found = [a['href'] for a in soup.find_all('a', href=True) if platform in a['href']]
        if found:
            social[platform.split('.')[0]] = list(set(found))
    return {'emails': combined_emails, 'phones': phones, 'social': social, 'soup': soup}

# ---------------- Discovery & controlled crawl (per-domain) ----------------
def candidate_seed_pages(base_url: str) -> List[str]:
    parsed = urlparse(base_url)
    base_root = f"{parsed.scheme}://{parsed.netloc}"
    seeds = [base_url]
    for p in CFG.COMMON_PATHS:
        seeds.append(urljoin(base_root, p))
    # unique
    return list(dict.fromkeys(seeds))

def same_domain(url1: str, url2: str) -> bool:
    try:
        return urlparse(url1).netloc.lower() == urlparse(url2).netloc.lower()
    except Exception:
        return False

def discover_for_domain(root_url: str, crawl_depth: int = CFG.DEFAULT_CRAWL_DEPTH, max_workers: int = 6, allow_external_emails: bool = True) -> Dict:
    """
    Discover pages for a single domain, isolated cache, controlled crawling.
    Returns results for this domain.
    """
    parsed_root = urlparse(root_url)
    if parsed_root.scheme not in ('http','https'):
        root_url = 'https://' + root_url
        parsed_root = urlparse(root_url)
    domain = parsed_root.netloc.lower()
    results = {
        'domain': domain,
        'root_url': root_url,
        'found_pages': [],  # tuples (url, emails_count, phones_count)
        'emails_raw': [],   # raw list before prioritization
        'phones_raw': [],
        'social': {},
        'stats': {'checked':0,'success':0,'failed':0}
    }

    seeds = candidate_seed_pages(root_url)
    seeds = [s for s in seeds if s]  # sanity

    # Stage 1: fetch seeds in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(domain_cached_fetch, domain, url, CFG.REQUEST_TIMEOUT, root_url): url for url in seeds}
        for fut in as_completed(futures):
            url = futures[fut]
            results['stats']['checked'] += 1
            try:
                html, status = fut.result()
            except Exception:
                html, status = None, None
            if html and status == 200:
                results['stats']['success'] += 1
                parsed = parse_page_for_data(html, url)
                results['found_pages'].append((url, len(parsed['emails']), len(parsed['phones'])))
                results['emails_raw'].extend(parsed['emails'])
                results['phones_raw'].extend(parsed['phones'])
                for k,v in parsed['social'].items():
                    results['social'].setdefault(k, []).extend(v)
            else:
                results['stats']['failed'] += 1

    # Controlled BFS-like crawl limited by crawl_depth (only same domain)
    if crawl_depth and crawl_depth > 0:
        # build initial frontier from fetched seeds
        frontier = set()
        for page_url, _, _ in results['found_pages']:
            page_html, _, _ = FETCH_CACHE.get(domain, {}).get(page_url, (None, None, None))
            if page_html:
                try:
                    soup = make_soup(page_html)
                    for a in soup.find_all('a', href=True):
                        href = a['href'].strip()
                        if href.lower().startswith('mailto:'):
                            continue
                        full = urljoin(page_url, href)
                        if same_domain(root_url, full):
                            frontier.add(full.split('#')[0])
                except Exception:
                    pass
        frontier = list(dict.fromkeys(frontier))[:800]

        depth_left = crawl_depth
        visited_local = set([p for p,_,_ in results['found_pages']])
        while depth_left > 0 and frontier:
            to_crawl = [u for u in frontier if u not in visited_local][:800]
            if not to_crawl:
                break
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {ex.submit(domain_cached_fetch, domain, url, CFG.REQUEST_TIMEOUT, root_url): url for url in to_crawl}
                for fut in as_completed(futures):
                    url = futures[fut]
                    results['stats']['checked'] += 1
                    try:
                        html, status = fut.result()
                    except Exception:
                        html, status = None, None
                    if html and status == 200:
                        visited_local.add(url)
                        results['stats']['success'] += 1
                        parsed = parse_page_for_data(html, url)
                        results['found_pages'].append((url, len(parsed['emails']), len(parsed['phones'])))
                        results['emails_raw'].extend(parsed['emails'])
                        results['phones_raw'].extend(parsed['phones'])
                        for k,v in parsed['social'].items():
                            results['social'].setdefault(k, []).extend(v)
                    else:
                        results['stats']['failed'] += 1
            # prepare next frontier from recently crawled pages
            new_frontier = set()
            for url in list(visited_local)[-len(to_crawl):]:
                page_html, _, _ = FETCH_CACHE.get(domain, {}).get(url, (None, None, None))
                if page_html:
                    try:
                        soup = make_soup(page_html)
                        for a in soup.find_all('a', href=True):
                            href = a['href'].strip()
                            if href.lower().startswith('mailto:'):
                                continue
                            full = urljoin(url, href)
                            if same_domain(root_url, full) and full not in visited_local:
                                new_frontier.add(full.split('#')[0])
                    except Exception:
                        pass
            frontier = list(dict.fromkeys(list(new_frontier)))[:800]
            depth_left -= 1

    # Deduplicate emails & phones preserving order
    dedup_emails = []
    seen = set()
    for e in results['emails_raw']:
        ne = normalize_email(e)
        if ne not in seen:
            seen.add(ne); dedup_emails.append(ne)
    results['emails_raw'] = dedup_emails

    phones_seen=set(); phones_out=[]
    for p in results['phones_raw']:
        if p not in phones_seen:
            phones_seen.add(p); phones_out.append(p)
    results['phones_raw'] = phones_out

    # Prioritization logic:
    # 1) emails whose domain == root domain
    # 2) emails present on contact/about/team pages
    # 3) other emails
    root_domain = domain
    contact_like_paths = set([p for p in CFG.COMMON_PATHS if 'contact' in p or 'about' in p or 'team' in p or 'support' in p or 'help' in p or 'اتصل' in p or 'تواصل' in p])

    # Collect mapping email -> pages where found & context score & mailto flag
    email_info = {}
    # scan cached pages for this domain to find occurrences and compute context
    domain_store = FETCH_CACHE.get(domain, {})
    for url, (html, status, ts) in (domain_store.items() if domain_store else []):
        if not html:
            continue
        parsed = make_soup(html)
        page_emails = extract_emails_from_html(html)
        mailtos = find_mailtos(parsed, url)
        page_emails = list(dict.fromkeys(mailtos + page_emails))
        for em in page_emails:
            nem = normalize_email(em)
            info = email_info.setdefault(nem, {'pages': set(), 'mailto': False, 'context_scores': []})
            info['pages'].add(url)
            if nem in mailtos:
                info['mailto'] = True
            try:
                info['context_scores'].append(context_score(parsed, nem))
            except Exception:
                info['context_scores'].append(0.1)

    # Now create priority buckets
    bucket_domain = []
    bucket_contact_pages = []
    bucket_other = []
    for em, info in email_info.items():
        em_domain = get_domain_from_email(em)
        avg_context = sum(info['context_scores'])/len(info['context_scores']) if info['context_scores'] else 0.1
        is_on_contact = any(any(cp in url.lower() for cp in contact_like_paths) for url in info['pages'])
        rec = {'email': em, 'pages': list(info['pages']), 'mailto': info['mailto'], 'context': round(avg_context,3)}
        if em_domain == root_domain:
            bucket_domain.append(rec)
        elif is_on_contact:
            bucket_contact_pages.append(rec)
        else:
            bucket_other.append(rec)

    # final ordered list
    prioritized = bucket_domain + bucket_contact_pages + bucket_other

    # Scoring each email (syntax, mx if available, context) and compute overall score (0-100)
    email_scores = []
    for item in prioritized:
        em = item['email']
        syntax_ok = is_email_valid_syntax(em)
        mx_ok = False
        if dns:
            try:
                mx_ok = check_mx(get_domain_from_email(em))
            except Exception:
                mx_ok = False
        ctx = item.get('context', 0.1)
        domain_score = 0.5 if ('.' in get_domain_from_email(em) and len(get_domain_from_email(em).split('.')[0])>1) else 0.2
        weights = {'syntax':0.4, 'mx':0.25, 'domain':0.15, 'context':0.2}
        raw = (1.0 if syntax_ok else 0.0)*weights['syntax'] + (1.0 if mx_ok else 0.0)*weights['mx'] + domain_score*weights['domain'] + ctx*weights['context']
        overall = min(max(raw, 0.0),1.0)
        email_scores.append({
            'email': em,
            'syntax_ok': bool(syntax_ok),
            'mx_ok': bool(mx_ok),
            'context': ctx,
            'pages': item['pages'],
            'overall': round(overall*100,2)
        })

    results['email_scores'] = email_scores
    results['prioritized_emails'] = [e['email'] for e in email_scores]
    results['quality_summary'] = {'avg_email_score': round(sum(e['overall'] for e in email_scores)/len(email_scores),2) if email_scores else 0.0}
    return results

# ---------------- Batch processing (multiple domains) ----------------
def batch_process_urls(urls: List[str], crawl_depth: int = CFG.DEFAULT_CRAWL_DEPTH, max_workers: int = 6) -> Dict:
    """
    Run discover_for_domain for each provided URL in parallel.
    Returns dict with per-domain results and combined summary.

    v4.4 adjustments:
    - Limit batch-level threadpool to a safe small number to avoid nested thread explosion.
    - Per-domain future timeout to ensure no silent hangs.
    - Provide progress reporting (expects being called from Streamlit UI).
    """
    # normalize urls
    normalized = []
    for u in urls:
        u = u.strip()
        if not u:
            continue
        if not u.startswith(('http://','https://')):
            u = 'https://' + u
        normalized.append(u)
    domains_to_url = {get_domain_from_url(u): u for u in normalized}

    results_by_domain = {}
    # run parallel domain jobs
    # Safe upper bound for batch-level concurrency to avoid nested ThreadPool blowup
    batch_pool_size = min(3, len(normalized) or 1)

    with ThreadPoolExecutor(max_workers=batch_pool_size) as ex:
        futures = {ex.submit(discover_for_domain, url, crawl_depth, max_workers): url for url in normalized}

        # Setup progress if running under Streamlit
        total = len(futures)
        done = 0
        try:
            progress = st.progress(0)
            use_progress = True
        except Exception:
            use_progress = False
            progress = None

        for fut in as_completed(futures):
            done += 1
            if use_progress and progress:
                try:
                    progress.progress(done / total)
                except Exception:
                    pass
            url = futures[fut]
            try:
                # safety timeout per-domain to avoid indefinite blocking
                res = fut.result(timeout=600)  # 10 minutes per domain
            except TimeoutError:
                logger.error(f"Timeout processing {url}")
                res = {'domain': get_domain_from_url(url), 'root_url': url, 'found_pages': [], 'emails_raw': [], 'phones_raw': [], 'social': {}, 'stats':{}, 'email_scores': [], 'prioritized_emails': [], 'quality_summary':{}}
            except Exception as e:
                logger.error(f"Error processing {url}: {e}")
                res = {'domain': get_domain_from_url(url), 'root_url': url, 'found_pages': [], 'emails_raw': [], 'phones_raw': [], 'social': {}, 'stats':{}, 'email_scores': [], 'prioritized_emails': [], 'quality_summary':{}}
            results_by_domain[res['domain']] = res

    # Combined summary
    combined = {'generated_at': datetime.now().isoformat(), 'domains': {}, 'totals': {}}
    total_emails = 0
    total_domains = len(results_by_domain)
    for d, r in results_by_domain.items():
        combined['domains'][d] = {
            'root_url': r['root_url'],
            'emails_count': len(r.get('prioritized_emails', [])),
            'phones_count': len(r.get('phones_raw', [])),
            'avg_email_score': r.get('quality_summary', {}).get('avg_email_score', 0),
            'found_pages': r.get('found_pages', [])
        }
        total_emails += len(r.get('prioritized_emails', []))
    combined['totals'] = {'domains': total_domains, 'emails': total_emails}
    return {'per_domain': results_by_domain, 'combined': combined}

# ---------------- Export combined Excel/JSON ----------------
def generate_combined_json(results_batch: Dict) -> str:
    """
    Convert the batch results to JSON (for download button in Streamlit)
    """
    return json.dumps(results_batch, indent=2, ensure_ascii=False)

# ---------------- Export combined Excel/JSON ----------------
def generate_combined_excel(results_batch: Dict) -> bytes:
    """
    Modified version:
    Create ONE Excel sheet ('All Results') with all domains merged in a single table.
    Each row = one email entry (with domain, URL, score, etc.)
    """
    output = io.BytesIO()

    # Prepare combined rows
    rows = []
    for domain, domain_res in results_batch['per_domain'].items():
        es = domain_res.get('email_scores', [])
        if es:
            for e in es:
                rows.append({
                    'domain': domain,
                    'root_url': domain_res.get('root_url', ''),
                    'email': e.get('email', ''),
                    'syntax_ok': e.get('syntax_ok', False),
                    'mx_ok': e.get('mx_ok', False),
                    'context': e.get('context', 0.0),
                    'overall_score': e.get('overall', 0.0),
                    'pages_found': ", ".join(e.get('pages', []))[:2000],  # trimmed
                })
        else:
            # domain with no emails
            rows.append({
                'domain': domain,
                'root_url': domain_res.get('root_url', ''),
                'email': '',
                'syntax_ok': False,
                'mx_ok': False,
                'context': 0.0,
                'overall_score': 0.0,
                'pages_found': ''
            })

    df_all = pd.DataFrame(rows)
    if df_all.empty:
        df_all = pd.DataFrame([{
            'domain': '',
            'root_url': '',
            'email': '',
            'syntax_ok': False,
            'mx_ok': False,
            'context': 0.0,
            'overall_score': 0.0,
            'pages_found': ''
        }])

    # Write to Excel (single sheet)
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_all.to_excel(writer, sheet_name='All_Results', index=False)

    return output.getvalue()

# ---------------- Streamlit UI ----------------
st.set_page_config(page_title=CFG.APP_NAME, layout="wide")
st.title("" + CFG.APP_NAME)
st.markdown("**v4.4 — Refined Unified Edition (English UI)**")

# Sidebar controls
with st.sidebar:
    st.header("Settings")
    crawl_mode = st.selectbox("Crawl mode", ["A: Known pages only (fast)", "B: Crawl up to depth N (deeper)"], index=1)
    crawl_depth = st.slider("If B: Crawl depth", 0, 5, CFG.DEFAULT_CRAWL_DEPTH)
    max_threads = st.slider("Max parallel threads (per domain)", 1, CFG.MAX_THREADS, min(6, CFG.MAX_THREADS))
    batch_workers = st.slider("Parallel domains (batch)", 1, CFG.MAX_THREADS, min(6, CFG.MAX_THREADS))
    enable_mx = st.checkbox("Enable MX checks (dnspython)", value=(dns is not None))
    enable_phonenumbers = st.checkbox("Enable phonenumbers parsing", value=(phonenumbers is not None))
    enable_email_validator = st.checkbox("Enable email-validator (syntax)", value=(validate_email is not None))
    restrict_same_domain = st.checkbox("Prefer same-domain emails (prioritize)", value=True)
    cache_minutes = st.number_input("Cache TTL (minutes)", min_value=1, max_value=120, value=10)
    CACHE_TTL = timedelta(minutes=int(cache_minutes))
    st.markdown("---")
    st.write("Notes:")
    st.write("- Mode A: fast (seed pages only). Mode B: deeper crawl up to depth N.")
    st.write("- Avoid scanning many sites in parallel without permission.")

# Session state initialization for history
if 'extraction_history' not in st.session_state:
    st.session_state.extraction_history = []

# Tabs (keeps similar layout as your original)
tabs = st.tabs(["Home","Text Extraction","Smart URL Search","Batch Processing","File Upload","Verification","History & Reports"])

# Home
with tabs[0]:
    st.header("Welcome — LetsExtract Pro Max v4.4")
    st.subheader("Core features")
    st.markdown("""
    - Per-domain isolated cache (no cross-domain contamination)
    - Prioritized email selection (domain > contact pages > others)
    - Parallel batch processing (combined report)
    - BeautifulSoup parsing, phonenumbers, email-validator, DNS MX checks (optional)
    """)
    total_ops = len(st.session_state.extraction_history)
    total_emails = sum(len(i.get('data', {}).get('prioritized_emails', [])) for i in st.session_state.extraction_history)
    st.metric("Total Operations", total_ops)
    st.metric("Total Emails Extracted", total_emails)

# Text Extraction tab
with tabs[1]:
    st.header("Text Extraction")
    text = st.text_area("Paste text or HTML:", height=300)
    ex_type = st.radio("Extract:", ["Emails","Phones","URLs","All"], horizontal=True)
    if st.button("Extract from text"):
        if not text.strip():
            st.warning("Please paste some text or HTML.")
        else:
            out = {}
            if ex_type in ["Emails","All"]:
                out['emails'] = extract_emails_from_html(text)
            if ex_type in ["Phones","All"]:
                out['phones'] = extract_phones_from_text(text)
            if ex_type in ["URLs","All"]:
                out['urls'] = re.findall(r'http[s]?://\S+', text)
            st.success("Extraction done")
            st.json(out)
            st.session_state.extraction_history.append({'timestamp':datetime.now(),'type':'text','count':len(out.get('emails',[]))+len(out.get('phones',[])),'data':out,'success':True})

# Smart URL Search (single)
with tabs[2]:
    st.header("Smart URL Search (single domain)")
    url_input = st.text_input("Enter website URL (or domain):", placeholder="https://example.com")
    if st.button("Start Smart Search (single)"):
        if not url_input.strip():
            st.warning("Please enter a URL or domain.")
        else:
            if not url_input.startswith(('http://','https://')):
                url_input = 'https://' + url_input
            effective_depth = 0 if crawl_mode.startswith('A') else int(crawl_depth)
            st.info("Running discovery (this may take time for deeper depth)...")
            start = datetime.now()
            result = discover_for_domain(url_input, crawl_depth=effective_depth, max_workers=max_threads)
            elapsed = (datetime.now()-start).total_seconds()
            st.success(f"Done in {elapsed:.1f}s — Pages checked: {result['stats'].get('checked',0)} — Emails: {len(result.get('prioritized_emails',[]))}")
            # show prioritized emails table
            es = result.get('email_scores', [])
            if es:
                df = pd.DataFrame(es).sort_values(by='overall', ascending=False)
                st.dataframe(df, use_container_width=True, height=300)
                st.download_button("Download domain JSON", data=json.dumps(result, indent=2, ensure_ascii=False), file_name=f"{result['domain']}_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", mime="application/json")
            else:
                st.info("No emails found.")
            st.session_state.extraction_history.append({'timestamp':datetime.now(),'type':'single','count':len(result.get('prioritized_emails',[])),'data':result,'success':True})

# Batch Processing (multiple URLs) - combined report
with tabs[3]:
    st.header("Batch Processing (multiple domains) — Combined Report")
    urls_text = st.text_area("Enter website URLs (one per line):", height=200, placeholder="https://site1.com\nhttps://site2.com")
    if st.button("Start Batch (combined)"):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        if not urls:
            st.warning("Please enter one or more URLs.")
        else:
            effective_depth = 0 if crawl_mode.startswith('A') else int(crawl_depth)
            st.info(f"Launching batch for {len(urls)} domains — depth={effective_depth}")
            start = datetime.now()
            # clear per-domain caches optionally? we keep caches (TTL controls staleness)
            # v4.4: clear cache before batch to avoid memory blowup from large stale caches
            FETCH_CACHE.clear()
            batch_res = batch_process_urls(urls, crawl_depth=effective_depth, max_workers=batch_workers)
            elapsed = (datetime.now()-start).total_seconds()
            st.success(f"Batch finished in {elapsed:.1f}s — Domains processed: {len(batch_res['per_domain'])}")
            # show summary
            summary = []
            for d, info in batch_res['per_domain'].items():
                summary.append({'domain': d, 'root_url': info.get('root_url',''), 'emails': len(info.get('prioritized_emails',[])), 'phones': len(info.get('phones_raw',[])), 'avg_score': info.get('quality_summary',{}).get('avg_email_score',0)})
            df_sum = pd.DataFrame(summary)
            st.dataframe(df_sum, use_container_width=True)
            # Combined exports
            st.markdown("### Download combined report")
            json_text = generate_combined_json(batch_res)
            excel_bytes = generate_combined_excel(batch_res)
            st.download_button("Download combined JSON", data=json_text, file_name=f"combined_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", mime="application/json")
            st.download_button("Download combined Excel", data=excel_bytes, file_name=f"combined_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            # Save to history
            st.session_state.extraction_history.append({'timestamp':datetime.now(),'type':'batch','count':sum(len(r.get('prioritized_emails',[])) for r in batch_res['per_domain'].values()),'data':batch_res,'success':True})

# File Upload
with tabs[4]:
    st.header("File Upload")
    uploaded = st.file_uploader("Upload TXT or CSV", type=['txt','csv'])
    if uploaded and st.button("Extract from file"):
        try:
            if uploaded.type == 'text/plain':
                text = uploaded.read().decode('utf-8')
            else:
                df = pd.read_csv(uploaded)
                text = df.to_string()
            emails = extract_emails_from_html(text)
            phones = extract_phones_from_text(text)
            st.success(f"Found {len(emails)} emails and {len(phones)} phones")
            if emails:
                st.dataframe(pd.DataFrame({'email':emails}), use_container_width=True)
            st.session_state.extraction_history.append({'timestamp':datetime.now(),'type':'file','count':len(emails)+len(phones),'data':{'emails':emails,'phones':phones},'success':True})
        except Exception as e:
            st.error(f"File processing error: {e}")

# Verification tab
with tabs[5]:
    st.header("Verification")
    verify_input = st.text_area("Enter emails or phones (one per line):", height=200)
    verify_mode = st.radio("Verify:", ["Email","Phone","All"], horizontal=True)
    if st.button("Run verification"):
        items = [i.strip() for i in verify_input.splitlines() if i.strip()]
        if not items:
            st.warning("Add items to verify.")
        else:
            rows=[]
            for it in items:
                if '@' in it and verify_mode in ("Email","All"):
                    syntax = is_email_valid_syntax(it)
                    mx = False
                    if dns:
                        try:
                            mx = check_mx(get_domain_from_email(it))
                        except Exception:
                            mx = False
                    rows.append({'item':it,'type':'email','syntax_ok':syntax,'mx_ok':mx})
                elif verify_mode in ("Phone","All"):
                    if phonenumbers:
                        try:
                            num = phonenumbers.parse(it, None)
                            ok = phonenumbers.is_valid_number(num)
                            fmt = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
                        except Exception:
                            ok=False; fmt=''
                        rows.append({'item':it,'type':'phone','valid':ok,'formatted':fmt})
                    else:
                        ok = bool(re.match(r'^\+?\d{7,15}$', re.sub(r'[\s\-]','',it)))
                        rows.append({'item':it,'type':'phone','valid':ok,'formatted':it})
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True)

# History & Reports
with tabs[6]:
    st.header("History & Reports")
    if not st.session_state.extraction_history:
        st.info("No operations recorded yet.")
    else:
        hist_rows=[]
        for h in st.session_state.extraction_history:
            hist_rows.append({'time': h['timestamp'].strftime("%Y-%m-%d %H:%M:%S"), 'type': h['type'], 'count': h['count']})
        st.dataframe(pd.DataFrame(hist_rows), use_container_width=True)
        if st.button("Download full history (JSON)"):
            hist_json = json.dumps([{'timestamp':h['timestamp'].isoformat(),'type':h['type'],'count':h['count']} for h in st.session_state.extraction_history], indent=2, ensure_ascii=False)
            st.download_button("Download", data=hist_json, file_name=f"history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", mime="application/json")

st.markdown("---")
st.markdown(f"<small>{CFG.APP_NAME} — v4.4 — {datetime.now().strftime('%Y-%m-%d')}</small>", unsafe_allow_html=True)
