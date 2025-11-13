# goxplorer.py — monsnode 専用スクレイパー
import os, re, time
from urllib.parse import urljoin
from datetime import datetime, timezone, timedelta

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
    )
}

SEARCH_WORDS = [
    "992ultra",
    "verycoolav",
    "bestav8",
    "movieszzzz",
    "himitukessya0"
]

# mp4 URL抽出
MP4_RE = re.compile(r"https://video\.twimg\.com/.+?\.mp4[^\s\"']*", re.I)


# =========================
#   X.gd 短縮
# =========================
def shorten_xgd(long_url: str) -> str:
    api_key = os.getenv("XGD_API_KEY", "")
    if not api_key:
        return long_url

    try:
        r = requests.get(
            "https://x.gd/create.php",
            params={"url": long_url, "key": api_key},
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
    except Exception:
        pass
    return long_url


# =========================
#   ページを Playwright で取得
# =========================
def fetch_html(url: str, timeout_ms=15000):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()

            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(800)

            html = page.content()

            ctx.close()
            browser.close()
            return html
    except Exception as e:
        print(f"[warn] fetch_html fail {url}: {e}")
        return None


# =========================
#   詳細ページ → mp4 抽出
# =========================
def extract_mp4_urls_from_detail(url: str) -> list:
    html = fetch_html(url)
    if not html:
        return []
    found = MP4_RE.findall(html)
    return list(set(found))


# =========================
#   一覧ページから detail リンク抽出
# =========================
def extract_detail_links_from_list(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # /v12345 の形式
        if re.match(r"^/v[0-9]+$", href):
            links.append(urljoin(BASE_ORIGIN, href))
    return list(set(links))


# =========================
#   monsnode 収集メイン
# =========================
def collect_fresh_gofile_urls(
    already_seen: set,
    want: int,
    num_pages: int = 3,
    deadline_sec: int = 240
) -> list:

    start_time = time.time()
    collected = []

    for word in SEARCH_WORDS:

        # 各キーワード
        for page in range(1, num_pages + 1):

            elapsed = time.time() - start_time
            if deadline_sec and elapsed > deadline_sec:
                print("[info] deadline reached; stop.")
                return collected

            list_url = f"{BASE_ORIGIN}/search.php?search={word}"
            if page > 1:
                list_url = f"{BASE_ORIGIN}/search.php?search={word}&page={page}&s="

            html = fetch_html(list_url)
            if not html:
                print(f"[warn] monsnode fetch failed: {list_url}")
                continue

            detail_links = extract_detail_links_from_list(html)
            print(f"[info] monsnode list {list_url}: found {len(detail_links)} detail links")

            for durl in detail_links:
                if len(collected) >= want:
                    return collected

                mp4s = extract_mp4_urls_from_detail(durl)
                for mp4 in mp4s:
                    if mp4 in already_seen:
                        continue
                    shorted = shorten_xgd(mp4)
                    collected.append(shorted)

                    if len(collected) >= want:
                        return collected

    return collected