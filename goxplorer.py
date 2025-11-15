# goxplorer.py — tktube embed 用：スプレッドシートの /videos/URL から
# embed URL を組み立てて x.gd で短縮して返す版（scraping なし）

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
#   設定
# =========================

# tktube のベースURL（embed 生成に使用）
BASE_ORIGIN = "https://tktube.com"

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

# Sheets 関連（環境変数から読む）
SHEET_ID   = os.getenv("SHEET_ID", "").strip()
SHEET_NAME = os.getenv("SHEET_NAME", "").strip() or "シート1"

# /videos/123456/ や /video/123456/ から ID を拾う
VIDEO_ID_RE = re.compile(r"/videos?/(\d+)/", re.I)


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
#   Google Sheets ヘルパ
# =========================

def _open_sheet():
    """
    サービスアカウント JSON（環境変数）から認証し、
    指定のシートを開いて Worksheet を返す。
    """
    if not SHEET_ID:
        print("[error] SHEET_ID is empty.")
        return None

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw_json:
        print("[error] GOOGLE_SERVICE_ACCOUNT_JSON is empty.")
        return None

    try:
        sa_info = json.loads(raw_json)
    except Exception as e:
        print(f"[error] invalid GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
        return None

    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    gc = gspread.authorize(creds)

    try:
        sh = gc.open_by_key(SHEET_ID)
    except Exception as e:
        print(f"[error] failed to open sheet: {e}")
        return None

    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        print(f"[error] worksheet '{SHEET_NAME}' not found in sheet {SHEET_ID}")
        return None
    except Exception as e:
        print(f"[error] failed to open worksheet: {e}")
        return None

    return ws


def _extract_video_id(src: str) -> Optional[str]:
    """
    スプレッドシートに入っている文字列から tktube の動画 ID を抽出する。
    - フル URL: https://tktube.com/ja/videos/327760/xxx
    - 省略 URL: https://tktube.com/videos/327760/
    - 数字だけ: 327760
    のどれでも OK。
    """
    if not src:
        return None

    s = src.strip()

    # 数字だけのケース
    if s.isdigit():
        return s

    m = VIDEO_ID_RE.search(s)
    if m:
        return m.group(1)

    return None


def _make_embed_url(video_id: str) -> str:
    """
    動画 ID から embed URL を作る。
    """
    video_id = video_id.strip()
    return f"{BASE_ORIGIN}/ja/embed/{video_id}"


def _sheet_fetch_unposted_rows(max_count: int) -> Tuple[Optional[gspread.Worksheet], List[Tuple[int, str]]]:
    """
    シートから「まだ投稿していない行」を最大 max_count 行ぶん取得。
    想定レイアウト:
      - 1行目: ヘッダ
      - 2行目以降:
          B列: 動画 URL (https://tktube.com/ja/videos/123456/...)
          D列: posted_at（空なら未投稿）
    戻り値: (worksheet, [(row_index, video_url), ...])
    """
    ws = _open_sheet()
    if ws is None:
        print("[error] cannot open Google Sheet; return empty list.")
        return None, []

    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[error] ws.get_all_values failed: {e}")
        return ws, []

    if not values:
        print("[info] sheet has no rows.")
        return ws, []

    results: List[Tuple[int, str]] = []

    # 1行目はヘッダ扱いなのでスキップ
    for idx, row in enumerate(values[1:], start=2):
        # row[1] = B列, row[3] = D列 (0-based index)
        video_url = row[1].strip() if len(row) > 1 and row[1] else ""
        posted_at = row[3].strip() if len(row) > 3 and row[3] else ""

        if not video_url:
            continue
        if posted_at:
            # すでに投稿済み
            continue

        results.append((idx, video_url))
        if len(results) >= max_count:
            break

    print(f"[info] sheet unposted rows: {len(results)}")
    return ws, results


def _sheet_mark_posted(ws, row_indices: List[int]):
    """
    指定された行番号の D 列に投稿日時（UTC）を書き込む。
    """
    if not ws or not row_indices:
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # まとめてアップデート（負荷軽減）
    cells = []
    for r in row_indices:
        # D列 = 4 列目
        cells.append(gspread.cell.Cell(row=r, col=4, value=now_str))

    try:
        ws.update_cells(cells)
        print(f"[info] sheet marked posted rows: {row_indices}")
    except Exception as e:
        print(f"[warn] failed to update posted_at in sheet: {e}")


# =========================
#   fetch_listing_pages（ダミー）
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None
) -> List[str]:
    """
    旧 goxplorer のインターフェイス互換だが、
    今回は tktube からスクレイピングしないのでダミー実装。
    実際の取得は collect_fresh_gofile_urls() の中でシートから行う。
    """
    return []


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
    - Google スプレッドシートから「未投稿の /videos/... URL」を取り出す
    - tktube の embed URL (https://tktube.com/ja/embed/<id>) に変換
    - x.gd で短縮
    - want 件だけ返す
    - 投稿に使った行の D 列に投稿日時を書き込む
    """

    if deadline_sec is None:
        _env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if _env:
                deadline_sec = int(_env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # シートから最大 RAW_LIMIT 件まで候補取得
    ws, rows = _sheet_fetch_unposted_rows(max_count=RAW_LIMIT)
    if not rows:
        print("[info] no unposted rows in sheet.")
        return []

    results: List[str] = []
    used_rows: List[int] = []
    seen_now: Set[str] = set()

    for row_idx, src_url in rows:
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during sheet filtering; stop.")
            break

        vid = _extract_video_id(src_url)
        if not vid:
            print(f"[warn] cannot extract video id from: {src_url}")
            continue

        embed_url = _make_embed_url(vid)
        norm_embed = _normalize_url(embed_url)

        if norm_embed in seen_now:
            continue
        if norm_embed in already_seen:
            # state.json にも残したい場合はここで弾ける
            continue

        short = shorten_via_xgd(embed_url)
        norm_short = _normalize_url(short)

        if norm_short in already_seen:
            continue

        seen_now.add(norm_embed)
        results.append(short)
        used_rows.append(row_idx)

        if len(results) >= want:
            break

    # シートに「投稿済み」の印を付ける
    if ws and used_rows:
        _sheet_mark_posted(ws, used_rows)

    return results[:want]