# goxplorer.py — gofilelab/newest を巡回し、各記事ページまで入って gofile リンクを抽出
# 仕様:
# ・一覧 (/newest?page=N) から記事URLを収集 → 各記事詳細を開いて gofile.io/d/... を抽出
# ・Age Gate を自動突破（localStorage + ボタン押下）
# ・gofilelab の redirect/out などの中間リンクを 1 回だけ解決して gofile に正規化
# ・死にリンクは厳密に除外
# ・cloudscraper（軽量）→ 0件やJS必要時は Playwright で再取得
# ・全体締め切り deadline_sec を bot から渡せる

import os
import re
import time
import random
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from typing import List, Set, Optional, Tuple

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

# ===== 共通ユーティリティ =====
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

# ===== 中間リンク解決（redirect/out → gofile） =====
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

    # 2) /out/xxx → 302 Location をみる（リダイレクトは追わずにヘッダだけ）
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

# ===== HTML から gofile 抽出（中間リンク対応） =====
def _extract_gofile_from_html(html: str, scraper) -> List[str]:
    urls: List[str] = []
    seen = set()
    soup = BeautifulSoup(html or "", "html.parser")

    # a タグの href / data-* を総なめ
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

    # 生HTML（script含む）の直書きも保険で拾う
    for m in GOFILE_RE.findall(html or ""):
        u = fix_scheme(m.strip())
        if u and u not in seen:
            urls.append(u); seen.add(u)
    return urls

# ===== HTML から “記事ページURL” を抽出 =====
def _extract_article_links_from_list(html: str) -> List[str]:
    """
    一覧ページから、各記事（詳細）への内部リンクを推定して抽出。
    - /newest?page=... などの自己参照や、/category/, /tag/ は除外
    - 単純に gofilelab.com ドメイン内の <a> を網羅し、パラメータ名やパスでスコアリング
    """
    soup = BeautifulSoup(html or "", "html.parser")
    links: List[str] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        # 絶対/相対を問わず絶対化
        url = urljoin(BASE_ORIGIN, href)
        pr = urlparse(url)

        # ドメイン外は除外
        if pr.netloc and not pr.netloc.endswith("gofilelab.com"):
            continue

        # 明らかな一覧/ナビは除外
        bad_substr = ("/newest", "/category/", "/tag/", "/page/", "/?page=", "/search", "/author")
        if any(x in pr.path for x in bad_substr):
            # ただし /newest?page= は一覧自身なのでスキップ
            if "/newest" in pr.path:
                continue
        # 拡張子でナビ/ファイルっぽいものを軽く除外
        if pr.path.endswith((".jpg", ".png", ".gif", ".webp", ".svg", ".css", ".js", ".zip", ".rar")):
            continue

        # 記事らしいURL長/構造に軽いスコア（だいたい /something/some-post/ のような形）
        # ここでは厳しく絞らず、後段で gofile が見つからなければ無視されるだけ
        key = url
        if key not in seen:
            seen.add(key)
            links.append(url)

    # 重いサイト対策：過剰に多い場合は先頭 50 件まで
    return links[:50]

# ===== ネット/Playwright/年齢確認 =====
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
    page.wait_for_timeout(150)
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

def _fetch_with_playwright(url: str, wait_ms: int = 900) -> str:
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
                _bypass_age_gate(page); page.wait_for_timeout(280)
            else:
                break

        page.wait_for_timeout(wait_ms)
        html = page.content()
        context.close(); browser.close()
        return html

# ===== 一覧巡回 → 記事詳細 → gofile抽出 =====
def _collect_from_detail(detail_url: str, scraper) -> List[str]:
    # 1) 軽量に cloudscraper → 0件なら Playwright
    try:
        r = _get_with_retry(scraper, detail_url, timeout=10, max_retry=2)
        urls = _extract_gofile_from_html(r.text, scraper)
        if urls:
            return urls
    except Exception as e:
        print(f"[warn] detail cloudscraper failed: {detail_url} ({e})")

    try:
        html = _fetch_with_playwright(detail_url, wait_ms=900)
        return _extract_gofile_from_html(html, scraper)
    except Exception as e:
        print(f"[warn] detail playwright failed: {detail_url} ({e})")
        return []

def fetch_listing_pages(num_pages: int = 100, deadline_ts: Optional[float] = None) -> List[str]:
    """
    一覧ページを巡回し、そこから記事リンクを収集→各記事で gofile を抽出し、一覧順で返す。
    """
    scraper = _build_scraper()
    results: List[str] = []
    seen_urls: Set[str] = set()   # gofile 重複排除
    seen_posts: Set[str] = set()  # 記事URL重複排除

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] deadline reached at page {p}; stop crawl.")
            break

        list_url = BASE_LIST_URL.format(page=p)
        article_urls: List[str] = []

        # 1) 一覧ページHTMLの取得
        html = ""
        try:
            r = _get_with_retry(scraper, list_url, timeout=10, max_retry=2)
            html = r.text
        except Exception as e:
            print(f"[warn] cloudscraper list page {p} failed: {e}")

        # 2) cloudscraper で記事URLが取れないなら Playwright
        if not html:
            try:
                html = _fetch_with_playwright(list_url, wait_ms=800)
            except Exception as e:
                print(f"[warn] playwright list page {p} failed: {e}")
                html = ""

        if html:
            article_urls = _extract_article_links_from_list(html)

        # 3) 各記事に入って gofile を抽出
        added = 0
        for post_url in article_urls:
            if _deadline_passed(deadline_ts):
                break
            if post_url in seen_posts:
                continue
            seen_posts.add(post_url)

            urls = _collect_from_detail(post_url, scraper)
            for u in urls:
                if u not in seen_urls:
                    results.append(u)
                    seen_urls.add(u)
                    added += 1

        print(f"[info] page {p}: extracted {added} new urls (total {len(results)})")
        time.sleep(0.6)  # サイト負荷軽減

    return results

# ===== 死活判定 =====
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

# ===== 収集メイン =====
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
