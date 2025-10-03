# goxplorer.py — gofilelab/newest を巡回し、各記事詳細まで入って gofile.io/d/... を抽出
# 変更点:
# - 一覧ページは常に Playwright で取得（JS後のDOMを確実に読む）
# - Age Gate 自動突破（localStorage + ボタンクリック）
# - 記事リンク抽出を強化（entry-title/rel=bookmark/内部リンク）
# - 詳細ページも Playwright 優先で gofile を抽出
# - gofilelab の redirect/out を 1 回だけ解決
# - 死にリンク厳密除外
# - deadline_sec で全体に締め切り

import os
import re
import time
import random
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from typing import List, Set, Optional

import cloudscraper
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_ORIGIN = "https://gofilelab.com"
BASE_LIST_URL = BASE_ORIGIN + "/newest?page={page}"

GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_ORIGIN + "/newest",
    "Connection": "keep-alive",
}

# ========= 基本ユーティリティ =========
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

# ========= Age Gate & Playwright =========
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
    try:
        page.reload(wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
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

def _playwright_get_html(url: str, wait_ms: int = 900) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="ja-JP")
        context.set_default_timeout(9000)
        page = context.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": HEADERS["Referer"],
            "Connection": HEADERS["Connection"],
        })
        page.goto(url, wait_until="domcontentloaded", timeout=22000)
        page.wait_for_timeout(300)
        for _ in range(2):
            probe = page.content()
            if ("年齢" in probe and "確認" in probe) or ("I am over" in probe) or ("Agree" in probe):
                _bypass_age_gate(page); page.wait_for_timeout(280)
            else:
                break
        page.wait_for_timeout(wait_ms)
        html = page.content()
        context.close(); browser.close()
        return html

# ========= 中間リンク → gofile 解決 =========
def _resolve_to_gofile(url: str, scraper, timeout: int = 8) -> Optional[str]:
    if not url:
        return None
    url = fix_scheme(url)
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
    try:
        r = scraper.get(url, timeout=timeout, allow_redirects=False)
        loc = r.headers.get("Location") or r.headers.get("location")
        if isinstance(loc, str):
            m = GOFILE_RE.search(loc)
            if m:
                return fix_scheme(m.group(0))
    except Exception:
        pass
    m = GOFILE_RE.search(url)
    if m:
        return fix_scheme(m.group(0))
    return None

# ========= HTML 解析 =========
def _extract_article_links_from_list(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links: List[str] = []
    seen = set()

    # 1) 記事タイトルっぽい a（よくある構造）
    for sel in ["article a", ".entry-title a", "a[rel='bookmark']"]:
        for a in soup.select(sel):
            href = a.get("href")
            if not href:
                continue
            url = urljoin(BASE_ORIGIN, href.strip())
            if url not in seen:
                seen.add(url); links.append(url)

    # 2) それでも少ない場合、内部リンクを広めに収集（ナビ等は除外）
    if len(links) < 10:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#"):
                continue
            url = urljoin(BASE_ORIGIN, href)
            pr = urlparse(url)
            if pr.netloc and not pr.netloc.endswith("gofilelab.com"):
                continue
            bad = ("/newest", "/category/", "/tag/", "/page/", "/search", "/author", "/feed")
            if any(x in pr.path for x in bad):
                continue
            ext_bad = (".jpg", ".png", ".gif", ".webp", ".svg", ".css", ".js", ".zip", ".rar")
            if pr.path.endswith(ext_bad):
                continue
            if url not in seen:
                seen.add(url); links.append(url)

    # 過剰に多い場合は先頭 50 まで
    return links[:50]

def _extract_gofile_from_html(html: str, scraper) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls: List[str] = []
    seen = set()

    # aタグ（href + data-*）
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href:
            m = GOFILE_RE.search(href)
            go = fix_scheme(m.group(0)) if m else _resolve_to_gofile(href, scraper)
            if go and go not in seen:
                seen.add(go); urls.append(go)
        for attr in ("data-url", "data-clipboard-text", "data-href"):
            v = (a.get(attr) or "").strip()
            if not v:
                continue
            m2 = GOFILE_RE.search(v)
            if m2:
                go2 = fix_scheme(m2.group(0))
                if go2 and go2 not in seen:
                    seen.add(go2); urls.append(go2)

    # 生HTML保険
    for m in GOFILE_RE.findall(html or ""):
        u = fix_scheme(m.strip())
        if u and u not in seen:
            seen.add(u); urls.append(u)
    return urls

# ========= 死活判定 =========
def is_gofile_alive(url: str, timeout: int = 12) -> bool:
    url = fix_scheme(url)
    s = _build_scraper()
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True)
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

# ========= 一覧→詳細→抽出 =========
def fetch_listing_pages(num_pages: int = 100, deadline_ts: Optional[float] = None) -> List[str]:
    s = _build_scraper()
    results: List[str] = []
    seen_gofile: Set[str] = set()
    seen_posts: Set[str] = set()

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] deadline reached at page {p}; stop crawl.")
            break

        list_url = BASE_LIST_URL.format(page=p)
        # ★ 一覧は常に Playwright
        try:
            html = _playwright_get_html(list_url, wait_ms=900)
        except Exception as e:
            print(f"[warn] playwright list page {p} failed: {e}")
            html = ""

        article_urls = _extract_article_links_from_list(html) if html else []
        print(f"[info] page {p}: found {len(article_urls)} article links")

        added = 0
        for post_url in article_urls:
            if _deadline_passed(deadline_ts):
                break
            if post_url in seen_posts:
                continue
            seen_posts.add(post_url)

            # 詳細も Playwright 優先（まず速い方で…という段階は捨てて確実性重視）
            try:
                dhtml = _playwright_get_html(post_url, wait_ms=900)
            except Exception as e:
                print(f"[warn] playwright detail failed: {post_url} ({e})")
                dhtml = ""

            urls = _extract_gofile_from_html(dhtml, s) if dhtml else []
            print(f"[info] detail: {post_url} → gofiles {len(urls)}")

            for u in urls:
                if u not in seen_gofile:
                    results.append(u); seen_gofile.add(u); added += 1

            # 少しだけ間隔
            time.sleep(0.3)

        print(f"[info] page {p}: extracted {added} new urls (total {len(results)})")
        # 過負荷回避
        time.sleep(0.5)
    return results

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
