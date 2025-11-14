# goxplorer.py — monsnode の redirect.php をそのまま短縮して返す版

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
        "Chrome/123.0.0.0 Safari/123.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://monsnode.com",
    "Connection": "keep-alive",
}

# 一度に拾う最大件数
RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))


def _monsnode_search_words() -> List[str]:
    """
    monsnode の検索ワードリスト。
    MONSNODE_SEARCH_TERMS 環境変数でカンマ or 改行区切り指定があればそちらを優先。
    """
    env = os.getenv("MONSNODE_SEARCH_TERMS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        words = [p.strip() for p in parts if p.strip()]
        if words:
            return words

    # デフォルト 5 ワード
    return [
        "992ultra",
        "verycoolav",
        "bestav8",
        "movieszzzz",
        "himitukessya0",
    ]


def _now() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts


def _normalize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")


# =========================
#   x.gd 短縮
# =========================

def shorten_via_xgd(long_url: str) -> str:
    """
    x.gd の API で URL を短縮。
    失敗したら元 URL をそのまま返す。
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
#   Playwright 共通（一覧ページ取得専用）
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


# =========================
#   一覧ページ → redirect.php 抽出
# =========================

def extract_redirect_links_from_list(html: str) -> List[str]:
    """
    検索結果ページの HTML から redirect.php?v=... のリンクを全部抜く。
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "redirect.php" not in href:
            continue
        full = urljoin(BASE_ORIGIN, href)
        if full not in seen:
            seen.add(full)
            links.append(full)

    print(f"[debug] extract_redirect_links_from_list: {len(links)} links")
    return links


def _collect_monsnode_redirects(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    検索ワード × ページ(1..num_pages) から redirect.php の URL を集める。
    ここではまだ短縮しない。
    """
    all_urls: List[str] = []
    seen: Set[str] = set()
    search_words = _monsnode_search_words()

    with sync_playwright() as pw:
        ctx = _playwright_ctx(pw)
        page = ctx.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": HEADERS["Referer"],
            "Connection": HEADERS["Connection"],
        })

        for word in search_words:
            for p in range(1, num_pages + 1):
                if _deadline_passed(deadline_ts):
                    print(f"[info] monsnode deadline at search={word}, page={p}; stop.")
                    ctx.close()
                    return all_urls[:RAW_LIMIT]

                if p == 1:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}"
                else:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}&page={p}&s="

                try:
                    page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[warn] playwright list goto failed: {list_url} ({e})")
                    continue

                # 軽くスクロールして遅延ロードさせる
                try:
                    for _ in range(4):
                        page.mouse.wheel(0, 1400)
                        page.wait_for_timeout(200)
                except Exception:
                    pass

                html = page.content()
                redirects = extract_redirect_links_from_list(html)
                print(f"[info] monsnode list {list_url}: found {len(redirects)} redirect links")

                for u in redirects:
                    if u not in seen:
                        seen.add(u)
                        all_urls.append(u)
                        if len(all_urls) >= RAW_LIMIT:
                            print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                            ctx.close()
                            return all_urls[:RAW_LIMIT]

                time.sleep(0.2)

        ctx.close()

    return all_urls[:RAW_LIMIT]


# =========================
#   fetch_listing_pages（bot.py 互換）
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None
) -> List[str]:
    """
    旧 goxplorer のインターフェイス互換。
    redirect.php の生 URL を返す。
    """
    return _collect_monsnode_redirects(num_pages=num_pages, deadline_ts=deadline_ts)


# =========================
#   collect_fresh_gofile_urls（bot.py から呼ばれる）
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    - monsnode から redirect.php の URL を集める
    - state.json 由来の already_seen で重複を除外
       （元URL・短縮URL 両方をチェック）
    - x.gd で短縮
    - want 件だけ返す
    """

    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # 生の redirect.php 一覧
    raw_redirects = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 多すぎると時間がかかるので、まず FILTER_LIMIT 件に絞る
    candidates = raw_redirects[: max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break

        norm_url = _normalize_url(url)

        # 同一 run 内での重複
        if norm_url in seen_now:
            continue

        # 元の redirect.php がすでに state.json にある場合はスキップ
        if norm_url in already_seen:
            continue

        # x.gd で短縮
        short = shorten_via_xgd(url)
        norm_short = _normalize_url(short)

        # 短縮後 URL が state.json にある場合もスキップ
        if norm_short in already_seen:
            continue

        seen_now.add(norm_url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]