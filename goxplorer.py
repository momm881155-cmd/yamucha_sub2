# goxplorer.py — gofilelab/newest を巡回し、gofile.io/d/... を収集
# 方針:
# 1) まず WordPress REST API から posts を直取得（超速・安定）
# 2) 0件の時だけ Playwright で /newest → 記事詳細へ遷移して抽出
# 共通:
# - 年齢確認UI対応（☑「私は18以上…」→「同意して閲覧する」）＋ localStorage/cookie 併用
# - redirect/out の中間リンクを 1 回だけ解決
# - 死にリンクは厳密に除外
# - deadline_sec で全体に締め切り

import os
import re
import time
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from typing import List, Set, Optional

import cloudscraper
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_ORIGIN = "https://gofilelab.com"
BASE_LIST_URL = BASE_ORIGIN + "/newest?page={page}"
WP_POSTS_API  = BASE_ORIGIN + "/wp-json/wp/v2/posts?page={page}&per_page=20&_fields=link,content.rendered"

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

    # 年齢確認のcookieでバイパス（サーバ側参照されることがある）
    try:
        s.cookies.set("ageVerified", "1", domain="gofilelab.com", path="/")
        s.cookies.set("adult", "true", domain="gofilelab.com", path="/")
    except Exception:
        pass
    return s

def fix_scheme(url: str) -> str:
    if url.startswith("htps://"):
        return "https://" + url[len("htps://"):]
    return url

def _now() -> float:
    return time.monotonic()

def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts

# ========= 中間リンク → gofile 解決 =========
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

# ========= WP REST API で記事→gofile 抽出 =========
def _extract_gofile_from_html(html: str, scraper) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls: List[str] = []
    seen = set()

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

def _collect_via_wp_api(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    scraper = _build_scraper()
    all_urls: List[str] = []
    seen: Set[str] = set()

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] wp-api deadline at page {p}; stop.")
            break

        api = WP_POSTS_API.format(page=p)
        try:
            r = scraper.get(api, timeout=12)
            if r.status_code == 400 and "rest_post_invalid_page_number" in r.text:
                # ページ上限超え（以降は存在しない）
                break
            r.raise_for_status()
            arr = r.json()
        except Exception as e:
            print(f"[warn] wp-api page {p} failed: {e}")
            break

        if not isinstance(arr, list) or not arr:
            # データが空なら打ち切り
            break

        added = 0
        for item in arr:
            html = (item.get("content", {}) or {}).get("rendered", "") if isinstance(item, dict) else ""
            urls = _extract_gofile_from_html(html, scraper)
            for u in urls:
                if u not in seen:
                    seen.add(u); all_urls.append(u); added += 1

        print(f"[info] wp-api page {p}: gofiles {added} (total {len(all_urls)})")
        time.sleep(0.2)

    return all_urls

# ========= Playwright（AgeGate・一覧→詳細） =========
def _playwright_ctx(pw):
    browser = pw.chromium.launch(headless=True, args=[
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
    ])
    context = browser.new_context(user_agent=HEADERS["User-Agent"], locale="ja-JP")
    # bot検知回避
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP','ja'] });
    """)
    # Cookie でも age gate を事前回避
    try:
        context.add_cookies([
            {"name": "ageVerified", "value": "1", "domain": "gofilelab.com", "path": "/"},
            {"name": "adult", "value": "true", "domain": "gofilelab.com", "path": "/"},
        ])
    except Exception:
        pass
    context.set_default_timeout(9000)
    return context

def _bypass_age_gate(page) -> None:
    # localStorage で既読扱い
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

    # チェックボックス + 「同意して閲覧する」ボタン対応
    checkbox_selectors = [
        "input[type='checkbox']",
        "label:has-text('18') >> input[type='checkbox']",
        "label:has-text('成人') >> input[type='checkbox']",
        "label:has-text('同意') >> input[type='checkbox']",
    ]
    button_selectors = [
        "text=同意して閲覧する",
        "text=同意して入場",
        "text=同意して閲覧",
        "text=同意して入る",
        "text=同意して入室",
        "text=同意する",
        "button:has-text('同意')",
        "text=I Agree",
        "button:has-text('I Agree')",
        "text=Enter",
        "button:has-text('Enter')",
    ]

    # チェックボックスをオン
    try:
        cb = None
        for sel in checkbox_selectors:
            cb = page.query_selector(sel)
            if cb:
                box = cb.bounding_box()
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    cb.click(force=True)
                    page.wait_for_timeout(150)
                    break
                cb = None
    except Exception:
        pass

    # ボタンを押す
    try:
        btn = None
        for sel in button_selectors:
            btn = page.query_selector(sel)
            if btn:
                box = btn.bounding_box()
                if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                    btn.click(force=True)
                    page.wait_for_timeout(250)
                    break
                btn = None
    except Exception:
        pass

    # 念のため再読込
    try:
        page.reload(wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(250)
    except Exception:
        pass

def _get_html_pw(url: str, scroll_steps: int = 8, wait_ms: int = 700) -> str:
    with sync_playwright() as pw:
        context = _playwright_ctx(pw)
        page = context.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": HEADERS["Referer"],
            "Connection": HEADERS["Connection"],
        })
        page.goto(url, wait_until="domcontentloaded", timeout=22000)
        page.wait_for_timeout(300)
        _bypass_age_gate(page)
        # 遅延ロード対策で段階スクロール
        for _ in range(scroll_steps):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(wait_ms)
        html = page.content()
        context.close()
        return html

def _extract_article_links_from_list(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links: List[str] = []
    seen = set()

    # 典型的な記事リンク
    for sel in ["article a", ".entry-title a", "a[rel='bookmark']"]:
        for a in soup.select(sel):
            href = a.get("href")
            if not href:
                continue
            url = urljoin(BASE_ORIGIN, href.strip())
            if url not in seen:
                seen.add(url); links.append(url)

    # 不足時は内部リンクを広めに
    if len(links) < 8:
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

    # 過剰に多い場合は先頭だけ
    return links[:50]

def _collect_via_playwright(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    s = _build_scraper()
    all_urls: List[str] = []
    seen_urls: Set[str] = set()
    seen_posts: Set[str] = set()

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] pw deadline at list page {p}; stop.")
            break

        list_url = BASE_LIST_URL.format(page=p)
        try:
            lhtml = _get_html_pw(list_url, scroll_steps=8, wait_ms=700)
        except Exception as e:
            print(f"[warn] playwright list {p} failed: {e}")
            lhtml = ""

        article_urls = _extract_article_links_from_list(lhtml) if lhtml else []
        print(f"[info] page {p}: found {len(article_urls)} article links")

        added = 0
        for post_url in article_urls:
            if _deadline_passed(deadline_ts):
                break
            if post_url in seen_posts:
                continue
            seen_posts.add(post_url)

            try:
                dhtml = _get_html_pw(post_url, scroll_steps=4, wait_ms=650)
            except Exception as e:
                print(f"[warn] playwright detail failed: {post_url} ({e})")
                dhtml = ""

            urls = _extract_gofile_from_html(dhtml, s) if dhtml else []
            for u in urls:
                if u not in seen_urls:
                    seen_urls.add(u); all_urls.append(u); added += 1

            time.sleep(0.25)

        print(f"[info] page {p}: extracted {added} new urls (total {len(all_urls)})")
        time.sleep(0.4)
    return all_urls

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

# ========= メイン収集 =========
def fetch_listing_pages(num_pages: int = 100, deadline_ts: Optional[float] = None) -> List[str]:
    # 1) まず WP API で高速収集
    urls = _collect_via_wp_api(num_pages=num_pages, deadline_ts=deadline_ts)
    if urls:
        return urls
    # 2) ダメなら Playwright で一覧→詳細
    return _collect_via_playwright(num_pages=num_pages, deadline_ts=deadline_ts)

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
