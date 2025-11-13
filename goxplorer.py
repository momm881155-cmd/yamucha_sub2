# goxplorer.py — monsnode + x.gd 専用版（gofile なし）

import os, re, time
from urllib.parse import urlparse, urljoin
from typing import List, Set, Optional

import requests
from bs4 import BeautifulSoup

# ====== 設定値 ======
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://monsnode.com",
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))  # 生で集める最大件数
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))  # state などでフィルタした後の上限

def _now() -> float:
    return time.monotonic()

def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts

# ====== x.gd 短縮 ======
def shorten_via_xgd(long_url: str) -> str:
    """x.gd の API を使って URL を短縮する。失敗時は元 URL をそのまま返す。"""
    api_key = os.getenv("XGD_API_KEY", "").strip()
    if not api_key:
        return long_url
    try:
        r = requests.get(
            "https://xgd.io/V1/shorten",
            params={"url": long_url, "key": api_key},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        short = (data.get("shorturl") or data.get("short_url") or "").strip()
        return short or long_url
    except Exception as e:
        print(f"[warn] x.gd shorten failed for {long_url}: {e}")
        return long_url

# ====== monsnode 検索URL ======
def _monsnode_search_urls() -> List[str]:
    """
    monsnode の検索URL群。
    環境変数 MONSNODE_SEARCH_URLS で上書き可能（カンマ or 改行区切り）。
    """
    env = os.getenv("MONSNODE_SEARCH_URLS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    # デフォルト（ご指定の5本）
    return [
        "https://monsnode.com/search.php?search=992ultra",
        "https://monsnode.com/search.php?search=verycoolav",
        "https://monsnode.com/search.php?search=bestav8",
        "https://monsnode.com/search.php?search=movieszzzz",
        "https://monsnode.com/search.php?search=himitukessya0",
    ]

# ====== monsnode 専用：検索結果から /v... の動画ページを集める ======
def _collect_monsnode_urls(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    monsnode の search 結果から「動画ページURL (https://monsnode.com/v...)」を収集する。
    ※ .mp4 直リンクは monsnode HTML に出てこないため、/v... ページを投稿用URLとする。
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)

    all_urls: List[str] = []
    seen: Set[str] = set()

    search_bases = _monsnode_search_urls()

    for base in search_bases:
        for p in range(0, num_pages):
            if _deadline_passed(deadline_ts):
                print(f"[info] monsnode deadline at page {p}; stop.")
                return all_urls[:RAW_LIMIT]

            # p=0 はそのまま / p>=1 は ?page=N&s=
            if p == 0:
                url = base
            else:
                sep = "&" if "?" in base else "?"
                url = f"{base}{sep}page={p}&s="

            try:
                r = sess.get(url, timeout=10)
                r.raise_for_status()
                html = r.text
            except Exception as e:
                print(f"[warn] monsnode fetch failed: {url} ({e})")
                break  # この検索ワードは打ち切り

            soup = BeautifulSoup(html or "", "html.parser")
            added = 0
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith("#") or href.startswith("javascript:"):
                    continue

                full = urljoin(base, href)
                pr = urlparse(full)

                # monsnode ドメインのみ対象
                if "monsnode.com" not in (pr.netloc or ""):
                    continue

                # /v1234567890 の形式だけ採用
                if not re.match(r"^/v[0-9]+$", pr.path):
                    continue

                if full not in seen:
                    seen.add(full)
                    all_urls.append(full)
                    added += 1
                    if len(all_urls) >= RAW_LIMIT:
                        print(f"[info] monsnode early stop at RAW_LIMIT={RAW_LIMIT}")
                        return all_urls[:RAW_LIMIT]

            print(f"[info] monsnode list {url}: +{added} detail urls (total {len(all_urls)})")
            time.sleep(0.1)

    return all_urls[:RAW_LIMIT]

# ====== 収集エントリ（bot.py からまずここが呼ばれるイメージ） ======
def fetch_listing_pages(num_pages: int = 100, deadline_ts: Optional[float] = None) -> List[str]:
    # 今回は monsnode 専用設計
    return _collect_monsnode_urls(num_pages=num_pages, deadline_ts=deadline_ts)

# ====== フィルタ・返却（bot.py から直接呼ばれる関数） ======
def collect_fresh_gofile_urls(
    already_seen: Set[str], want: int = 3, num_pages: int = 100, deadline_sec: Optional[int] = None
) -> List[str]:
    """
    bot.py から呼ばれるメイン関数。
    - monsnode の /v... ページURLを集める
    - state.json で既出URLを除外
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

    # monsnode から /v... を集める
    raw = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # state.json にあるURL（短縮後 URL も含む）は除外
    candidates = [u for u in raw if u not in already_seen][:max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break
        if url in seen_now:
            continue

        # ★ ここで x.gd で短縮
        short = shorten_via_xgd(url)

        # すでに投稿済みの短縮URLもスキップ
        if short in already_seen:
            continue

        seen_now.add(url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]
