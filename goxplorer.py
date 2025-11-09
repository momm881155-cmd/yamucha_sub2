# goxplorer.py — 汎用スクレイパ（gofilelab年齢ゲート対応 / 早期打ち切り + 超軽量死活判定）
# 目的:
# - まず RAW_LIMIT 件だけ素早く収集（環境変数で調整可能、デフォルト100）
# - 先頭 FILTER_LIMIT 件だけ超軽量フィルタ → 既出除外＆死活OKから want 本揃った時点で即返す
# - 死活判定は 1 回だけ ≤0.5s GET、先頭 1.5KB に死亡確定文言があれば False。
#   タイムアウト/403/503 などは “死と断定不可” → True（=投稿候補OK）
#
# 環境変数（任意）:
#   BASE_ORIGIN         対象サイトのオリジン（例: https://gofilelab.com）
#   BASE_LIST_URL       リストページURLテンプレ（例: https://gofilelab.com/newest?page={page}）
#   PAGE1_URL           1ページ目のURL（例: https://gofilelab.com/newest?page=1）
#   RAW_LIMIT=100       収集時の上限
#   FILTER_LIMIT=50     フィルタに回す最大件数
#   SCRAPE_TIMEOUT_SEC  収集＋フィルタの締切秒（botから未指定時に参照）
#
# 依存は requirements.txt のまま。

import os
import re
import time
from html import unescape
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from typing import List, Set, Optional

import cloudscraper
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ====== 環境デフォルト ======
ENV_BASE_ORIGIN   = os.getenv("BASE_ORIGIN", "https://gofilelab.com").rstrip("/")
ENV_BASE_LIST_URL = os.getenv("BASE_LIST_URL", ENV_BASE_ORIGIN + "/newest?page={page}")
ENV_PAGE1_URL     = os.getenv("PAGE1_URL", ENV_BASE_ORIGIN + "/newest?page=1")

BASE_ORIGIN   = ENV_BASE_ORIGIN
BASE_LIST_URL = ENV_BASE_LIST_URL
PAGE1_URL     = ENV_PAGE1_URL

# サイト固有Fastルート（gofilelabのみ活用）
WP_POSTS_API  = BASE_ORIGIN + "/wp-json/wp/v2/posts?page={page}&per_page=20&_fields=link,content.rendered"
SITEMAP_INDEX = BASE_ORIGIN + "/sitemap_index.xml"

GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)
_LOC_RE   = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE | re.DOTALL)

HEADERS = {
    "User-Agent": (
        # 少し新しめのUAでCF対策
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_ORIGIN,
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))

def _build_scraper():
    proxies = {}
    http_p = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_p = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if http_p:  proxies["http"]  = http_p
    if https_p: proxies["https"] = https_p

    s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    if proxies: s.proxies.update(proxies)
    s.headers.update(HEADERS)

    # gofilelab系の年齢Cookie（他サイトでも無害）
    try:
        host = urlparse(BASE_ORIGIN).hostname or ""
        root_dom = "." + ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host
        for dom in set([host, "."+host if not host.startswith(".") else host, root_dom]):
            if dom:
                s.cookies.set("ageVerified", "1", domain=dom, path="/")
                s.cookies.set("adult", "true",   domain=dom, path="/")
    except Exception:
        pass
    return s

def fix_scheme(url: str) -> str:
    return ("https://" + url[len("htps://"):]) if url.startswith("htps://") else url

def _now() -> float: return time.monotonic()
def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts

# ====== 死活判定（超軽量） ======
_DEATH_MARKERS = (
    "This content does not exist",
    "The content you are looking for could not be found",
    "has been automatically removed",
    "has been deleted by the owner",
)

def is_gofile_alive(url: str) -> bool:
    """
    1回だけ超短時間 GET (timeout=0.5s)。先頭 1.5KB で死亡確定文言があれば False。
    それ以外（タイムアウトやエラー）は True（=死と断定不可）。
    """
    url = fix_scheme(url)
    s = _build_scraper()
    try:
        r = s.get(url, timeout=0.5, allow_redirects=True, stream=True)
        if hasattr(r, "raw") and r.raw:
            chunk = r.raw.read(1536, decode_content=True)
            data = chunk.decode(errors="ignore") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        else:
            data = (getattr(r, "text", "") or "")[:1536]
        tl = (data or "").lower()
        for dm in _DEATH_MARKERS:
            if dm.lower() in tl:
                return False
        return True
    except Exception:
        return True

# ====== 抽出ユーティリティ ======
def _resolve_to_gofile(url: str, scraper, timeout: int = 4) -> Optional[str]:
    if not url: return None
    url = fix_scheme(url)
    try:
        pr = urlparse(url)
        # nsnnの /redirect?url=... にも対応（任意ホスト）
        qs = parse_qs(pr.query or "")
        for k in ("url", "u", "target", "to"):
            if k in qs and qs[k]:
                cand = unquote(qs[k][0])
                m = GOFILE_RE.search(cand)
                if m: return fix_scheme(m.group(0))
    except Exception:
        pass
    # ヘッダのLocationに gofile が出る場合
    try:
        r = scraper.get(url, timeout=timeout, allow_redirects=False)
        loc = r.headers.get("Location") or r.headers.get("location")
        if isinstance(loc, str):
            m = GOFILE_RE.search(loc)
            if m: return fix_scheme(m.group(0))
    except Exception:
        pass
    # 直書き
    m = GOFILE_RE.search(url)
    return fix_scheme(m.group(0)) if m else None

def _extract_gofile_from_html(html: str, scraper) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls, seen = [], set()

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href:
            m = GOFILE_RE.search(href)
            go = fix_scheme(m.group(0)) if m else _resolve_to_gofile(href, scraper)
            if go and go not in seen:
                seen.add(go); urls.append(go)

        for attr in ("data-url", "data-clipboard-text", "data-href"):
            v = (a.get(attr) or "").strip()
            if not v: continue
            m2 = GOFILE_RE.search(v)
            if m2:
                go2 = fix_scheme(m2.group(0))
                if go2 and go2 not in seen:
                    seen.add(go2); urls.append(go2)

    for m in GOFILE_RE.findall(html or ""):
        u = fix_scheme(m.strip())
        if u and u not in seen:
            seen.add(u); urls.append(u)
    return urls

# ====== sitemap/wp-api（gofilelab等／速攻で空ならスキップ） ======
def _extract_locs_from_xml(xml_text: str) -> List[str]:
    if not xml_text: return []
    raw = _LOC_RE.findall(xml_text)
    locs = []
    for x in raw:
        u = unescape(x).replace("\n","").replace("\r","").replace("\t","").strip()
        if u: locs.append(u)
    return locs

def _fetch_sitemap_post_urls(scraper, max_pages: int, deadline_ts: Optional[float]) -> List[str]:
    urls = []
    def _get(url: str, timeout: int = 8):
        try:
            r = scraper.get(url, timeout=timeout); r.raise_for_status(); return r.text
        except Exception:
            return None
    xml = _get(SITEMAP_INDEX) or _get(BASE_ORIGIN + "/sitemap.xml")
    if not xml:
        print("[warn] sitemap not available"); return urls
    locs = _extract_locs_from_xml(xml)
    if not locs:
        print("[warn] sitemap had no <loc>"); return urls

    post_sitemaps = [u for u in locs if "post" in u or "news" in u or "posts" in u] or locs
    cap = max_pages * 20
    for sm in post_sitemaps:
        if _deadline_passed(deadline_ts): print("[info] sitemap deadline reached; stop."); break
        xml2 = _get(sm)
        if not xml2: continue
        for u in _extract_locs_from_xml(xml2):
            if u.startswith(BASE_ORIGIN):
                urls.append(u)
                if len(urls) >= cap: break
        if len(urls) >= cap: break
    print(f"[info] sitemap collected {len(urls)} post urls")
    return urls

def _collect_via_sitemap(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    s = _build_scraper()
    posts = _fetch_sitemap_post_urls(s, max_pages=num_pages, deadline_ts=deadline_ts)
    if not posts: return []
    all_urls, seen = [], set()
    for i, post_url in enumerate(posts, 1):
        if _deadline_passed(deadline_ts): print(f"[info] sitemap deadline at post {i}; stop."); break
        try:
            r = s.get(post_url, timeout=8); r.raise_for_status(); html = r.text
        except Exception as e:
            print(f"[warn] sitemap detail fetch failed: {post_url} ({e})"); continue
        for u in _extract_gofile_from_html(html, s):
            if u not in seen:
                seen.add(u); all_urls.append(u)
        if len(all_urls) >= RAW_LIMIT:  # 早期打ち切り
            return all_urls[:RAW_LIMIT]
        time.sleep(0.08)
    return all_urls[:RAW_LIMIT]

def _collect_via_wp_api(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    s = _build_scraper()
    all_urls, seen = [], set()
    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts): print(f"[info] wp-api deadline at page {p}; stop."); break
        api = WP_POSTS_API.format(page=p)
        try:
            r = s.get(api, timeout=8)
            if "json" not in (r.headers.get("Content-Type","")): raise ValueError("non-json returned")
            arr = r.json()
        except Exception as e:
            print(f"[warn] wp-api page {p} failed: {e}"); break
        if not isinstance(arr, list) or not arr: break
        for item in arr:
            html = (item.get("content", {}) or {}).get("rendered", "") if isinstance(item, dict) else ""
            for u in _extract_gofile_from_html(html, s):
                if u not in seen:
                    seen.add(u); all_urls.append(u)
        if len(all_urls) >= RAW_LIMIT:
            return all_urls[:RAW_LIMIT]
        time.sleep(0.12)
    return all_urls[:RAW_LIMIT]

# ====== Playwright（年齢ゲート突破 / エラー耐性強化 / 早期打ち切り） ======
def _playwright_ctx(pw):
    browser = pw.chromium.launch(headless=True, args=[
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
    ])
    ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="ja-JP")
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP','ja'] });
        try {
          // 年齢同意を初期化時に仕込む（描画前）
          localStorage.setItem('ageVerified','1');
          localStorage.setItem('adult','true');
          localStorage.setItem('age_verified','true');
          localStorage.setItem('age_verified_at', Date.now().toString());
        } catch(e){}
    """)
    # Cookie は対象オリジンのドメインに対しても付与
    try:
        host = urlparse(BASE_ORIGIN).hostname or ""
        doms = set([host, "."+host if not host.startswith(".") else host])
        for dom in doms:
            if not dom: continue
            ctx.add_cookies([
                {"name": "ageVerified", "value": "1", "domain": dom, "path": "/"},
                {"name": "adult",       "value": "true", "domain": dom, "path": "/"},
            ])
    except Exception:
        pass
    ctx.set_default_timeout(15000)
    return ctx

def _bypass_age_gate(page):
    # localStorage で念押し
    try:
        page.evaluate("""
          try{
            localStorage.setItem('ageVerified','1');
            localStorage.setItem('adult','true');
            localStorage.setItem('age_verified','true');
            localStorage.setItem('age_verified_at', Date.now().toString());
          }catch(e){}
        """)
    except Exception:
        pass
    page.wait_for_timeout(150)

    checkbox_sels = [
        "input[type='checkbox']",
        "label:has-text('18') >> input[type='checkbox']",
        "label:has-text('成人') >> input[type='checkbox']",
        "label:has-text('同意') >> input[type='checkbox']",
        "text=18歳以上です >> xpath=..//input[@type='checkbox']",
        "xpath=//input[@type='checkbox']",
    ]
    button_sels = [
        "text=同意して閲覧する", "text=同意して入場", "text=同意して閲覧",
        "text=同意する", "button:has-text('同意')",
        "text=I Agree", "button:has-text('I Agree')",
        "text=Enter", "button:has-text('Enter')",
    ]

    # チェック → ボタンの順に総当たり
    try:
        for sel in checkbox_sels:
            try:
                el = page.locator(sel).first
                if el and el.is_visible():
                    el.click(force=True, timeout=1000)
                    page.wait_for_timeout(120)
                    break
            except Exception:
                continue

        for sel in button_sels:
            try:
                btn = page.locator(sel).first
                if btn and btn.is_visible():
                    btn.click(force=True, timeout=1500)
                    page.wait_for_timeout(200)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # 反映待ち
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass

def _get_html_pw(url: str, scroll_steps: int = 6, wait_ms: int = 600) -> str:
    with sync_playwright() as pw:
        ctx = _playwright_ctx(pw)
        page = ctx.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": BASE_ORIGIN,
            "Connection": HEADERS["Connection"],
        })

        # 直接ターゲットへ遷移 → 年齢同意突破
        page.goto(url, wait_until="domcontentloaded", timeout=22000)
        _bypass_age_gate(page)

        # まだゲートが残っていそうなら軽くリロード
        try:
            html_now = page.content() or ""
            if ("同意" in html_now and "成人" in html_now) or ("I Agree" in html_now):
                page.reload(wait_until="domcontentloaded", timeout=15000)
                _bypass_age_gate(page)
        except Exception:
            pass

        # スクロールで遅延ロード要素を引っ張る
        for _ in range(scroll_steps):
            try:
                page.mouse.wheel(0, 1500)
            except Exception:
                pass
            page.wait_for_timeout(wait_ms)

        html = ""
        try:
            html = page.content()
        except Exception:
            html = ""
        ctx.close()
        return html

def _extract_article_links_from_list(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links, seen = [], set()

    # 代表セレクタ（WordPress想定／他サイトでもそこそこヒット）
    for sel in ["article a", ".entry-title a", "a[rel='bookmark']"]:
        for a in soup.select(sel):
            href = a.get("href")
            if not href: continue
            url = urljoin(BASE_ORIGIN, href.strip())
            if url not in seen:
                seen.add(url); links.append(url)

    # セーフティ: 内部リンクでノイズ除外
    if len(links) < 12:
        base_host = urlparse(BASE_ORIGIN).hostname or ""
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#"): continue
            url = urljoin(BASE_ORIGIN, href)
            pr = urlparse(url)
            if pr.netloc and base_host and (base_host not in pr.netloc):
                continue
            bad = ("/newest","/category/","/tag/","/page/","/search","/author","/feed","/privacy","/contact")
            if any(x in pr.path for x in bad): continue
            if pr.path.endswith((".jpg",".png",".gif",".webp",".svg",".css",".js",".zip",".rar",".pdf",".xml")): continue
            if url not in seen:
                seen.add(url); links.append(url)

    # nsnn の一覧では直接 redirect?url=... が並ぶケース → そのまま記事扱い
    if len(links) < 5:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "redirect?url=" in href:
                url = urljoin(BASE_ORIGIN, href)
                if url not in seen:
                    seen.add(url); links.append(url)

    return links[:50]

def _collect_via_playwright(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    s = _build_scraper()
    all_urls, seen_urls, seen_posts = [], set(), set()

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] pw deadline at list page {p}; stop."); break

        list_url = BASE_LIST_URL.format(page=p)
        try:
            lhtml = _get_html_pw(list_url, scroll_steps=4, wait_ms=520)
        except Exception as e:
            print(f"[warn] playwright list {p} failed: {e}"); lhtml = ""

        article_urls = _extract_article_links_from_list(lhtml) if lhtml else []
        print(f"[info] page {p}: found {len(article_urls)} article links")

        # 詳細へ（早期に RAW_LIMIT 到達を狙う）
        added = 0
        for post_url in article_urls:
            if _deadline_passed(deadline_ts): break
            if post_url in seen_posts: continue
            seen_posts.add(post_url)

            try:
                dhtml = _get_html_pw(post_url, scroll_steps=2, wait_ms=420)
            except Exception as e:
                print(f"[warn] playwright detail failed: {post_url} ({e})"); dhtml = ""

            urls = _extract_gofile_from_html(dhtml, s) if dhtml else []
            # nsnn の redirect 自体からも抽出（HTMLが空でもURL中に含まれることがある）
            if not urls:
                m = _resolve_to_gofile(post_url, s)
                if m: urls = [m]

            for u in urls:
                if u not in seen_urls:
                    seen_urls.add(u); all_urls.append(u); added += 1
                    if len(all_urls) >= RAW_LIMIT:
                        print(f"[info] early stop: reached RAW_LIMIT={RAW_LIMIT} (total {len(all_urls)})")
                        return all_urls[:RAW_LIMIT]
            time.sleep(0.08)

        print(f"[info] page {p}: extracted {added} new urls (total {len(all_urls)})")
        time.sleep(0.15)

    return all_urls[:RAW_LIMIT]

# ====== エントリーポイント ======
def fetch_listing_pages(num_pages: int = 100, deadline_ts: Optional[float] = None) -> List[str]:
    # gofilelab など WPサイトはまず sitemap / wp-api を当て、ダメなら Playwright
    urls = _collect_via_sitemap(num_pages=num_pages, deadline_ts=deadline_ts)
    if urls: return urls[:RAW_LIMIT]
    urls = _collect_via_wp_api(num_pages=num_pages, deadline_ts=deadline_ts)
    if urls: return urls[:RAW_LIMIT]
    return _collect_via_playwright(num_pages=num_pages, deadline_ts=deadline_ts)

def collect_fresh_gofile_urls(
    already_seen: Set[str], want: int = 3, num_pages: int = 100, deadline_sec: Optional[int] = None
) -> List[str]:
    # bot側未指定なら環境変数 SCRAPE_TIMEOUT_SEC を採用
    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None
    raw = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 先頭 FILTER_LIMIT 件だけ超軽量判定。want 到達で即返す。
    candidates = [u for u in raw if u not in already_seen][:max(1, FILTER_LIMIT)]
    uniq, seen_now = [], set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop."); break
        if url in seen_now: continue
        if is_gofile_alive(url):
            uniq.append(url); seen_now.add(url)
            if len(uniq) >= want:
                break
    return uniq
