# goxplorer.py — monsnode redirect 専用版（bot.py 互換 / mp4 には潜らない）

import os
import re
import time
from typing import List, Set, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =========================
#   基本設定
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

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))   # 1回の最大生URL数
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))  # フィルタ後の上限


def _monsnode_search_words() -> List[str]:
    """
    検索ワード一覧（環境変数 MONSNODE_SEARCH_TERMS で上書き可）
    例: MONSNODE_SEARCH_TERMS="992ultra,verycoolav,bestav8,movieszzzz,himitukessya0"
    """
    env = os.getenv("MONSNODE_SEARCH_TERMS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        words = [p.strip() for p in parts if p.strip()]
        if words:
            return words

    # デフォルト 5つ
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
            headers={"User-Agent": HEADERS["User-Agent"]},
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
#   Playwright（HTML取得専用）
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
    """
    monsnode は requests だけだと 403 になりやすいので、
    Playwright で HTML を取得してから BeautifulSoup で解析する。
    """
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

            # jscroll の遅延ロード対策で軽くスクロール
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
#   一覧ページ → redirect.php 抽出
# =========================

def extract_redirect_links_from_list(html: str) -> List[str]:
    """
    検索結果ページから redirect.php?v=... のリンクを全部抜く。
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "redirect.php?v=" not in href:
            continue

        # 相対パスだったら絶対URLに
        full = urljoin(BASE_ORIGIN, href)
        if full not in seen:
            seen.add(full)
            links.append(full)

    return links


def _collect_redirect_urls_from_search_pages(
    num_pages: int,
    deadline_ts: Optional[float],
) -> List[str]:
    """
    monsnode の search.php?search=WORD (&page=N) を直接叩いて、
    redirect.php?v=... のURLを集める。
    """
    all_urls: List[str] = []
    seen: Set[str] = set()

    search_words = _monsnode_search_words()

    for word in search_words:
        for page in range(1, num_pages + 1):
            if _deadline_passed(deadline_ts):
                print(f"[info] monsnode deadline at search={word}, page={page}; stop.")
                return all_urls[:RAW_LIMIT]

            if page == 1:
                list_url = f"{BASE_ORIGIN}/search.php?search={word}"
            else:
                # これまで 80 件取れていた仕様に合わせる
                list_url = f"{BASE_ORIGIN}/search.php?search={word}&page={page}&s="

            html = fetch_html_with_playwright(list_url)
            if not html:
                print(f"[warn] monsnode fetch failed: {list_url}")
                continue

            redirect_links = extract_redirect_links_from_list(html)
            print(f"[info] monsnode list {list_url}: found {len(redirect_links)} redirect links")

            for u in redirect_links:
                if u in seen:
                    continue
                seen.add(u)
                all_urls.append(u)
                if len(all_urls) >= RAW_LIMIT:
                    print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                    return all_urls[:RAW_LIMIT]

            # サーバーへの負荷軽減
            time.sleep(0.1)

    return all_urls[:RAW_LIMIT]


# =========================
#   fetch_listing_pages (bot.py 互換)
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,
) -> List[str]:
    """
    旧 goxplorer 互換インターフェイス。
    monsnode 専用。
    - 各 search ワードの search.php?search=WORD (&page=2,3) を開く
    - redirect.php?v=XXXX をまとめて返す
    """
    return _collect_redirect_urls_from_search_pages(
        num_pages=num_pages,
        deadline_ts=deadline_ts,
    )


# =========================
#   collect_fresh_gofile_urls (bot.py から呼ばれるメイン)
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    bot.py から呼び出されるメイン関数。
    ここでは「gofile」ではなく monsnode redirect URL を扱う。

    - monsnode から redirect.php?v=XXXX の URL を集める
    - state.json 由来の already_seen でフィルタ
    - x.gd で短縮
    - want 件だけ返却
    """

    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # 生の redirect URL 一覧
    raw_urls = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 既出除外（元URLが state.json にあればスキップ）
    candidates = [u for u in raw_urls if u not in already_seen][:max(1, FILTER_LIMIT)]

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