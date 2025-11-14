# goxplorer.py — tktube 専用：
# categories 一覧から data-preview の mp4 を集めて、
# そこから embed URL (https://tktube.com/ja/embed/<id>) を作り、
# それを x.gd で短縮して返す版

import os
import re
import time
from typing import List, Set, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# =========================
#   設定
# =========================

BASE_ORIGIN = os.getenv("BASE_ORIGIN", "https://tktube.com").rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://tktube.com",
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))


def _tktube_category_urls() -> List[str]:
    """
    収集対象のカテゴリ一覧URLテンプレートを返す。
    TKTUBE_CATEGORY_URLS 環境変数があればそれを優先（改行 or カンマ区切り）。
    {page} プレースホルダを 1..NUM_PAGES で埋める想定。
    """
    env = os.getenv("TKTUBE_CATEGORY_URLS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    # デフォルト：fc2 カテゴリ
    return [
        "https://tktube.com/ja/categories/fc2/?page={page}",
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
#   mp4 → embed 変換
# =========================

# .../357435/357435_preview.mp4 の 357435 を抜くパターン
EMBED_ID_RE = re.compile(r"/(\d+)_preview\.mp4", re.I)


def preview_to_embed(preview_url: str) -> Optional[str]:
    """
    例:
      https://pv.tkcdns.com/tk59/357000/357435/357435_preview.mp4
        -> https://tktube.com/ja/embed/357435
    """
    if not preview_url:
        return None

    u = preview_url.strip()

    m = EMBED_ID_RE.search(u)
    if m:
        vid = m.group(1)
    else:
        # 念のためフォールバック（/357435/xxx.mp4 みたいな形）
        m2 = re.search(r"/(\d+)/[^/]*\.mp4", u)
        if not m2:
            return None
        vid = m2.group(1)

    base = BASE_ORIGIN or "https://tktube.com"
    return f"{base}/ja/embed/{vid}"


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
#   一覧ページ → data-preview (.mp4) 抽出
# =========================

def extract_preview_mp4_from_list(html: str) -> List[str]:
    """
    カテゴリ一覧ページの HTML から、
    <img data-preview="...mp4"> の URL を全部抜く。
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    seen: Set[str] = set()

    for img in soup.find_all("img"):
        preview = img.get("data-preview")
        if not preview:
            continue

        full = urljoin(BASE_ORIGIN, preview.strip())
        full = _normalize_url(full)

        if ".mp4" not in full:
            continue

        if full not in seen:
            seen.add(full)
            links.append(full)

    print(f"[debug] extract_preview_mp4_from_list: {len(links)} links")
    return links


def _collect_tktube_preview_urls(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    複数カテゴリURL × page=1..num_pages を回して、
    data-preview の mp4 URL を集める。
    """
    all_urls: List[str] = []
    seen: Set[str] = set()
    category_templates = _tktube_category_urls()

    for tmpl in category_templates:
        for page in range(1, num_pages + 1):
            if _deadline_passed(deadline_ts):
                print(f"[info] tktube deadline at {tmpl}, page={page}; stop.")
                return all_urls[:RAW_LIMIT]

            list_url = tmpl.format(page=page)
            try:
                resp = requests.get(list_url, headers=HEADERS, timeout=20)
            except Exception as e:
                print(f"[warn] tktube request failed: {list_url} ({e})")
                continue

            if resp.status_code != 200:
                print(f"[warn] tktube status {resp.status_code}: {list_url}")
                continue

            html = resp.text
            links = extract_preview_mp4_from_list(html)
            print(f"[info] tktube list {list_url}: found {len(links)} preview links")

            for u in links:
                if u in seen:
                    continue
                seen.add(u)
                all_urls.append(u)
                if len(all_urls) >= RAW_LIMIT:
                    print(f"[info] tktube early stop at RAW_LIMIT={RAW_LIMIT}")
                    return all_urls[:RAW_LIMIT]

            time.sleep(0.2)

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
    まずは data-preview mp4 URL リストを返す。
    """
    return _collect_tktube_preview_urls(num_pages=num_pages, deadline_ts=deadline_ts)


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
    - tktube から data-preview の mp4 URL を集める
    - そこから embed URL を生成：https://tktube.com/ja/embed/<id>
    - state.json 由来の already_seen で重複を除外
      （embed URL・短縮URL でチェック）
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

    # 生の mp4 一覧
    raw_urls = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 多すぎると時間がかかるので、まず FILTER_LIMIT 件に絞る
    candidates = raw_urls[: max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for preview_url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break

        embed_url = preview_to_embed(preview_url)
        if not embed_url:
            # 万一パターンが取れなければ、従来通り preview を使う
            embed_url = preview_url

        norm_embed = _normalize_url(embed_url)

        # 同一 run 内の重複
        if norm_embed in seen_now:
            continue

        # 既に state.json に embed URL があるならスキップ
        if norm_embed in already_seen:
            continue

        # x.gd で短縮（embed URL を短縮）
        short = shorten_via_xgd(embed_url)
        norm_short = _normalize_url(short)

        # 短縮後 URL が既に state.json にあるならスキップ
        if norm_short in already_seen:
            continue

        seen_now.add(norm_embed)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]