# goxplorer.py — monsnode redirect 専用版（bot.py 互換 / mp4 は追わない）

import os
import re
import time
from typing import List, Set, Optional

import requests
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
    "Referer": BASE_ORIGIN,
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))  # 1回の最大生URL数
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))


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

    # デフォルト 5サイト相当の語
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


# =========================
#   検索ページ → redirect.php リンク抽出
# =========================

def _collect_redirect_urls_from_search_pages(
    num_pages_hint: int,
    deadline_ts: Optional[float],
) -> List[str]:
    """
    monsnode の search.php?search=WORD ページを Playwright で開き、
    jscroll による追い読みを含めてスクロールしながら

        <a href="https://monsnode.com/redirect.php?v=XXXXX">

    を全部拾う。
    992ultra だけでなく verycoolav / bestav8 なども同じ処理で取る。
    """
    all_urls: List[str] = []
    seen: Set[str] = set()

    search_words = _monsnode_search_words()

    # num_pages_hint は「どれくらいスクロールするか」の目安にだけ使う
    max_scrolls_per_word = max(6, num_pages_hint * 6)

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
            if _deadline_passed(deadline_ts):
                print(f"[info] monsnode deadline before search={word}; stop.")
                break

            search_url = f"{BASE_ORIGIN}/search.php?search={word}"
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                print(f"[warn] monsnode search load failed: {search_url} ({e})")
                continue

            # 可能なら「Latest」に切り替え（ボタンが無ければ無視）
            try:
                page.click("a.sort-btn[href*='s=n']", timeout=3000)
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # jscroll での遅延読み込み用にスクロールしまくる
            prev_h = 0
            for i in range(max_scrolls_per_word):
                if _deadline_passed(deadline_ts):
                    break
                try:
                    page.mouse.wheel(0, 1800)
                except Exception:
                    pass
                page.wait_for_timeout(300)
                try:
                    h = page.evaluate("() => document.body.scrollHeight") or 0
                except Exception:
                    h = 0
                if h == prev_h:
                    # もう増えなければちょっと待って二度確認
                    page.wait_for_timeout(400)
                    try:
                        h2 = page.evaluate("() => document.body.scrollHeight") or 0
                    except Exception:
                        h2 = 0
                    if h2 == prev_h:
                        break
                    prev_h = h2
                else:
                    prev_h = h

            # すべて読み込んだ後、redirect.php?v= を全部拾う
            try:
                hrefs = page.eval_on_selector_all(
                    'a[href*="redirect.php?v="]',
                    'els => els.map(a => a.href)',
                ) or []
            except Exception:
                hrefs = []

            print(f"[info] monsnode search={word}: found {len(hrefs)} redirect links")

            for raw in hrefs:
                if not raw:
                    continue
                url = raw.strip()
                if not url:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                all_urls.append(url)
                if len(all_urls) >= RAW_LIMIT:
                    print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                    ctx.close()
                    return all_urls[:RAW_LIMIT]

        ctx.close()

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
    - 各 search ワードの検索ページを開く
    - 無限スクロールで一覧を全部読み込む
    - redirect.php?v=XXXX の URL をまとめて返す
    """
    return _collect_redirect_urls_from_search_pages(
        num_pages_hint=num_pages,
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