# goxplorer.py — monsnode 専用 + x.gd 短縮（bot.py 互換・deadline 無し版）

import os
import re
import time
from urllib.parse import urljoin
from typing import List, Set, Optional

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
        "Chrome/123.0.0.0 Safari/123.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://monsnode.com",
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))


def _monsnode_search_words() -> List[str]:
    """
    検索ワードを返す。
    環境変数 MONSNODE_SEARCH_TERMS があればそれを優先（カンマ or 改行区切り）。
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


# video.twimg.com の .mp4
MP4_RE = re.compile(
    r"https://video\.twimg\.com/[^\s\"']*?\.mp4[^\s\"']*",
    re.I,
)


def _now() -> float:
    return time.monotonic()


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
#   一覧ページ → redirect.php 抽出
# =========================

def extract_redirect_links_from_list(html: str) -> List[str]:
    """
    検索結果ページのサムネイル一覧から
    https://monsnode.com/redirect.php?v=...... のリンクを拾う。
    """
    if not html:
        print("[debug] extract_redirect_links_from_list: empty html")
        return []

    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "redirect.php?v=" not in href:
            continue
        full = urljoin(BASE_ORIGIN, href)
        if full not in seen:
            seen.add(full)
            links.append(full)

    print(f"[debug] extract_redirect_links_from_list: {len(links)} links")
    return links


# =========================
#   redirect.php → mp4 抽出
# =========================

def resolve_redirect_to_mp4(page, redirect_url: str, max_attempts: int = 3) -> List[str]:
    """
    redirect.php?v=... をブラウザで開いて、
    - 1回目は広告に飛ばされる可能性が高いので、複数回トライ
    - 最終URL、ページHTML の両方から video.twimg.com の .mp4 を探す
    """
    collected: List[str] = []
    seen: Set[str] = set()

    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(redirect_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            print(f"[warn] redirect playwright goto failed (try={attempt}): {redirect_url} ({e})")
            continue

        # 軽く待つ（広告のリダイレクトなど）
        try:
            page.wait_for_timeout(800)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
        except Exception:
            pass

        final_url = page.url or ""
        if "video.twimg.com" in final_url and ".mp4" in final_url:
            u = final_url.strip()
            if u not in seen:
                seen.add(u)
                collected.append(u)

        html = ""
        try:
            html = page.content() or ""
        except Exception:
            html = ""

        if html:
            for m in MP4_RE.findall(html):
                u = m.strip()
                if u and u not in seen:
                    seen.add(u)
                    collected.append(u)

        if collected:
            break  # この redirect はもうOK

    return collected


# =========================
#   monsnode 専用収集（deadline なし）
# =========================

def _collect_monsnode_mp4(num_pages: int) -> List[str]:
    """
    monsnode の search 結果から mp4 URL を収集。
    - search.php?search=WORD (&page=2&s= など) を開く
    - redirect.php?v=... を一覧から全取得
    - 各 redirect.php から video.twimg.com の mp4 を抜く
    """
    all_mp4: List[str] = []
    seen_mp4: Set[str] = set()

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
                # RAW_LIMIT に達したら全体終了
                if len(all_mp4) >= RAW_LIMIT:
                    print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                    ctx.close()
                    return all_mp4[:RAW_LIMIT]

                # 1ページ目と2ページ目以降でURL構造が違う
                if p == 1:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}"
                else:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}&page={p}&s="

                try:
                    page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[warn] monsnode list goto failed: {list_url} ({e})")
                    continue

                # サムネイルが遅延ロードされる可能性があるので軽くスクロール
                try:
                    for _ in range(4):
                        page.mouse.wheel(0, 1600)
                        page.wait_for_timeout(200)
                except Exception:
                    pass

                try:
                    html = page.content() or ""
                except Exception:
                    html = ""

                redirect_links = extract_redirect_links_from_list(html)
                print(f"[info] monsnode list {list_url}: found {len(redirect_links)} redirect links")

                # 各 redirect から mp4 を抜く
                for rurl in redirect_links:
                    if len(all_mp4) >= RAW_LIMIT:
                        print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                        ctx.close()
                        return all_mp4[:RAW_LIMIT]

                    mp4s = resolve_redirect_to_mp4(page, rurl, max_attempts=3)
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
#   fetch_listing_pages (互換)
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,  # 互換のため引数だけ残すが使わない
) -> List[str]:
    """
    旧 goxplorer のインターフェイス互換。
    monsnode 専用で mp4 URL リストを返す。
    """
    _ = deadline_ts  # unused
    return _collect_monsnode_mp4(num_pages=num_pages)


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
    bot.py から呼び出されるメイン関数（インターフェイスはそのまま）。
    - monsnode から mp4 URL を集める
    - state.json 由来の already_seen でフィルタ
    - x.gd で短縮
    - WANT_POST 件だけ返却
    """

    # deadline は互換のため受け取るが、monsnode では使わない
    _ = deadline_sec  # unused

    # 生 mp4 URL 一覧
    raw_mp4 = fetch_listing_pages(num_pages=num_pages, deadline_ts=None)

    # 既出除外（元URLベース）
    candidates = [u for u in raw_mp4 if u not in already_seen][:max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if url in seen_now:
            continue

        short = shorten_via_xgd(url)

        # 短縮後URLも既に使っているならスキップ
        if short in already_seen or short in results:
            continue

        seen_now.add(url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]