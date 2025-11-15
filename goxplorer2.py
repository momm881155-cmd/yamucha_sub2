# goxplorer2.py — orevideo.pythonanywhere.com 専用スクレイパー
#
# 役割:
#   - orevideo の一覧ページ (?sort=newest&page=N) を巡回
#   - data-video-url="https://video.twimg.com/....mp4?tag=14" を回収
#     → v.gd で短縮して返す
#   - HTML 内に出てくる https://gofile.io/d/XXXXXX を回収
#     → 生URLのまま返す（短縮しない）
#   - state.json 由来の already_seen と、この run 内の重複を排除
#   - WANT_POST / MIN_POST / RAW_LIMIT / FILTER_LIMIT で制御
#
# bot_orevideo.py から collect_fresh_gofile_urls() が呼ばれる想定

import os
import re
import time
from typing import List, Set, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# =========================
#   設定
# =========================

BASE_LIST_URL = os.getenv(
    "ORE_BASE_URL",
    "https://orevideo.pythonanywhere.com"
).rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/123.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://orevideo.pythonanywhere.com",
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "200"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "80"))  # 候補数の頭打ち用

# gofile 検出用
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)


def _now() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts


def _normalize_url(u: str) -> str:
    """
    state.json と同じ形で比較できるように http→https & 末尾 / 削り
    """
    if not u:
        return u
    u = u.strip()
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")


# =========================
#   v.gd 短縮
# =========================

def shorten_via_vgd(long_url: str) -> str:
    """
    v.gd の API で URL を短縮。
    失敗したら元 URL をそのまま返す。
    """
    try:
        r = requests.get(
            "https://v.gd/create.php",
            params={"format": "simple", "url": long_url},
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        short = r.text.strip()
        # v.gd は短縮成功時に http(s) から始まる URL を返す
        if short.lower().startswith("http"):
            return short
    except Exception as e:
        print(f"[warn] v.gd shorten failed for {long_url}: {e}")

    return long_url


# =========================
#   一覧ページ URL 生成
# =========================

def _ore_listing_urls(num_pages: int) -> List[str]:
    """
    https://orevideo.pythonanywhere.com/?sort=newest&page=1
    https://orevideo.pythonanywhere.com/?page=2&sort=newest
    みたいな URL を 1..num_pages ぶん作る
    """
    urls: List[str] = []
    base = BASE_LIST_URL

    for page in range(1, num_pages + 1):
        if page == 1:
            url = f"{base}/?sort=newest&page=1"
        else:
            url = f"{base}/?page={page}&sort=newest"
        urls.append(url)

    return urls


# =========================
#   HTML → twimg / gofile 抜き出し
# =========================

def extract_links_from_html(html: str) -> Tuple[List[str], List[str]]:
    """
    orevideo の HTML から:
      - data-video-url="https://video.twimg.com/...mp4?tag=14" を抽出
      - https://gofile.io/d/XXXXXX を抽出
    戻り値: (twimg_list, gofile_list)
    """
    if not html:
        return [], []

    soup = BeautifulSoup(html, "html.parser")

    twimg_links: List[str] = []
    gofile_links: List[str] = []

    seen_twimg: Set[str] = set()
    seen_gofile: Set[str] = set()

    # data-video-url="https://video.twimg.com/..."
    for li in soup.find_all("li", attrs={"data-video-url": True}):
        u = li.get("data-video-url", "").strip()
        if not u:
            continue
        if "video.twimg.com" not in u:
            continue
        if ".mp4" not in u:
            continue

        full = _normalize_url(u)
        if full in seen_twimg:
            continue
        seen_twimg.add(full)
        twimg_links.append(full)

    # HTML 全体から gofile.io/d/XXXXXX を正規表現で拾う
    for m in GOFILE_RE.finditer(html):
        u = _normalize_url(m.group(0))
        if u in seen_gofile:
            continue
        seen_gofile.add(u)
        gofile_links.append(u)

    print(
        f"[debug] extract_links_from_html: twimg={len(twimg_links)}, "
        f"gofile={len(gofile_links)}"
    )
    return twimg_links, gofile_links


# =========================
#   orevideo → URL 一覧収集
# =========================

def _collect_orevideo_urls(num_pages: int, deadline_ts: Optional[float]) -> List[str]:
    """
    orevideo の複数ページを回して、twimg + gofile の URL を集める。
    """
    all_urls: List[str] = []
    seen: Set[str] = set()

    listing_urls = _ore_listing_urls(num_pages=num_pages)

    for list_url in listing_urls:
        if _deadline_passed(deadline_ts):
            print(f"[info] orevideo deadline at {list_url}; stop.")
            break

        try:
            resp = requests.get(list_url, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"[warn] orevideo request failed: {list_url} ({e})")
            continue

        if resp.status_code != 200:
            print(f"[warn] orevideo status {resp.status_code}: {list_url}")
            continue

        html = resp.text
        twimg_links, gofile_links = extract_links_from_html(html)

        print(
            f"[info] orevideo list {list_url}: "
            f"twimg={len(twimg_links)}, gofile={len(gofile_links)}"
        )

        # まずは twimg, そのあと gofile を同一リストに詰めていく
        for u in twimg_links + gofile_links:
            if u in seen:
                continue
            seen.add(u)
            all_urls.append(u)
            if len(all_urls) >= RAW_LIMIT:
                print(f"[info] orevideo early stop at RAW_LIMIT={RAW_LIMIT}")
                return all_urls[:RAW_LIMIT]

        time.sleep(0.3)

    return all_urls[:RAW_LIMIT]


# =========================
#   fetch_listing_pages（bot.py 互換）
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,
) -> List[str]:
    """
    旧 goxplorer との互換のためのラッパ。
    orevideo の twimg + gofile URL をまとめて返す。
    """
    return _collect_orevideo_urls(num_pages=num_pages, deadline_ts=deadline_ts)


# =========================
#   collect_fresh_gofile_urls（bot_orevideo.py から呼ばれる）
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    - orevideo から twimg (mp4) + gofile の URL を集める
    - state.json 由来の already_seen で重複を除外
      （元URL・短縮後URL 両方をチェック）
    - twimg → v.gd で短縮 / gofile → 生URL
    - want 件だけ返す（MIN_POST 未満なら []）
    """

    # MIN_POST を環境変数から取得（パースできなければ 1）
    try:
        min_post = int(os.getenv("MIN_POST", "1"))
    except ValueError:
        min_post = 1

    # 締切時間
    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # 生の候補 URL 一覧
    raw_urls = fetch_listing_pages(num_pages=num_pages, deadline_ts=deadline_ts)

    # 多すぎると時間がかかるので、まず FILTER_LIMIT 件に絞る
    candidates = raw_urls[: max(1, FILTER_LIMIT)]

    results: List[str] = []
    seen_now: Set[str] = set()

    for url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during filtering; stop.")
            break

        norm_orig = _normalize_url(url)

        # この run 内での重複
        if norm_orig in seen_now:
            continue

        # state.json（元URL）に既にあるならスキップ
        if norm_orig in already_seen:
            continue

        # 種別ごとに短縮有無を分ける
        if norm_orig.startswith("https://gofile.io/d/"):
            short = url  # gofile は短縮せず生URL
        else:
            # それ以外（主に twimg mp4）は v.gd で短縮
            short = shorten_via_vgd(url)

        norm_short = _normalize_url(short)

        # state.json（短縮URL）に既にあるならスキップ
        if norm_short in already_seen:
            continue

        seen_now.add(norm_orig)
        results.append(short)

        if len(results) >= want:
            break

    # MIN_POST 未満なら「何もなかった扱い」
    if len(results) < min_post:
        print(f"[info] only {len(results)} urls collected (< MIN_POST={min_post}); skip.")
        return []

    return results[:want]