# goxplorer.py — monsnode 専用 + x.gd 短縮（bot.py 互換・mp4 5本で即切り上げ）

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

# 検索ワード（環境変数 MONSNODE_SEARCH_TERMS で上書き可能）
def _monsnode_search_words() -> List[str]:
    env = os.getenv("MONSNODE_SEARCH_TERMS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        words = [p.strip() for p in parts if p.strip()]
        if words:
            return words

    # デフォルト 5 本
    return [
        "992ultra",
        "verycoolav",
        "bestav8",
        "movieszzzz",
        "himitukessya0",
    ]


# video.twimg.com の mp4 検出用（保険で使う）
MP4_RE = re.compile(
    r"https://video\.twimg\.com/[^\s\"']*?\.mp4[^\s\"']*",
    re.I,
)


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

def extract_mp4_from_html(html: str) -> List[str]:
    """
    redirect.php の HTML から video.twimg.com の .mp4 URL を抜き出す。
    aタグの href と、保険で生のテキストも見る。
    """
    if not html:
        return []

    found: List[str] = []
    seen: Set[str] = set()

    soup = BeautifulSoup(html, "html.parser")

    # 1) aタグの href から探す（あなたが貼ってくれたパターン）
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "video.twimg.com" in href and ".mp4" in href:
            u = href
            if u not in seen:
                seen.add(u)
                found.append(u)

    # 2) 念のため HTML 全体からも正規表現で拾う（保険）
    if "video.twimg.com" in html:
        for m in MP4_RE.findall(html):
            u = m.strip()
            if u and u not in seen:
                seen.add(u)
                found.append(u)

    return found


def resolve_redirect_to_mp4(ctx, redirect_url: str, max_attempts: int = 3) -> List[str]:
    """
    redirect.php?v=... を開いて mp4 を探す。
    - 1回目は広告に飛ばされることを想定して、同じURLに最大 max_attempts 回トライ
    - タブを閉じて開き直す挙動を new_page() / close() で再現
    """
    collected: List[str] = []
    seen: Set[str] = set()

    for attempt in range(1, max_attempts + 1):
        page = ctx.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": HEADERS["Referer"],
            "Connection": HEADERS["Connection"],
        })
        try:
            page.goto(redirect_url, wait_until="domcontentloaded", timeout=20000)
            # 少し待ってから networkidle も試す
            try:
                page.wait_for_timeout(800)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
            except Exception:
                pass

            try:
                html = page.content() or ""
            except Exception:
                html = ""

        except Exception as e:
            print(f"[warn] redirect playwright goto failed (try={attempt}): {redirect_url} ({e})")
            page.close()
            continue

        finally:
            page.close()

        if not html:
            continue

        # 実際に mp4 を探す
        mp4s = extract_mp4_from_html(html)
        for u in mp4s:
            if u not in seen:
                seen.add(u)
                collected.append(u)

        if collected:
            # この redirect からはもう十分取れたので終了
            break

    return collected


# =========================
#   monsnode 専用収集
# =========================

def _collect_monsnode_mp4(num_pages: int, want: int) -> List[str]:
    """
    monsnode の search 結果から mp4 URL を収集。
    - search.php?search=WORD (&page=2&s=...) を開く
    - redirect.php?v=... を一覧から取得
    - 各 redirect.php から video.twimg.com の .mp4 を抜く
    - mp4 が "target" 本集まったら即終了
    """
    all_mp4: List[str] = []
    seen_mp4: Set[str] = set()

    # 重複などを考えて、少し余裕を持った目標値
    target = max(want * 3, want)
    if target > RAW_LIMIT:
        target = RAW_LIMIT

    search_words = _monsnode_search_words()

    with sync_playwright() as pw:
        ctx = _playwright_ctx(pw)

        # 一覧ページ用タブ
        list_page = ctx.new_page()
        list_page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": HEADERS["Referer"],
            "Connection": HEADERS["Connection"],
        })

        for word in search_words:
            for p in range(1, num_pages + 1):
                if len(all_mp4) >= target:
                    print(f"[info] monsnode early stop: reached target={target}")
                    ctx.close()
                    return all_mp4[:target]

                # 1ページ目とそれ以降で URL が違う仕様
                if p == 1:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}"
                else:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}&page={p}&s="

                try:
                    list_page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[warn] monsnode list goto failed: {list_url} ({e})")
                    continue

                # サムネイルの遅延ロード対策で軽くスクロール
                try:
                    for _ in range(4):
                        list_page.mouse.wheel(0, 1600)
                        list_page.wait_for_timeout(200)
                except Exception:
                    pass

                try:
                    html = list_page.content() or ""
                except Exception:
                    html = ""

                redirect_links = extract_redirect_links_from_list(html)
                print(f"[info] monsnode list {list_url}: found {len(redirect_links)} redirect links")

                for rurl in redirect_links:
                    if len(all_mp4) >= target:
                        print(f"[info] monsnode early stop inside redirect loop: target={target}")
                        ctx.close()
                        return all_mp4[:target]

                    mp4s = resolve_redirect_to_mp4(ctx, rurl, max_attempts=3)
                    for m in mp4s:
                        if m not in seen_mp4:
                            seen_mp4.add(m)
                            all_mp4.append(m)
                            if len(all_mp4) >= target:
                                print(f"[info] monsnode early stop at target={target}")
                                ctx.close()
                                return all_mp4[:target]

                time.sleep(0.1)

        ctx.close()

    return all_mp4[:target]


# =========================
#   fetch_listing_pages (互換)
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,  # 互換のため残すが使わない
) -> List[str]:
    """
    旧 goxplorer のインターフェイス用。
    通常は collect_fresh_gofile_urls から直接 _collect_monsnode_mp4 を呼ぶ。
    """
    _ = deadline_ts
    return _collect_monsnode_mp4(num_pages=num_pages, want=10)


# =========================
#   collect_fresh_gofile_urls (bot.py メイン入口)
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    bot.py から呼び出されるメイン関数。
    - monsnode から mp4 URL を集める（"want" 本×少し余裕ぶん）
    - state.json 由来の already_seen でフィルタ
    - x.gd で短縮
    - WANT_POST 件だけ返却
    """

    _ = deadline_sec  # 今回は使わない（monsnode専用）

    raw_mp4 = _collect_monsnode_mp4(num_pages=num_pages, want=want)

    # 既出除外（元URLベース）
    candidates = [u for u in raw_mp4 if u not in already_seen][:max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if url in seen_now:
            continue

        short = shorten_via_xgd(url)

        # 短縮後 URL も既に使っているならスキップ
        if short in already_seen or short in results:
            continue

        seen_now.add(url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]