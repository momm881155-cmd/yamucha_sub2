# goxplorer.py — monsnode 特化 + x.gd 短縮 + バッチ処理版
import os
import re
import time
from urllib.parse import urljoin
from typing import List, Set, Optional

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =========================
# 基本設定
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

# 1バッチで処理する redirect.php の件数
REDIRECT_BATCH = int(os.getenv("REDIRECT_BATCH", "10"))

# video.twimg.com の mp4
MP4_RE = re.compile(
    r"https://video\.twimg\.com/[^\s\"']*?\.mp4[^\s\"']*",
    re.I,
)


def _now() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts

# =========================
# monsnode 検索ワード
# =========================

def _monsnode_search_words() -> List[str]:
    """
    検索ワードは環境変数 MONSNODE_SEARCH_TERMS でも上書き可能。
    カンマ or 改行区切り。
    """
    env = os.getenv("MONSNODE_SEARCH_TERMS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        words = [p.strip() for p in parts if p.strip()]
        if words:
            return words

    # デフォルト（あなたが指定していた5つ）
    return [
        "992ultra",
        "verycoolav",
        "bestav8",
        "movieszzzz",
        "himitukessya0",
    ]

# =========================
# x.gd 短縮
# =========================

def shorten_via_xgd(long_url: str) -> str:
    """
    x.gd の API を使って URL を短縮。
    失敗時は元 URL のまま返す。
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
# Playwright 共通
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
# 一覧ページ → redirect.php 抽出
# =========================

def extract_redirect_links_from_list(html: str) -> List[str]:
    """
    検索結果ページから redirect.php?v=... のリンクを抽出。
    """
    if not html:
        print("[debug] extract_redirect_links_from_list: html is empty")
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
# redirect.php ページ → mp4 抽出
# =========================

def extract_mp4_urls_from_redirect_page(page, redirect_url: str) -> List[str]:
    """
    redirect.php?v=... を開いて、中の video.twimg.com の mp4 を抽出。
    1回目で広告サイトに飛ばされる可能性を考慮して、
    最大 2 回同じ URL を開いてみる。
    """
    uniq: List[str] = []
    seen: Set[str] = set()

    for attempt in range(2):  # 最大2回 try
        try:
            page.goto(redirect_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            print(f"[warn] redirect playwright goto failed (attempt {attempt+1}): {redirect_url} ({e})")
            continue

        # JS / ads の読み込み待ち
        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass

        try:
            html = page.content() or ""
        except Exception as e:
            print(f"[warn] redirect page.content failed: {redirect_url} ({e})")
            continue

        found = MP4_RE.findall(html)
        if found:
            for u in found:
                u = u.strip()
                if u and u not in seen:
                    seen.add(u)
                    uniq.append(u)

            # mp4 が1つでも見つかったらそこで終了
            break

    return uniq

# =========================
# monsnode 専用：バッチで redirect.php → mp4 収集
# =========================

def _collect_monsnode_new_urls(
    already_seen: Set[str],
    want: int,
    num_pages: int,
    deadline_ts: Optional[float],
) -> List[str]:
    """
    - search.php から redirect.php?v=... を一覧取得
    - redirect.php を 10件ずつバッチで開き mp4 を探す
    - x.gd で短縮し、state.json 由来の already_seen と重複しないものだけ集める
    - 「新規URL」が want 件に達したら即終了
    """
    results: List[str] = []          # 最終的に bot.py へ返す短縮URL
    seen_redirect: Set[str] = set()  # 同一 redirect.php を2回開かないように
    seen_raw_mp4: Set[str] = set()   # 1 run 内での重複 mp4 除外用

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
                if _deadline_passed(deadline_ts) or len(results) >= want:
                    break

                # 1ページ目と2ページ目以降のURL形式
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

                try:
                    lhtml = page.content() or ""
                except Exception as e:
                    print(f"[warn] monsnode list content failed: {list_url} ({e})")
                    lhtml = ""

                redirect_links = extract_redirect_links_from_list(lhtml)
                print(f"[info] monsnode list {list_url}: found {len(redirect_links)} redirect links")

                # redirect.php をバッチ処理
                batch: List[str] = []
                for rurl in redirect_links:
                    if _deadline_passed(deadline_ts) or len(results) >= want:
                        break
                    if rurl in seen_redirect:
                        continue
                    seen_redirect.add(rurl)
                    batch.append(rurl)

                    if len(batch) >= REDIRECT_BATCH:
                        _process_redirect_batch(
                            page=page,
                            redirect_urls=batch,
                            already_seen=already_seen,
                            seen_raw_mp4=seen_raw_mp4,
                            results=results,
                            deadline_ts=deadline_ts,
                            want=want,
                        )
                        batch = []
                        if _deadline_passed(deadline_ts) or len(results) >= want:
                            break

                # 最後にバッチに残った分
                if batch and (not _deadline_passed(deadline_ts)) and len(results) < want:
                    _process_redirect_batch(
                        page=page,
                        redirect_urls=batch,
                        already_seen=already_seen,
                        seen_raw_mp4=seen_raw_mp4,
                        results=results,
                        deadline_ts=deadline_ts,
                        want=want,
                    )

            if len(results) >= want or _deadline_passed(deadline_ts):
                break

        ctx.close()

    return results

def _process_redirect_batch(
    page,
    redirect_urls: List[str],
    already_seen: Set[str],
    seen_raw_mp4: Set[str],
    results: List[str],
    deadline_ts: Optional[float],
    want: int,
):
    """
    redirect.php をまとめて処理するバッチ。
    - 各 redirect.php から mp4 を抽出
    - raw mp4 / 短縮URL ともに state 上の既出と重複しないものだけ results に追加
    - results が want 件に達したら即終了
    """
    for rurl in redirect_urls:
        if _deadline_passed(deadline_ts) or len(results) >= want:
            break

        mp4s = extract_mp4_urls_from_redirect_page(page, rurl)
        if mp4s:
            print(f"[debug] mp4 candidates from {rurl}: {len(mp4s)}")

        for raw in mp4s:
            if raw in seen_raw_mp4:
                continue
            seen_raw_mp4.add(raw)

            # まず生URLで既出チェック
            if raw in already_seen:
                continue

            short = shorten_via_xgd(raw)

            # 短縮URL側でも既出チェック
            if short in already_seen:
                continue
            if short in results:
                continue

            results.append(short)
            print(f"[info] mp4 accepted: {short}")

            if len(results) >= want:
                break

# =========================
# bot.py 互換インターフェース
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None
) -> List[str]:
    """
    旧 goxplorer の互換用ダミー。
    今回は使わず、collect_fresh_gofile_urls から直接
    _collect_monsnode_new_urls を呼ぶ。
    残しておくだけ。
    """
    return []

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None
) -> List[str]:
    """
    bot.py から呼ばれるメイン関数。
    - monsnode から redirect.php を 10件ずつ取得
    - 各 redirect.php から mp4 を抽出
    - state.json 由来 already_seen で重複除外
    - x.gd 短縮してから、重複なしの新規URLを want 件返す
    """
    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    # デッドラインが設定されていない場合は一応 10 分にしておく
    if deadline_sec is None or deadline_sec <= 0:
        deadline_sec = 600

    deadline_ts = _now() + deadline_sec if deadline_sec else None
    want = max(1, want)

    urls = _collect_monsnode_new_urls(
        already_seen=already_seen,
        want=want,
        num_pages=num_pages,
        deadline_ts=deadline_ts,
    )

    return urls[:want]