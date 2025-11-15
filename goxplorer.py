# goxplorer.py — tktube + Googleスプレッドシート専用版
# - スプシB列の tktube 動画URL から embed URL を作成
# - x.gd で短縮
# - 使った行の D 列に投稿日時を書き込む
# - bot.py からは collect_fresh_gofile_urls() だけ今まで通り呼べる

import os
import re
import json
import time
from datetime import datetime, timezone
from typing import List, Set, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials

# =========================
#  設定・共通ユーティリティ
# =========================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))   # 形式だけ残す（ほぼ未使用）
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50")) # 同上

SHEET_ID   = os.getenv("SHEET_ID", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "シート1").strip() or "シート1"


def _now() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now() >= deadline_ts


def _normalize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    # 必要なら http→https 揃え
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")


# =========================
#  x.gd 短縮
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
#  Google Sheets 周り
# =========================

def _open_worksheet():
    """
    Service Account JSON（環境変数 GOOGLE_SERVICE_ACCOUNT_JSON）から
    スプレッドシート(SHEET_ID, SHEET_NAME)を開く。
    """
    if not SHEET_ID:
        print("[error] SHEET_ID is empty.")
        return None

    sa_raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_raw:
        print("[error] GOOGLE_SERVICE_ACCOUNT_JSON is empty.")
        return None

    try:
        sa = json.loads(sa_raw)
    except Exception as e:
        print(f"[error] failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
        return None

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        creds = Credentials.from_service_account_info(sa, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet(SHEET_NAME)
    except Exception as e:
        print(f"[error] failed to open sheet: {e}")
        return None

    return ws


def _load_unposted_rows(ws) -> List[Tuple[int, str]]:
    """
    シート全体を読み、まだ投稿していない行を拾う。
    - B列: 元の tktube 動画URL
    - D列: 投稿日時（空なら「未投稿」とみなす）
    戻り値: [(row_index, video_url), ...]
    """
    values = ws.get_all_values()
    if not values:
        return []

    results: List[Tuple[int, str]] = []

    for idx, row in enumerate(values, start=1):
        if idx == 1:
            # 1行目はヘッダ想定（「video_url」とか）
            continue

        # B列（インデックス1）
        video_url = row[1].strip() if len(row) > 1 and row[1] else ""
        # D列（インデックス3）
        posted_at = row[3].strip() if len(row) > 3 and row[3] else ""

        if not video_url:
            continue
        if posted_at:
            # 既に投稿済み
            continue
        if "tktube.com" not in video_url:
            continue

        results.append((idx, video_url))

    print(f"[info] sheet unposted rows: {len(results)}")
    return results


def _mark_posted(ws, row_indices: List[int]):
    """
    指定された行の D 列に現在時刻(UTC)を書き込む。
    """
    if not row_indices:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 1行ずつでも件数少ないのでOK（毎時5件程度）
    for r in row_indices:
        try:
            ws.update_cell(r, 4, now)  # D列 = 4
        except Exception as e:
            print(f"[warn] failed to update D{r}: {e}")


# =========================
#  tktube 動画URL -> embed URL
# =========================

_VIDEO_RE = re.compile(
    r"tktube\.com/(?:([a-z]{2})/)?videos/(\d+)/",
    re.I,
)


def _video_url_to_embed(video_url: str) -> Optional[str]:
    """
    https://tktube.com/ja/videos/327760/... →
    https://tktube.com/ja/embed/327760
    """
    m = _VIDEO_RE.search(video_url)
    if not m:
        return None

    lang = (m.group(1) or "ja").lower()
    vid = m.group(2)

    return f"https://tktube.com/{lang}/embed/{vid}"


# =========================
#  旧 API 互換: fetch_listing_pages
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None
) -> List[str]:
    """
    旧 goxplorer 用のダミー実装。
    今回は「外部サイトから収集」ではなく、
    スプレッドシートから読むのでここでは何もしない。
    """
    # bot.py から呼ばれても差し支えないように空リストを返す。
    return []


# =========================
#  メイン: collect_fresh_gofile_urls
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    bot.py から呼び出されるメイン関数。
    - シートのB列から tktube 動画URLを取得
    - D列が空（未投稿）のものだけ対象
    - embed URL に変換 → x.gd で短縮
    - state.json の already_seen で重複も避ける
    - 採用した行には D列に投稿時刻を書き込む
    - 最終的に (短縮済みURL) を want 件まで返す
    """

    # デッドラインは一応受け取るが、ほぼ関係なし
    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    ws = _open_worksheet()
    if ws is None:
        print("[error] cannot open Google Sheet; return empty list.")
        return []

    # シートからまだ投稿していない行を取得
    candidates = _load_unposted_rows(ws)

    results: List[str] = []
    seen_now: Set[str] = set()
    used_rows: List[int] = []

    for row_idx, video_url in candidates:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during sheet processing; stop.")
            break

        embed_url = _video_url_to_embed(video_url)
        if not embed_url:
            print(f"[warn] cannot parse video url at row {row_idx}: {video_url}")
            continue

        norm_embed = _normalize_url(embed_url)

        # 同一run内
        if norm_embed in seen_now:
            continue

        # state.json 側に既に存在するならスキップ
        if norm_embed in already_seen:
            continue

        # x.gd で短縮
        short = shorten_via_xgd(embed_url)
        norm_short = _normalize_url(short)

        # 短縮後 URL が state.json に既にあればスキップ
        if norm_short in already_seen:
            continue

        seen_now.add(norm_embed)
        results.append(short)
        used_rows.append(row_idx)

        print(f"[info] picked row {row_idx}: {video_url} -> {embed_url} -> {short}")

        if len(results) >= want:
            break

    # D列に投稿日時を記録
    try:
        _mark_posted(ws, used_rows)
    except Exception as e:
        print(f"[warn] failed to mark posted rows: {e}")

    return results[:want]