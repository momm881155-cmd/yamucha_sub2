# goxplorer.py — monsnode + x.gd 専用版（bot.py 互換）

import os
import re
import time
from urllib.parse import urljoin
from typing import List, Set, Optional

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =========================
#   設定
# =========================

BASE_ORIGIN = os.getenv("BASE_ORIGIN", "https://monsnode.com").rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_ORIGIN,
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))


def _monsnode_search_urls() -> List[str]:
    """
    検索スタートとなる search.php の URL 群。
    環境変数 MONSNODE_SEARCH_URLS で上書きも可能。
    """
    env = os.getenv("MONSNODE_SEARCH_URLS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    # デフォルト：指定の 5 本
    return [
        "https://monsnode.com/search.php?search=992ultra",
        "https://monsnode.com/search.php?search=verycoolav",
        "https://monsnode.com/search.php?search=bestav8",
        "https://monsnode.com/search.php?search=movieszzzz",
        "https://monsnode.com/search.php?search=himitukessya0",
    ]


# video.twimg.com の mp4 抽出
MP4_RE = re.compile(
    r"https://video\.twimg\.com/[^\s\"']*?\.mp4[^\s\"']*",
    re.I,
)


def _now() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts


# =========================
#   x.gd 短縮
# =========================

def shorten_via_xgd(long_url: str) -> str:
    """
    x.gd の API を使って URL を短縮する。
    失敗時は元URLのまま返す。
    """
    api_key = os.getenv("XGD_API_KEY", "").strip()
    if not api_key:
        return long_url

    try:
        r = requests.get(
            "https://xgd.io/V1/shorten",
            params={"url": long_url, "key": api_key},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        short = (data.get("shorturl") or data.get("short_url") or "").strip()
        if short:
            return short
    except Exception as e:
        print(f"[warn] x.gd shorten failed for {long_url}: {e}")

    return long_url


# =========================
#   Playwright 共通
# =========================

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
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP','ja'] });
    """)
    return ctx


def fetch_html_with_playwright(url: str, timeout_ms: int = 20000) -> Optional[str]:
    """monsnode は requests だと 403 になるので Playwright で取得。"""
    try:
        with sync_playwright() as pw:
            ctx = _playwright_ctx(pw)
            page = ctx.new_page()
            page.set_extra_http_headers({
                "Accept": HEADERS["Accept"],
                "Accept-Language": HEADERS["Accept-Language"],
                "Referer": HEADERS["Referer"],
                "Connection": HEADERS["Connection"],
            })

            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # 軽くスクロールして遅延ロードを促す
            try:
                for _ in range(4):
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(200)
            except Exception:
                pass

            html = page.content()
            ctx.close()
            return html
    except Exception as e:
        print(f"[warn] fetch_html_with_playwright failed: {url} ({e})")
        return None


# =========================
#   一覧ページ → 詳細リンク抽出
# =========================

def extract_detail_links_from_list(html: str) -> List[str]:
    """
    検索結果ページから「動画詳細 or リダイレクトページ」へのリンクを抽出する。

    例:
      - https://monsnode.com/v1951182235140274713
      - https://monsnode.com/redirect.php?v=20892092

    どちらも、クリックの先で mp4 が見つかる可能性があるので対象にする。
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href:
            continue

        # 絶対URLに正規化
        full = urljoin(BASE_ORIGIN, href)

        # monsnode 以外（広告ドメインなど）は無視
        if "monsnode.com" not in full:
            continue

        # 対象とするリンク:
        #   - /vxxxxx... （ユーザページ/動画詳細）
        #   - redirect.php?v=xxxxxx （広告挟まるが最終的に mp4 へ飛ぶ）
        if ("/v" in full) or ("redirect.php?v=" in full):
            if full not in seen:
                seen.add(full)
                links.append(full)

    return links


# =========================
#   詳細ページ → mp4 抽出
# =========================

def extract_mp4_urls_from_detail(url: str) -> List[str]:
    """
    動画詳細ページ（/v..., redirect.php?v=...）から video.twimg.com の .mp4 を抽出。
    """
    html = fetch_html_with_playwright(url)
    if not html:
        return []

    found = MP4_RE.findall(html)
    uniq: List[str] = []
    seen: Set[str] = set()
    for u in found:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)

    return uniq


# =========================
#   monsnode 専用収集ルート
# =========================

def _collect_monsnode_urls(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    monsnode の search 結果から:
      - /v... 形式 or redirect.php?v=... のページURLを取得
      - そこから mp4 を抜き出し
    生の mp4 URL を返す（短縮は別フェーズ）。
    """
    all_mp4: List[str] = []
    seen_mp4: Set[str] = set()

    search_urls = _monsnode_search_urls()

    for base in search_urls:
        for page in range(1, num_pages + 1):
            if _deadline_passed(deadline_ts):
                print(f"[info] monsnode deadline at base={base}, page={page}; stop.")
                return all_mp4[:RAW_LIMIT]

            # 1ページ目と2ページ目以降で URL 形式が違うパターンに対応
            if page == 1:
                list_url = base
            else:
                sep = "&" if "?" in base else "?"
                list_url = f"{base}{sep}page={page}&s="

            html = fetch_html_with_playwright(list_url)
            if not html:
                print(f"[warn] monsnode fetch failed: {list_url}")
                continue

            detail_links = extract_detail_links_from_list(html)
            print(f"[info] monsnode list {list_url}: found {len(detail_links)} detail links")

            for durl in detail_links:
                if _deadline_passed(deadline_ts):
                    print("[info] monsnode deadline during detail; stop.")
                    return all_mp4[:RAW_LIMIT]

                mp4s = extract_mp4_urls_from_detail(durl)
                added = 0
                for m in mp4s:
                    if m not in seen_mp4:
                        seen_mp4.add(m)
                        all_mp4.append(m)
                        added += 1
                        if len(all_mp4) >= RAW_LIMIT:
                            print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                            return all_mp4[:RAW_LIMIT]

                if added:
                    print(f"[info] detail {durl}: +{added} mp4 (total {len(all_mp4)})")

            time.sleep(0.1)

    return all_mp4[:RAW_LIMIT]


# =========================
#   fetch_listing_pages (bot.py 互換)
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None
) -> List[str]:
    """旧 goxplorer と同じ名前で、monsnode 専用 mp4 URL リストを返す。"""
    return _collect_monsnode_urls(num_pages=num_pages, deadline_ts=deadline_ts)


# =========================
#   collect_fresh_gofile_urls (bot.py から呼ばれるメイン)
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None
) -> List[str]:
    """
    bot.py から呼び出されるメイン関数。
    - monsnode から mp4 URL を集める
    - state.json 由来の already_seen でフィルタ
    - x.gd で短縮
    - WANT_POST 件だけ返却
    """

    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # 生の mp4 URL 一覧
    raw_mp4 = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 既出除外（元 mp4 URL が state にあればスキップ）
    candidates = [u for u in raw_mp4 if u not in already_seen][:max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break
        if url in seen_now:
            continue

        short = shorten_via_xgd(url)

        # 短縮後URLも state.json にあったらスキップ
        if short in already_seen:
            continue

        seen_now.add(url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]