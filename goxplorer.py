# goxplorer.py — tktube 用: data-preview の mp4 を集めて x.gd で短縮して返す版

import os
import re
import time
from typing import List, Set, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# =========================
#   基本設定
# =========================

# tktube のベースURL（必要なら env で上書き可能）
BASE_ORIGIN = os.getenv("BASE_ORIGIN", "https://tktube.com").rstrip("/")

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

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))

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
#   スクレイピング対象URLリスト
# =========================
#   ★ここを少しいじれば、他カテゴリにも流用できます。

def _listing_urls() -> List[str]:
    """
    収集対象の一覧ページURLを返す。

    - 環境変数 TKTUBE_CATEGORY_URLS があればそちらを優先
      （カンマ or 改行区切りで複数指定可）
      例:
        TKTUBE_CATEGORY_URLS="https://tktube.com/ja/categories/fc2/?page={page}"
    """
    env = os.getenv("TKTUBE_CATEGORY_URLS", "").strip()
    if env:
        parts = re.split(r"[,\n]+", env)
        urls = [p.strip() for p in parts if p.strip()]
        if urls:
            return urls

    # デフォルト: ご提示の fc2 カテゴリ（?page={page} は後で format される）
    return [
        f"{BASE_ORIGIN}/ja/categories/fc2/?page={{page}}",
    ]


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
#   一覧ページ → data-preview の mp4 抽出
# =========================

def extract_preview_mp4_from_listing(html: str) -> List[str]:
    """
    一覧ページの HTML から data-preview="...mp4" を全部抜き出す。

    例: 
      <img src="...jpg"
           data-preview="https://pv.tkcdns.com/.../357384_preview.mp4"
           ...>
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []
    seen: Set[str] = set()

    # data-preview 属性を持つ全タグを対象にする
    for tag in soup.find_all(attrs={"data-preview": True}):
        pv = (tag.get("data-preview") or "").strip()
        if not pv:
            continue

        # 絶対URL / 相対URL 両対応
        full = pv if pv.startswith("http") else urljoin(BASE_ORIGIN, pv)
        full = _normalize_url(full)

        if full not in seen:
            seen.add(full)
            urls.append(full)

    print(f"[debug] extract_preview_mp4_from_listing: {len(urls)} urls")
    return urls


def _collect_preview_urls(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    - _listing_urls() でカテゴリ一覧URLを取得
    - 各ページから data-preview の mp4 を回収
    """
    all_urls: List[str] = []
    seen: Set[str] = set()
    listing_bases = _listing_urls()

    for base_url in listing_bases:
        for p in range(1, num_pages + 1):
            if _deadline_passed(deadline_ts):
                print(f"[info] deadline at base={base_url}, page={p}; stop.")
                return all_urls[:RAW_LIMIT]

            # ページング
            if "{page}" in base_url:
                list_url = base_url.format(page=p)
            else:
                # ?page がないURLなら1ページ目だけ
                if p > 1:
                    break
                list_url = base_url

            try:
                resp = requests.get(list_url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
            except Exception as e:
                print(f"[warn] listing requests failed: {list_url} ({e})")
                continue

            html = resp.text
            previews = extract_preview_mp4_from_listing(html)
            print(f"[info] list {list_url}: found {len(previews)} preview mp4")

            for u in previews:
                if u in seen:
                    continue
                seen.add(u)
                all_urls.append(u)
                if len(all_urls) >= RAW_LIMIT:
                    print(f"[info] early stop at RAW_LIMIT={RAW_LIMIT}")
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
    旧 goxplorer と同じインターフェイス。
    ここでは「data-preview の mp4 URL の生リスト」を返す。
    """
    return _collect_preview_urls(num_pages=num_pages, deadline_ts=deadline_ts)


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
    - data-preview の mp4 URL を集める
    - state.json 由来の already_seen で重複を除外
      （元URL・短縮URL 両方をチェック）
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

    # 生の mp4 URL 一覧
    raw_urls = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 多すぎると時間がかかるので上限
    candidates = raw_urls[: max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break

        norm_url = _normalize_url(url)

        # 同一 run 内の重複
        if norm_url in seen_now:
            continue

        # 元URL がすでに state.json にある場合はスキップ
        if norm_url in already_seen:
            continue

        # x.gd で短縮
        short = shorten_via_xgd(url)
        norm_short = _normalize_url(short)

        # 短縮後 URL が state.json にある場合もスキップ
        if norm_short in already_seen:
            continue

        seen_now.add(norm_url)
        results.append(short)

        if len(results) >= want:
            break

    return results[:want]