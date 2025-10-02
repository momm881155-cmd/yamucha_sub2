# goxplorer.py — gofilelab/newest を最大100ページ巡回して Gofile リンクを収集
# ・Age Gate 突破（localStorage + ボタン）
# ・redirect/out 短縮リンクを1回だけ解決し gofile.io/d/... を抽出
# ・ダウンロード数は不使用、死にリンクは厳密排除
# ・cloudscraper → 0件なら Playwright フォールバック
# ・deadline_sec で全体に締め切り

import os
import re
import time
import random
from urllib.parse import urlparse, parse_qs, unquote
from typing import List, Set, Optional

import cloudscraper
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_LIST_URL = "https://gofilelab.com/newest?page={page}"
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://gofilelab.com/newest",
    "Connection": "keep-alive",
}

# ---- 共通 ----
def _build_scraper():
    proxies = {}
    http_p = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_p = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if http_p:
        proxies["http"] = http_p
    if https_p:
        proxies["https"] = https_p

    s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    if proxies:
        s.proxies.update(proxies)
    s.headers.update(HEADERS)
    return s

def fix_scheme(url: str) -> str:
    if url.startswith("htps://"):
        return "https://" + url[len("htps://"):]
    return url

def _now() -> float:
    return time.monotonic()

def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts

# ---- 中間リンク解決 ----
def _resolve_to_gofile(url: str, scraper, timeout: int = 8) -> Optional[str]:
    if not url:
        return None
    url = fix_scheme(url)

    # 1) /redirect?url=<encoded gofile>
    try:
        pr = urlparse(url)
        if pr.netloc.endswith("gofilelab.com"):
            qs = parse_qs(pr.query or "")
            for k in ("url", "u", "target"):
                if k in qs and qs[k]:
                    cand = unquote(qs[k][0])
                    m = GOFILE_RE.search(cand)
                    if m:
                        return fix_scheme(m.group(0))
    except Exception:
        pass

    # 2) /out/xxx → 302 Location を見る
    try:
        r = scraper.get(url, timeout=timeout, allow_redirects=False)
        loc = r.headers.get("Location") or r.headers.get("location")
        if isinstance(loc, str):
            m = GOFILE_RE.search(loc)
            if m:
                return fix_scheme(m.group(0))
    except Exception:
        pass

    # 3) もともと gofile
    m = GOFILE_RE.search(url)
    if m:
        return fix_scheme(m.group(0))
    return None

# ---- HTML抽出 ----
def _extract_urls_from_html(html: str, scraper) -> List[str]:
    urls: List[str] = []
    seen = set()
    soup = BeautifulSoup(html or "", "html.parser")

    # a タグすべて（href/各data属性）
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href:
            m = GOFILE_RE.search(href)
            go = fix_scheme(m.group(0)) if m else _resolve_to_gofile(href, scraper)
            if go and go not in seen:
                urls.append(go); seen.add(go)

        for attr in ("data-url", "data-clipboard-text", "data-href"):
            v = (a.get(attr) or "").strip()
            if not v:
                continue
            m2 = GOFILE_RE.search(v)
            if m2:
                go2 = fix_scheme(m2.group(0))
                if go2 and go2 not in seen:
                    urls.append(go2); seen.add(go2)

    # 生HTML（script含む）にも保険
    for m in GOFILE_RE.findall(html or ""):
        u = fix_scheme(m.strip())
        if u and u not in seen:
            urls.append(u); seen.add(u)
    return urls

# ---- ネット/Playwright/年齢確認 ----
def _get_with_retry(scraper, url: str, timeout: int = 10, max_retry: int = 3):
    for attempt in range(1, max_retry + 1):
        try:
            r = scraper.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} for {url}", response=r)
            return r
        except (requests.HTTPError, requests.RequestException):
            if attempt == max_retry:
                raise
            base = 0.7 * (2 ** (attempt - 1))
            time.sleep(base + random.uniform(0, base))

def _bypass_age_gate(page) -> None:
    js = """
    try {
      localStorage.setItem('ageVerified', '1');
      localStorage.setItem('adult', 'true');
      localStorage.setItem('age_verified', 'true');
      localStorage.setItem('age_verified_at', Date.now().toString());
    } catch (e) {}
    """
    page.evaluate(js)
    page.wait_for_timeout(160)
    page.reload(wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(200)

    sels = [
        "text=はい", "text=同意", "text=Enter", "text=I Agree", "text=Agree",
        "button:has-text('はい')", "button:has-text('同意')",
        "button:has-text('Enter')", "button:has-text('I Agree')",
        "[data-testid='age-accept']",
    ]
    for sel in sels:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click(); page.wait_for_timeout(220)
                break
        except PWTimeout:
            pass

def _fetch_page_with_playwright(url: str, wait_ms: int = 1000) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="ja-JP")
        context.set_default_timeout(8000)
        page = context.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": HEADERS["Referer"],
            "Connection": HEADERS["Connection"],
        })
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(300)

        for _ in range(2):
            probe = page.content()
            if ("年齢" in probe and "確認" in probe) or ("I am over" in probe) or ("Agree" in probe):
                _bypass_age_gate(page); page.wait_for_timeout(300)
            else:
                break

        page.wait_for_timeout(wait_ms)
        html = page.content()
        context.close(); browser.close()
        return html

# ---- 一覧巡回 ----
def fetch_listing_pages(num_pages: int = 100, deadline_ts: Optional[float] = None) -> List[str]:
    scraper = _build_scraper()
    results: List[str] = []
    seen: Set[str] = set()

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] deadline reached at page {p}; stop crawl.")
            break

        list_url = BASE_LIST_URL.format(page=p)
        urls: List[str] = []

        try:
            r = _get_with_retry(scraper, list_url, timeout=10, max_retry=3)
            urls = _extract_urls_from_html(r.text, scraper)
        except Exception as e:
            print(f"[warn] cloudscraper page {p} failed: {e}")

        if not urls and not _deadline_passed(deadline_ts):
            try:
                html = _fetch_page_with_playwright(list_url, wait_ms=1000)
                urls = _extract_urls_from_html(html, scraper)
            except Exception as e:
                print(f"[warn] playwright page {p} failed: {e}")

        added = 0
        for u in urls:
            if u not in seen:
                results.append(u); seen.add(u); added += 1

        print(f"[info] page {p}: extracted {added} new urls (total {len(results)})")
        time.sleep(0.8)
    return results

# ---- 死活判定 ----
def is_gofile_alive(url: str, timeout: int = 12) -> bool:
    url = fix_scheme(url)
    scraper = _build_scraper()
    try:
        r = _get_with_retry(scraper, url, timeout=timeout, max_retry=2)
        text = r.text or ""
        death = [
            "This content does not exist",
            "The content you are looking for could not be found",
            "has been automatically removed",
            "has been deleted by the owner",
        ]
        if any(m.lower() in text.lower() for m in death):
            return False
        if r.status_code >= 400:
            return False
        if len(text) < 500 and ("error" in text.lower() or "not found" in text.lower()):
            return False
        return True
    except Exception:
        return False

# ---- 収集メイン ----
def collect_fresh_gofile_urls(
    already_seen: Set[str], want: int = 20, num_pages: int = 100, deadline_sec: Optional[int] = None
) -> List[str]:
    deadline_ts = (_now() + deadline_sec) if deadline_sec else None
    urls = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    uniq: List[str] = []
    seen_now: Set[str] = set()
    for url in urls:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break
        if url in already_seen or url in seen_now:
            continue
        if not is_gofile_alive(url):
            continue
        uniq.append(url); seen_now.add(url)
        if len(uniq) >= want:
            break
    return uniq
