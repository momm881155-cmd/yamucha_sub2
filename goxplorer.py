# goxplorer.py — monsnode + x.gd 専用版（bot.py 互換）

import os
import re
import time
from typing import List, Set, Optional

import requests
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
    "Referer": "https://monsnode.com",
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))

# =========================
#   検索ワード（5サイト相当）
# =========================

def _monsnode_search_words() -> List[str]:
    """
    環境変数 MONSNODE_SEARCH_TERMS で差し替え・追加可能。
    空ならデフォルト5ワードを使う。
    """
    env = os.getenv("MONSNODE_SEARCH_TERMS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        words = [p.strip() for p in parts if p.strip()]
        if words:
            return words

    # デフォルト（ご指定の5つ）
    return [
        "992ultra",
        "verycoolav",
        "bestav8",
        "movieszzzz",
        "himitukessya0",
    ]


# =========================
#   共通 util
# =========================

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


# =========================
#   詳細ページから mp4 抽出
# =========================

def _extract_mp4_from_redirect_url(redirect_url: str) -> List[str]:
    """
    redirect.php?v=xxxxx のページから video.twimg.com の mp4 を抜く。
    まず requests で取りに行き、ダメなら Playwright で再トライ。
    """
    html = None

    # 1) requests で素直に取りに行く
    try:
        r = requests.get(redirect_url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            html = r.text
        else:
            print(f"[warn] redirect requests status {r.status_code}: {redirect_url}")
    except Exception as e:
        print(f"[warn] redirect requests failed: {redirect_url} ({e})")

    # 2) 取れなかったら Playwright で再チャレンジ
    if not html:
        try:
            with sync_playwright() as pw:
                ctx = _playwright_ctx(pw)
                page = ctx.new_page()
                page.set_extra_http_headers({
                    "Accept": HEADERS["Accept"],
                    "Accept-Language": HEADERS["Accept-Language"],
                    "Referer": BASE_ORIGIN,
                    "Connection": HEADERS["Connection"],
                })
                page.goto(redirect_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(500)
                html = page.content()
                ctx.close()
        except Exception as e:
            print(f"[warn] redirect playwright failed: {redirect_url} ({e})")
            html = None

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
#   monsnode 一覧ページ → redirect.php リンク抽出
# =========================

def _collect_monsnode_mp4_urls(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    monsnode の search 結果から:
      1) 一覧ページで redirect.php?v=... のリンクを Playwright で拾う
      2) 各 redirect.php ページから mp4 URL を抜く
    生の mp4 URL リストを返す。
    """
    all_mp4: List[str] = []
    seen_mp4: Set[str] = set()
    seen_redirect: Set[str] = set()

    search_words = _monsnode_search_words()

    with sync_playwright() as pw:
        ctx = _playwright_ctx(pw)
        page = ctx.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": BASE_ORIGIN,
            "Connection": HEADERS["Connection"],
        })

        for word in search_words:
            for p in range(1, num_pages + 1):
                if _deadline_passed(deadline_ts):
                    print(f"[info] monsnode deadline at search={word}, page={p}; stop.")
                    ctx.close()
                    return all_mp4[:RAW_LIMIT]

                # 1ページ目と2ページ目以降でURLが違う仕様に対応
                if p == 1:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}"
                else:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}&page={p}&s="

                try:
                    page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[warn] monsnode list goto failed: {list_url} ({e})")
                    continue

                # 遅延ロード対策で軽くスクロール
                try:
                    for _ in range(6):
                        page.mouse.wheel(0, 1600)
                        page.wait_for_timeout(200)
                except Exception:
                    pass

                # Playwright の JS で redirect.php?v= を含む href を全部回収
                try:
                    redirect_links = page.eval_on_selector_all(
                        "a[href*='redirect.php?v=']",
                        "els => Array.from(new Set(els.map(e => e.href)))"
                    ) or []
                except Exception as e:
                    print(f"[warn] eval_on_selector_all failed at {list_url}: {e}")
                    redirect_links = []

                # 結果をデバッグログ
                print(f"[info] monsnode list {list_url}: found {len(redirect_links)} redirect links")

                # 各 redirect.php から mp4 抽出
                for rurl in redirect_links:
                    if _deadline_passed(deadline_ts):
                        print("[info] monsnode deadline during redirect loop; stop.")
                        ctx.close()
                        return all_mp4[:RAW_LIMIT]

                    if not isinstance(rurl, str):
                        continue
                    rurl = rurl.strip()
                    if not rurl or rurl in seen_redirect:
                        continue
                    seen_redirect.add(rurl)

                    mp4s = _extract_mp4_from_redirect_url(rurl)
                    for m in mp4s:
                        if m not in seen_mp4:
                            seen_mp4.add(m)
                            all_mp4.append(m)
                            if len(all_mp4) >= RAW_LIMIT:
                                print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                                ctx.close()
                                return all_mp4[:RAW_LIMIT]

                time.sleep(0.1)

        ctx.close()

    return all_mp4[:RAW_LIMIT]


# =========================
#   fetch_listing_pages (bot.py 互換)
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None
) -> List[str]:
    """
    旧 goxplorer と同じインターフェイス。
    monsnode 専用で、生の mp4 URL リストを返す。
    """
    return _collect_monsnode_mp4_urls(num_pages=num_pages, deadline_ts=deadline_ts)


# =========================
#   collect_fresh_gofile_urls (bot.py から呼ばれる)
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None
) -> List[str]:
    """
    bot.py 側から呼び出されるメイン関数。

    流れ:
      1) fetch_listing_pages で monsnode から mp4 URL を一覧取得
      2) state.json の posted_urls / recent_urls_24h 由来の already_seen でフィルタ
      3) x.gd で短縮
      4) 短縮後 URL も already_seen にあったら重複とみなして捨てる
      5) WANT_POST 件だけ返す
    """

    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # 1) 生 mp4 一覧
    raw_mp4 = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 2) 元URL時点で既出除外
    candidates = [u for u in raw_mp4 if u not in already_seen][:max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break
        if url in seen_now:
            continue

        # 3) x.gd で短縮
        short = shorten_via_xgd(url)

        # 4) 短縮後も state.json にあればスキップ（過去に使った短縮URL）
        if short in already_seen:
            continue

        seen_now.add(url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]