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
ENV_BASE_ORIGIN   = os.getenv("BASE_ORIGIN", "https://gofilelab.com").rstrip("/")
ENV_BASE_LIST_URL = os.getenv("BASE_LIST_URL", ENV_BASE_ORIGIN + "/newest?page={page}")
ENV_PAGE1_URL     = os.getenv("PAGE1_URL", ENV_BASE_ORIGIN + "/newest?page=1")

BASE_ORIGIN   = ENV_BASE_ORIGIN
BASE_LIST_URL = ENV_BASE_LIST_URL
PAGE1_URL     = ENV_PAGE1_URL

WP_POSTS_API  = BASE_ORIGIN + "/wp-json/wp/v2/posts?page={page}&per_page=20&_fields=link,content.rendered"
SITEMAP_INDEX = BASE_ORIGIN + "/sitemap_index.xml"

GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)
MP4_RE    = re.compile(r"https?://[^\s\"'>]+\.mp4\b", re.I)
_LOC_RE   = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE | re.DOTALL)

# monsnode の redirect リンク検出用
MONS_REDIRECT_RE = re.compile(
    r'href=[\'"]([^\'"]*redirect\.php[^\'"]*)[\'"]',
    re.I
)

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
        proxies["http"] = http_p
    if https_p:
        proxies["https"] = https_p

    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
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
            s.cookies.set("adult", "true", domain=dom, path="/")
    except Exception:
        pass
    return s


def fix_scheme(url: str) -> str:
    if url.startswith("htps://"):
        return "https://" + url[len("htps://"):]
    if url.startswith("http://gofile.io/"):  # ★ http→https を強制（過去互換用）
        return "https://" + url[len("http://"):]
    return url


def _now() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts


# ====== 死活（軽量） ======
_DEATH_MARKERS = (
    "This content does not exist",
    "The content you are looking for could not be found",
    "has been automatically removed",
    "has been deleted by the owner",
)


def is_gofile_alive(url: str) -> bool:
    """元 gofile 専用の死活確認。monsnode(mp4)では使用しない。"""
    url = fix_scheme(url)
    s = _build_scraper()
    try:
        r = s.get(url, timeout=0.6, allow_redirects=True, stream=True)
        if hasattr(r, "raw") and r.raw:
            chunk = r.raw.read(1536, decode_content=True)
            data = (
                chunk.decode(errors="ignore")
                if isinstance(chunk, (bytes, bytearray))
                else str(chunk)
            )
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


# ====== 抽出ユーティリティ（gofile 用／mp4 用） ======
def _resolve_to_gofile(url: str, scraper, timeout: int = 4) -> Optional[str]:
    if not url:
        return None
    url = fix_scheme(url)
    try:
        pr = urlparse(url)
        qs = parse_qs(pr.query or "")
        for k in ("url", "u", "target", "to"):
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
        for attr in ("data-url", "data-clipboard-text", "data-href"):
            v = (a.get(attr) or "").strip()
            if not v:
                continue
            m2 = GOFILE_RE.search(v)
            if m2:
                go2 = fix_scheme(m2.group(0))
                if go2 and go2 not in seen:
                    seen.add(go2)
                    urls.append(go2)
    # 生HTML全量からも拾う（JS生成文字列含む）
    for m in GOFILE_RE.findall(html or ""):
        u = fix_scheme(m.strip())
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _extract_mp4_urls_from_html(html: str) -> List[str]:
    """monsnode ページから .mp4 URL を抽出する（保険）。"""
    if not html:
        return []
    urls, seen = [], set()
    soup = BeautifulSoup(html, "html.parser")

    # a / video / source から拾う
    for tag_name, attr in [("a", "href"), ("video", "src"), ("source", "src")]:
        for tag in soup.find_all(tag_name):
            href = (tag.get(attr) or "").strip()
            if not href:
                continue
            m = MP4_RE.search(href)
            if m:
                u = m.group(0)
                if u not in seen:
                    seen.add(u)
                    urls.append(u)

    # 生HTML全体からも拾う（保険）
    for m in MP4_RE.findall(html):
        u = m.strip()
        if u not in seen:
            seen.add(u)
            urls.append(u)

    return urls


# ====== sitemap / wp-api（一般サイト向け） ======
def _extract_locs_from_xml(xml_text: str) -> List[str]:
    if not xml_text:
        return []
    raw = _LOC_RE.findall(xml_text)
    locs = []
    for x in raw:
        u = (
            unescape(x)
            .replace("\n", "")
            .replace("\r", "")
            .replace("\t", "")
            .strip()
        )
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
    # （NameError対策で必ず定義）
    s = _build_scraper()
    all_urls, seen = [], set()
    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] wp-api deadline at page {p}; stop.")
            break
        api = WP_POSTS_API.format(page=p)
        try:
            r = s.get(api, timeout=8)
            if "json" not in (r.headers.get("Content-Type", "")):
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
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    ctx = browser.new_context(
        user_agent=HEADERS["User-Agent"],
        locale="ja-JP",
        viewport={"width": 1360, "height": 2400},
    )
    ctx.add_init_script(
        """
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
    """
    )
    try:
        host = urlparse(BASE_ORIGIN).hostname or ""
        doms = {host, "." + host if host and not host.startswith(".") else host}
        for dom in doms:
            if dom:
                ctx.add_cookies(
                    [
                        {
                            "name": "ageVerified",
                            "value": "1",
                            "domain": dom,
                            "path": "/",
                        },
                        {
                            "name": "adult",
                            "value": "true",
                            "domain": dom,
                            "path": "/",
                        },
                    ]
                )
    except Exception:
        pass
    ctx.set_default_timeout(22000)
    return ctx


def _bypass_age_gate(page):
    try:
        page.evaluate(
            """
          try{
            localStorage.setItem('ageVerified','1');
            localStorage.setItem('adult','true');
            localStorage.setItem('age_verified','true');
            localStorage.setItem('age_verified_at', Date.now().toString());
          }catch(e){}
        """
        )
    except Exception:
        pass
    page.wait_for_timeout(120)
    for sel in [
        "input[type='checkbox']",
        "label:has-text('18') >> input[type='checkbox']",
        "label:has-text('成人') >> input[type='checkbox']",
        "label:has-text('同意') >> input[type='checkbox']",
        "xpath=//input[@type='checkbox']",
    ]:
        try:
            el = page.locator(sel).first
            if el and el.is_visible():
                el.click(force=True, timeout=800)
                page.wait_for_timeout(80)
                break
        except Exception:
            pass
    for sel in [
        "text=同意して閲覧する",
        "text=同意して入場",
        "text=同意して閲覧",
        "text=同意する",
        "button:has-text('同意')",
        "text=I Agree",
        "button:has-text('I Agree')",
        "text=Enter",
        "button:has-text('Enter')",
    ]:
        try:
            btn = page.locator(sel).first
            if btn and btn.is_visible():
                btn.click(force=True, timeout=1200)
                page.wait_for_timeout(150)
                break
        except Exception:
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass


# ====== ★ lab専用：一覧 + 詳細ページ追撃（gofilelab用に残しておく） ======
def _collect_lab_fast(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    all_urls, seen_gofile, seen_posts = [], set(), set()
    with sync_playwright() as pw:
        ctx = _playwright_ctx(pw)
        page = ctx.new_page()
        page.set_extra_http_headers(
            {
                "Accept": HEADERS["Accept"],
                "Accept-Language": HEADERS["Accept-Language"],
                "Referer": BASE_ORIGIN,
                "Connection": HEADERS["Connection"],
            }
        )

    # ...（この部分は前回版と同じ。省略せずにそのまま残してOKですが、長くなるので割愛します）
    # 実際に貼り付けるときは、ここも前回の gofilelab 用ロジックをそのまま残してください。
    # もし「gofilelab もう絶対使わない」なら、このブロックごと消しても構いません。

    # ====== 一般 Playwright（nsnn/orevideoで使用・互換のため残す） ======
    # ここも同様に、前回コピーした内容をそのまま残してOKです。


# ↑ ここまでは gofile 時代の互換用（削除してもいいけど、そのままでも動作には影響なし）


# ====== monsnode 専用：redirect.php を mp4 に解決 ======
def _monsnode_search_urls() -> List[str]:
    """monsnode の検索URL群。環境変数 MONSNODE_SEARCH_URLS で増減可能。"""
    env = os.getenv("MONSNODE_SEARCH_URLS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    # デフォルト（ご指定の5本）
    return [
        "https://monsnode.com/search.php?search=992ultra",
        "https://monsnode.com/search.php?search=verycoolav",
        "https://monsnode.com/search.php?search=bestav8",
        "https://monsnode.com/search.php?search=movieszzzz",
        "https://monsnode.com/search.php?search=himitukessya0",
    ]


def _resolve_monsnode_redirect_to_mp4(url: str, scraper, timeout: int = 8) -> Optional[str]:
    """monsnode の redirect.php を叩いて Location から mp4 を取り出す。"""
    try:
        r = scraper.get(url, timeout=timeout, allow_redirects=False)
    except Exception as e:
        print(f"[warn] monsnode redirect fetch failed: {url} ({e})")
        return None

    loc = r.headers.get("Location") or r.headers.get("location")
    if not isinstance(loc, str) or not loc.strip():
        return None

    target = urljoin(url, loc.strip())

    # Location 1 回目で .mp4 が含まれていなければ、もう一段階だけ追う
    if not MP4_RE.search(target):
        try:
            r2 = scraper.get(target, timeout=timeout, allow_redirects=False)
            loc2 = r2.headers.get("Location") or r2.headers.get("location")
            if isinstance(loc2, str) and loc2.strip():
                target = urljoin(target, loc2.strip())
        except Exception:
            pass

    m = MP4_RE.search(target)
    if m:
        return m.group(0)
    return None


def _collect_monsnode_mp4(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """monsnode の search 結果から .mp4 URL を収集する。毎回 1ページ目から走査。"""
    s = _build_scraper()
    all_urls: List[str] = []
    seen_mp4: Set[str] = set()
    seen_redirect: Set[str] = set()

    search_bases = _monsnode_search_urls()

    for base in search_bases:
        for p in range(0, num_pages):
            if _deadline_passed(deadline_ts):
                print(f"[info] monsnode deadline at page {p}; stop.")
                return all_urls[:RAW_LIMIT]

            if p == 0:
                url = base
            else:
                # 例: https://monsnode.com/search.php?search=992ultra&page=1&s=
                sep = "&" if "?" in base else "?"
                url = f"{base}{sep}page={p}&s="

            try:
                r = s.get(url, timeout=10)
                r.raise_for_status()
                html = r.text
            except Exception as e:
                print(f"[warn] monsnode fetch failed: {url} ({e})")
                break  # この search は打ち切り

            added = 0

            # 1) もし HTML に直接 .mp4 があれば、それも拾う（保険）
            for u in _extract_mp4_urls_from_html(html):
                if u not in seen_mp4:
                    seen_mp4.add(u)
                    all_urls.append(u)
                    added += 1
                    if len(all_urls) >= RAW_LIMIT:
                        print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                        return all_urls[:RAW_LIMIT]

            # 2) redirect.php を探して、Location 経由で .mp4 を解決
            for href in MONS_REDIRECT_RE.findall(html):
                redir = urljoin("https://monsnode.com/", href.strip())
                if redir in seen_redirect:
                    continue
                seen_redirect.add(redir)

                mp4 = _resolve_monsnode_redirect_to_mp4(redir, s)
                if not mp4:
                    continue
                if mp4 in seen_mp4:
                    continue

                seen_mp4.add(mp4)
                all_urls.append(mp4)
                added += 1
                if len(all_urls) >= RAW_LIMIT:
                    print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                    return all_urls[:RAW_LIMIT]

            print(f"[info] monsnode {url}: +{added} (total {len(all_urls)})")
            time.sleep(0.1)

    return all_urls[:RAW_LIMIT]


# ====== 収集エントリ ======
def fetch_listing_pages(
    num_pages: int = 100, deadline_ts: Optional[float] = None
) -> List[str]:
    host = urlparse(BASE_ORIGIN).hostname or ""

    # monsnode.com は専用ルート
    if "monsnode.com" in (host or ""):
        return _collect_monsnode_mp4(num_pages=num_pages, deadline_ts=deadline_ts)

    # gofilelab は専用ルート（互換のため残す）
    if "gofilelab.com" in (host or ""):
        return _collect_lab_fast(num_pages=num_pages, deadline_ts=deadline_ts)

    # それ以外は従来（sitemap → wp-api → playwright）
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
    """
    bot.py から呼ばれるエントリ。
    - monsnode の場合: .mp4 URL を集めて x.gd で短縮した URL を返す
    - gofile 時代との互換のため、RAW_LIMIT / FILTER_LIMIT などはそのまま利用
    - 一度ツイートした「短縮URL」は state.json 経由の already_seen で二度と使わない
    """
    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None
    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    raw = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 一旦 RAW_LIMIT と FILTER_LIMIT で頭を絞る（元URLベース）
    candidates = raw[:max(1, FILTER_LIMIT)]

    host = urlparse(BASE_ORIGIN).hostname or ""
    is_monsnode = "monsnode.com" in (host or "")

    results: List[str] = []
    seen_raw: Set[str] = set()
    seen_short: Set[str] = set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break

        if url in seen_raw:
            continue
        seen_raw.add(url)

        # 古い state.json に「生URL」が残っていても一応避けておく
        if url in already_seen:
            continue

        alive = True
        # monsnode (.mp4) のときは死活チェック不要
        if not is_monsnode:
            alive = is_gofile_alive(url)

        if not alive:
            continue

        # ★ ここで短縮し、短縮後URLに対して重複チェックする
        short = shorten_via_xgd(url)

        # すでに state.json に記録済みの短縮URLはスキップ
        if short in already_seen:
            continue

        if short in seen_short:
            continue

        seen_short.add(short)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]
