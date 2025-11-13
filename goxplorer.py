# goxplorer.py — monsnode + x.gd 対応版（完全置き換え）

import os, re, time
from html import unescape
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from typing import List, Set, Optional

import cloudscraper
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ====== 環境 ======
ENV_BASE_ORIGIN   = os.getenv("BASE_ORIGIN", "https://monsnode.com").rstrip("/")
ENV_BASE_LIST_URL = os.getenv("BASE_LIST_URL", ENV_BASE_ORIGIN + "/")
ENV_PAGE1_URL     = os.getenv("PAGE1_URL", ENV_BASE_ORIGIN)

BASE_ORIGIN   = ENV_BASE_ORIGIN
BASE_LIST_URL = ENV_BASE_LIST_URL
PAGE1_URL     = ENV_PAGE1_URL

WP_POSTS_API  = BASE_ORIGIN + "/wp-json/wp/v2/posts?page={page}&per_page=20&_fields=link,content.rendered"
SITEMAP_INDEX = BASE_ORIGIN + "/sitemap_index.xml"

GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)
MP4_RE    = re.compile(r"https?://[^\s\"'>]+\.mp4\b", re.I)
_LOC_RE   = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE | re.DOTALL)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
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
    if http_p:
        proxies["http"]  = http_p
    if https_p:
        proxies["https"] = https_p

    s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    if proxies:
        s.proxies.update(proxies)
    s.headers.update(HEADERS)

    # age gate cookie（無害）
    try:
        host = urlparse(BASE_ORIGIN).hostname or ""
        roots = {host}
        if host and not host.startswith("."):
            roots.add("." + host)
        if host.count(".") >= 1:
            roots.add("." + ".".join(host.split(".")[-2:]))
        for dom in roots:
            s.cookies.set("ageVerified", "1", domain=dom, path="/")
            s.cookies.set("adult", "true",     domain=dom, path="/")
    except Exception:
        pass
    return s

def fix_scheme(url: str) -> str:
    if url.startswith("htps://"):
        return "https://" + url[len("htps://"):]
    if url.startswith("http://gofile.io/"):  # 旧 gofile 用
        return "https://" + url[len("http://"):]
    return url

def _now() -> float:
    return time.monotonic()

def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts

# ====== 死活（旧 gofile 用） ======
_DEATH_MARKERS = (
    "This content does not exist",
    "The content you are looking for could not be found",
    "has been automatically removed",
    "has been deleted by the owner",
)
def is_gofile_alive(url: str) -> bool:
    url = fix_scheme(url)
    s = _build_scraper()
    try:
        r = s.get(url, timeout=0.6, allow_redirects=True, stream=True)
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
        return True  # 断定不可は通す

# ====== x.gd 短縮 ======
def shorten_via_xgd(long_url: str) -> str:
    """x.gd の API を使って URL を短縮する。失敗時は元 URL をそのまま返す。"""
    api_key = os.getenv("XGD_API_KEY", "").strip()
    if not api_key:
        return long_url
    try:
        r = requests.get(
            "https://xgd.io/V1/shorten",
            params={"url": long_url, "key": api_key},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        short = (data.get("shorturl") or data.get("short_url") or "").strip()
        return short or long_url
    except Exception as e:
        print(f"[warn] x.gd shorten failed for {long_url}: {e}")
        return long_url

# ====== 汎用 gofile 抽出（旧サイト用に残す） ======
def _resolve_to_gofile(url: str, scraper, timeout: int = 4) -> Optional[str]:
    if not url:
        return None
    url = fix_scheme(url)
    try:
        pr = urlparse(url)
        qs = parse_qs(pr.query or "")
        for k in ("url","u","target","to"):
            if k in qs and qs[k]:
                cand = unquote(qs[k][0])
                m = GOFILE_RE.search(cand)
                if m:
                    return fix_scheme(m.group(0))
    except Exception:
        pass
    try:  # 3xx の Location
        r = scraper.get(url, timeout=timeout, allow_redirects=False)
        loc = r.headers.get("Location") or r.headers.get("location")
        if isinstance(loc, str):
            m = GOFILE_RE.search(loc)
            if m:
                return fix_scheme(m.group(0))
    except Exception:
        pass
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
                seen.add(go)
                urls.append(go)
        for attr in ("data-url","data-clipboard-text","data-href"):
            v = (a.get(attr) or "").strip()
            if not v:
                continue
            m2 = GOFILE_RE.search(v)
            if m2:
                go2 = fix_scheme(m2.group(0))
                if go2 and go2 not in seen:
                    seen.add(go2)
                    urls.append(go2)
    for m in GOFILE_RE.findall(html or ""):
        u = fix_scheme(m.strip())
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls

# ====== sitemap / wp-api（旧 gofile 系） ======
def _extract_locs_from_xml(xml_text: str) -> List[str]:
    if not xml_text:
        return []
    raw = _LOC_RE.findall(xml_text)
    locs = []
    for x in raw:
        u = unescape(x).replace("\n","").replace("\r","").replace("\t","").strip()
        if u:
            locs.append(u)
    return locs

def _fetch_sitemap_post_urls(scraper, max_pages: int, deadline_ts: Optional[float]) -> List[str]:
    urls = []
    def _get(url: str, timeout: int = 8):
        try:
            r = scraper.get(url, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception:
            return None
    xml = _get(SITEMAP_INDEX) or _get(BASE_ORIGIN + "/sitemap.xml")
    if not xml:
        print("[warn] sitemap not available")
        return urls
    locs = _extract_locs_from_xml(xml)
    if not locs:
        print("[warn] sitemap had no <loc>")
        return urls

    post_sitemaps = [u for u in locs if "post" in u or "news" in u or "posts" in u] or locs
    cap = max_pages * 20
    for sm in post_sitemaps:
        if _deadline_passed(deadline_ts):
            print("[info] sitemap deadline reached; stop.")
            break
        xml2 = _get(sm)
        if not xml2:
            continue
        for u in _extract_locs_from_xml(xml2):
            if u.startswith(BASE_ORIGIN):
                urls.append(u)
                if len(urls) >= cap:
                    break
        if len(urls) >= cap:
            break
    print(f"[info] sitemap collected {len(urls)} post urls")
    return urls

def _collect_via_sitemap(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    s = _build_scraper()
    posts = _fetch_sitemap_post_urls(s, max_pages=num_pages, deadline_ts=deadline_ts)
    if not posts:
        return []
    all_urls, seen = [], set()
    for i, post_url in enumerate(posts, 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] sitemap deadline at post {i}; stop.")
            break
        try:
            r = s.get(post_url, timeout=8)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            print(f"[warn] sitemap detail fetch failed: {post_url} ({e})")
            continue
        for u in _extract_gofile_from_html(html, s):
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
        if len(all_urls) >= RAW_LIMIT:
            return all_urls[:RAW_LIMIT]
        time.sleep(0.06)
    return all_urls[:RAW_LIMIT]

def _collect_via_wp_api(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    s = _build_scraper()
    all_urls, seen = [], set()
    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] wp-api deadline at page {p}; stop.")
            break
        api = WP_POSTS_API.format(page=p)
        try:
            r = s.get(api, timeout=8)
            if "json" not in (r.headers.get("Content-Type","")):
                raise ValueError("non-json returned")
            arr = r.json()
        except Exception as e:
            print(f"[warn] wp-api page {p} failed: {e}")
            break
        if not isinstance(arr, list) or not arr:
            break
        for item in arr:
            html = (item.get("content", {}) or {}).get("rendered", "") if isinstance(item, dict) else ""
            for u in _extract_gofile_from_html(html, s):
                if u not in seen:
                    seen.add(u)
                    all_urls.append(u)
        if len(all_urls) >= RAW_LIMIT:
            return all_urls[:RAW_LIMIT]
        time.sleep(0.08)
    return all_urls[:RAW_LIMIT]

# ====== Playwright 共通 ======
def _playwright_ctx(pw):
    browser = pw.chromium.launch(headless=True, args=[
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
    ])
    ctx = browser.new_context(
        user_agent=HEADERS["User-Agent"],
        locale="ja-JP",
        viewport={"width": 1360, "height": 2400}
    )
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP','ja'] });
        try{
          localStorage.setItem('ageVerified','1');
          localStorage.setItem('adult','true');
          localStorage.setItem('age_verified','true');
          localStorage.setItem('age_verified_at', Date.now().toString());
        }catch(e){}
    """)
    try:
        host = urlparse(BASE_ORIGIN).hostname or ""
        doms = {host}
        if host and not host.startswith("."):
            doms.add("." + host)
        for dom in doms:
            if dom:
                ctx.add_cookies([
                    {"name":"ageVerified","value":"1","domain":dom,"path":"/"},
                    {"name":"adult","value":"true","domain":dom,"path":"/"},
                ])
    except Exception:
        pass
    ctx.set_default_timeout(22000)
    return ctx

# ====== 一般 Playwright ルート（旧 gofile 用に残す） ======
def _extract_article_links_from_list(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links, seen = [], set()
    for sel in ["article a", ".entry-title a", "a[rel='bookmark']"]:
        for a in soup.select(sel):
            href = a.get("href")
            if not href:
                continue
            url = urljoin(BASE_ORIGIN, href.strip())
            if url not in seen:
                seen.add(url)
                links.append(url)

    if len(links) < 12:
        base_host = urlparse(BASE_ORIGIN).hostname or ""
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#"):
                continue
            url = urljoin(BASE_ORIGIN, href)
            pr = urlparse(url)
            if pr.netloc and base_host and (base_host not in pr.netloc):
                continue
            bad = ("/newest","/category/","/tag/","/page/","/search","/author","/feed","/privacy","/contact")
            if any(x in pr.path for x in bad):
                continue
            if pr.path.endswith((".jpg",".png",".gif",".webp",".svg",".css",".js",".zip",".rar",".pdf",".xml")):
                continue
            if url not in seen:
                seen.add(url)
                links.append(url)
    if len(links) < 5:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "redirect?url=" in href:
                url = urljoin(BASE_ORIGIN, href)
                if url not in seen:
                    seen.add(url)
                    links.append(url)
    return links[:50]

def _collect_via_playwright(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    s = _build_scraper()
    all_urls, seen_urls, seen_posts = [], set(), set()
    with sync_playwright() as pw:
        ctx = _playwright_ctx(pw)
        page = ctx.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": BASE_ORIGIN,
            "Connection": HEADERS["Connection"],
        })

        for p in range(1, num_pages + 1):
            if _deadline_passed(deadline_ts):
                print(f"[info] pw deadline at list page {p}; stop.")
                break
            list_url = BASE_LIST_URL.format(page=p)
            try:
                page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                print(f"[warn] playwright list {p} failed: {e}")
                continue

            for _ in range(6):
                try:
                    page.mouse.wheel(0, 1600)
                except Exception:
                    pass
                page.wait_for_timeout(200)

            lhtml = page.content()
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
                    page.goto(post_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[warn] playwright detail failed: {post_url} ({e})")
                    continue

                for _ in range(2):
                    try:
                        page.mouse.wheel(0, 1500)
                    except Exception:
                        pass
                    page.wait_for_timeout(180)
                dhtml = page.content() or ""

                urls = _extract_gofile_from_html(dhtml, s) if dhtml else []
                if not urls:
                    m = _resolve_to_gofile(post_url, s)
                    if m:
                        urls = [m]

                for u in urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        all_urls.append(u)
                        added += 1
                        if len(all_urls) >= RAW_LIMIT:
                            print(f"[info] early stop: reached RAW_LIMIT={RAW_LIMIT} (total {len(all_urls)})")
                            ctx.close()
                            return all_urls[:RAW_LIMIT]
                time.sleep(0.06)

            print(f"[info] page {p}: extracted {added} new urls (total {len(all_urls)})")
        ctx.close()
    return all_urls[:RAW_LIMIT]

# ====== monsnode 検索URL ======
def _monsnode_search_urls() -> List[str]:
    env = os.getenv("MONSNODE_SEARCH_URLS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    return [
        "https://monsnode.com/search.php?search=992ultra",
        "https://monsnode.com/search.php?search=verycoolav",
        "https://monsnode.com/search.php?search=bestav8",
        "https://monsnode.com/search.php?search=movieszzzz",
        "https://monsnode.com/search.php?search=himitukessya0",
    ]

# ====== monsnode 専用：Playwright で .mp4 ネットワークURLを取得 ======
def _collect_monsnode_mp4(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    all_urls: List[str] = []
    seen_mp4: Set[str] = set()
    seen_detail: Set[str] = set()

    search_bases = _monsnode_search_urls()

    with sync_playwright() as pw:
        ctx = _playwright_ctx(pw)
        page = ctx.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": BASE_ORIGIN,
            "Connection": HEADERS["Connection"],
        })

        def on_request(request):
            url = request.url
            if ".mp4" in url:
                if url not in seen_mp4:
                    seen_mp4.add(url)
                    all_urls.append(url)
                    print(f"[info] caught mp4: {url}")
        page.on("request", on_request)

        for base in search_bases:
            for p in range(0, num_pages):
                if _deadline_passed(deadline_ts):
                    print(f"[info] monsnode deadline at page {p}; stop.")
                    ctx.close()
                    return all_urls[:RAW_LIMIT]

                if p == 0:
                    search_url = base
                else:
                    sep = "&" if "?" in base else "?"
                    search_url = f"{base}{sep}page={p}&s="

                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[warn] monsnode list goto failed: {search_url} ({e})")
                    break

                page.wait_for_timeout(1200)

                try:
                    detail_links = page.evaluate("""
                        () => Array.from(document.querySelectorAll('a[href*="/v"]'))
                                   .map(a => a.href)
                    """) or []
                except Exception:
                    detail_links = []

                print(f"[info] monsnode list {search_url}: found {len(detail_links)} detail links")

                for durl in detail_links:
                    if _deadline_passed(deadline_ts):
                        break
                    if durl in seen_detail:
                        continue
                    seen_detail.add(durl)

                    try:
                        page.goto(durl, wait_until="domcontentloaded", timeout=20000)
                    except Exception as e:
                        print(f"[warn] monsnode detail goto failed: {durl} ({e})")
                        continue

                    # 自動再生が走らない場合に備えて Watch ボタンを押してみる
                    try:
                        page.click("text=Watch", timeout=2000)
                    except Exception:
                        pass

                    page.wait_for_timeout(4000)

                    if len(all_urls) >= RAW_LIMIT:
                        print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                        ctx.close()
                        return all_urls[:RAW_LIMIT]

        ctx.close()
    return all_urls[:RAW_LIMIT]

# ====== 収集エントリ ======
def fetch_listing_pages(num_pages: int = 100, deadline_ts: Optional[float] = None) -> List[str]:
    host = urlparse(BASE_ORIGIN).hostname or ""

    if "monsnode.com" in (host or ""):
        return _collect_monsnode_mp4(num_pages=num_pages, deadline_ts=deadline_ts)

    # 旧 gofile 系（nsnn/orevideo など）が必要なとき用
    urls = _collect_via_sitemap(num_pages=num_pages, deadline_ts=deadline_ts)
    if urls:
        return urls[:RAW_LIMIT]
    urls = _collect_via_wp_api(num_pages=num_pages, deadline_ts=deadline_ts)
    if urls:
        return urls[:RAW_LIMIT]
    return _collect_via_playwright(num_pages=num_pages, deadline_ts=deadline_ts)

# ====== フィルタ・返却 ======
def collect_fresh_gofile_urls(
    already_seen: Set[str], want: int = 3, num_pages: int = 100, deadline_sec: Optional[int] = None
) -> List[str]:
    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None
    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    raw = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    host = urlparse(BASE_ORIGIN).hostname or ""
    is_monsnode = "monsnode.com" in (host or "")

    uniq_raw: List[str] = []
    uniq_short: List[str] = []
    seen_now: Set[str] = set()

    for url in raw:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break
        if url in seen_now:
            continue

        if is_monsnode:
            # monsnode: .mp4 をそのまま使用し、短縮URL重複を抑止
            short = shorten_via_xgd(url)
            if short in already_seen:
                continue
            seen_now.add(url)
            uniq_raw.append(url)
            uniq_short.append(short)
            if len(uniq_short) >= want:
                break
        else:
            # 旧 gofile: 元のロジック（死活チェック + raw URL のまま）を維持
            if url in already_seen:
                continue
            if not is_gofile_alive(url):
                continue
            seen_now.add(url)
            uniq_raw.append(url)
            uniq_short.append(url)
            if len(uniq_short) >= want:
                break

    # monsnode の場合は短縮したURLを返す。旧 gofile の場合は元URLを返す。
    return uniq_short[:want]
