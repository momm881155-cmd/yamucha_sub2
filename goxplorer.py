# goxplorer.py — monsnode 専用 + x.gd 短縮版（bot.py 互換）

import os
import re
import time
from typing import List, Set, Optional

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =========================
#   共通設定
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

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))   # 生の mp4 を最大何件まで集めるか
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))  # state.json でフィルタした後の候補上限

# monsnode 検索ワード（環境変数で増減も可能）
def _monsnode_search_words() -> List[str]:
    env = os.getenv("MONSNODE_SEARCH_TERMS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        words = [p.strip() for p in parts if p.strip()]
        if words:
            return words

    # デフォルト（あなたが使ってる 5つ）
    return [
        "992ultra",
        "verycoolav",
        "bestav8",
        "movieszzzz",
        "himitukessya0",
    ]


# video.twimg.com の mp4 抽出用
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
#   一覧 -> redirect.php 抽出
# =========================

def _extract_redirect_links_from_list(page_html: str) -> List[str]:
    """
    検索結果ページの HTML から redirect.php?v=... を全部抜く。
    """
    if not page_html:
        return []

    soup = BeautifulSoup(page_html, "html.parser")
    links: List[str] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "redirect.php?v=" not in href:
            continue

        # 絶対URLにしなくても href が完全URLならそのまま / 相対なら BASE_ORIGIN を前に付けてもOK
        if href.startswith("http"):
            full = href
        else:
            if not href.startswith("/"):
                href = "/" + href
            full = BASE_ORIGIN + href

        if full not in seen:
            seen.add(full)
            links.append(full)

    print(f"[debug] extract_redirect_links_from_list: {len(links)} links")
    return links


# =========================
#   redirect.php -> mp4 抽出
# =========================

def _extract_mp4_from_html(html: str) -> List[str]:
    """
    redirect.php の中から video.twimg.com の .mp4 を全部抜き出す。
    """
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

def _collect_monsnode_mp4(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    monsnode の search 結果から：
      1) redirect.php?v=... を一覧ページから取得
      2) 各 redirect.php を新しいタブで開く
      3) その HTML から .mp4 を抜き出す
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
            "Referer": BASE_ORIGIN,
            "Connection": HEADERS["Connection"],
        })

        for word in search_words:
            for p in range(1, num_pages + 1):
                if _deadline_passed(deadline_ts):
                    print(f"[info] monsnode deadline at search={word}, page={p}; stop.")
                    ctx.close()
                    return all_mp4[:RAW_LIMIT]

                # 1ページ目と2ページ目以降の URL 仕様
                if p == 1:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}"
                else:
                    list_url = f"{BASE_ORIGIN}/search.php?search={word}&page={p}&s="

                try:
                    page.goto(list_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[warn] monsnode list goto failed: {list_url} ({e})")
                    continue

                # 軽くスクロール（遅延ロード対策）
                try:
                    for _ in range(4):
                        page.mouse.wheel(0, 1400)
                        page.wait_for_timeout(200)
                except Exception:
                    pass

                list_html = page.content() or ""
                redirect_urls = _extract_redirect_links_from_list(list_html)
                print(f"[info] monsnode list {list_url}: found {len(redirect_urls)} redirect links")

                # redirect.php を順番に開いて mp4 を抜く
                for red_url in redirect_urls:
                    if _deadline_passed(deadline_ts):
                        print("[info] monsnode deadline during redirect; stop.")
                        ctx.close()
                        return all_mp4[:RAW_LIMIT]

                    # 新しいタブで開く（広告ごと全部読み込ませるイメージ）
                    dpage = ctx.new_page()
                    try:
                        dpage.goto(red_url, wait_until="domcontentloaded", timeout=20000)
                    except Exception as e:
                        print(f"[warn] redirect playwright goto failed: {red_url} ({e})")
                        dpage.close()
                        continue

                    # 少し待つ（広告やスクリプトの読み込み待ち）
                    dpage.wait_for_timeout(3000)

                    try:
                        html = dpage.content() or ""
                    except Exception:
                        html = ""
                    dpage.close()

                    mp4s = _extract_mp4_from_html(html)
                    if mp4s:
                        print(f"[debug] mp4 candidates from {red_url}: {len(mp4s)}")

                    for m in mp4s:
                        if m not in seen_mp4:
                            seen_mp4.add(m)
                            all_mp4.append(m)
                            print(f"[info] mp4 found: {m}")
                            if len(all_mp4) >= RAW_LIMIT:
                                print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                                ctx.close()
                                return all_mp4[:RAW_LIMIT]

                # ちょっとだけクールダウン
                time.sleep(0.1)

        ctx.close()

    return all_mp4[:RAW_LIMIT]


# =========================
#   bot.py から呼ばれる互換関数
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None
) -> List[str]:
    """
    旧 goxplorer とインターフェイス互換。
    monsnode 専用で「生の mp4 URL リスト」を返す。
    """
    return _collect_monsnode_mp4(num_pages=num_pages, deadline_ts=deadline_ts)


def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None
) -> List[str]:
    """
    bot.py から呼ばれるメイン関数。
    - monsnode から mp4 URL を集める
    - state.json 由来の already_seen でフィルタ
    - x.gd で短縮
    - WANT_POST 件だけ返却
    """

    # 締切（秒）→ 時刻
    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # 生の mp4 一覧
    raw_mp4 = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # state.json の既出URLを除外（元URLベース）
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

        # 短縮後URLも既に state.json にあればスキップ
        if short in already_seen:
            continue

        seen_now.add(url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]