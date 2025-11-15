# goxplorer.py — Google Sheet から URL を拾って短縮 → bot.py に返す 汎用版
#
# 想定シート構成（行方向）:
#   行1: ヘッダ（中身は何でもOK）
#   行2以降:
#     B列 … 元URL
#       - SOURCE_KIND=tktube   のとき: https://tktube.com/ja/videos/327760/...
#       - SOURCE_KIND=monsnode のとき: https://monsnode.com/twjn.php?v=21503421
#     D列 … 投稿済み日時（空なら未投稿とみなす）
#
# 環境変数:
#   SHEET_ID                    ... スプレッドシートID
#   SHEET_NAME                  ... シート名（例: "シート1" or "シート2"）
#   GOOGLE_SERVICE_ACCOUNT_JSON ... サービスアカウントJSON文字列
#
#   SOURCE_KIND                 ... "tktube" / "monsnode"
#   WANT_POST                   ... 1回でツイートしたい件数（bot.pyからも渡される）
#   MIN_POST                    ... これ未満なら「ツイートしない & シート更新しない」
#   XGD_API_KEY                 ... （※今は未使用。v.gd はキー不要）
#
# bot.py からは collect_fresh_gofile_urls() が呼ばれる想定。

import os
import re
import time
import json
from datetime import datetime, timezone
from typing import List, Set, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials

# =========================
#   設定
# =========================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/123.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://tktube.com",
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "100"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "50"))

SOURCE_KIND = os.getenv("SOURCE_KIND", "tktube").strip().lower()

# tktube の /videos/, /video/ から ID を抜く
VIDEO_ID_RE  = re.compile(r"https?://tktube\.com/(?:[a-z]{2}/)?videos/(\d+)/", re.I)
VIDEO_ID2_RE = re.compile(r"https?://tktube\.com/(?:[a-z]{2}/)?video/(\d+)/", re.I)


def _now() -> float:
    return time.monotonic()


def _normalize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    # http → https に正規化（state.json 側と揃える用）
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")


# =========================
#   v.gd 短縮
# =========================

def shorten_via_xgd(long_url: str) -> str:
    """
    v.gd の API で URL を短縮。
    失敗したら元 URL をそのまま返す。
    """
    try:
        r = requests.get(
            "https://v.gd/create.php",
            params={
                "format": "simple",  # 短縮URLのみのテキストが返る
                "url": long_url,
            },
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        short = (r.text or "").strip()

        # v.gd が http で返してきた場合は https に揃える
        if short.startswith("http://"):
            short = "https://" + short[len("http://"):]

        # 想定通りの形式か軽くチェック
        if short.startswith("https://v.gd/"):
            return short
        else:
            print(f"[warn] v.gd unexpected response for {long_url}: {short}")

    except Exception as e:
        print(f"[warn] v.gd shorten failed for {long_url}: {e}")

    # 失敗したときは元URLをそのまま使う
    return long_url


# =========================
#   Google Sheet 関連
# =========================

def _open_sheet():
    """環境変数からサービスアカウントで Sheet を開く。失敗時は None."""
    sheet_id   = os.getenv("SHEET_ID", "").strip()
    sheet_name = os.getenv("SHEET_NAME", "").strip()
    sa_json    = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    if not sheet_id or not sheet_name or not sa_json:
        print("[error] SHEET_ID / SHEET_NAME / GOOGLE_SERVICE_ACCOUNT_JSON が足りません。")
        return None

    try:
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet(sheet_name)
        return ws
    except Exception as e:
        print(f"[error] failed to open sheet: {e}")
        return None


def _sheet_get_unposted(max_rows: int) -> List[Tuple[int, str]]:
    """
    シートから「未投稿行」を最大 max_rows 件取り出す。
    戻り値: [(row_index, url_in_B), ...]  （row_index は 1始まり）
      - B列（index=1）に URL
      - D列（index=3）が空なら「未投稿」
    """
    ws = _open_sheet()
    if ws is None:
        print("[error] cannot open Google Sheet; return empty list.")
        return []

    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[error] sheet get_all_values failed: {e}")
        return []

    if not values:
        print("[info] sheet is empty.")
        return []

    result: List[Tuple[int, str]] = []

    # 1行目はヘッダ想定 → 2行目から
    for row_idx in range(2, len(values) + 1):
        row = values[row_idx - 1]

        url = row[1].strip() if len(row) > 1 and row[1] else ""
        posted = row[3].strip() if len(row) > 3 and row[3] else ""

        if not url:
            continue
        if posted:
            # すでに日付入り = 投稿済み扱い
            continue

        result.append((row_idx, url))

        if len(result) >= max_rows:
            break

    print(f"[info] sheet unposted rows: {len(result)}")
    return result


def _sheet_mark_posted(row_indices: List[int]) -> None:
    """
    指定された行の D列 に現在UTC時刻を入れる。
    row_indices は 1始まりインデックス。
    """
    if not row_indices:
        return

    ws = _open_sheet()
    if ws is None:
        print("[error] cannot reopen sheet to mark posted.")
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for r in row_indices:
        try:
            ws.update_cell(r, 4, now_str)  # D列 = col 4
        except Exception as e:
            print(f"[warn] sheet update_cell failed at row {r}: {e}")

    print(f"[info] sheet marked posted rows: {row_indices}")


# =========================
#   URL 変換ロジック
# =========================

def _to_embed_url_from_video_url(video_url: str) -> Optional[str]:
    """
    tktube 用:
      https://tktube.com/ja/videos/327760/... → https://tktube.com/ja/embed/327760
    """
    m = VIDEO_ID_RE.search(video_url) or VIDEO_ID2_RE.search(video_url)
    if not m:
        return None
    vid = m.group(1)
    return f"https://tktube.com/ja/embed/{vid}"


def _convert_sheet_url(sheet_url: str) -> Optional[str]:
    """
    シートの B列のURL -> 実際に短縮対象とする URL に変換する。
      - SOURCE_KIND=tktube   : /videos → /embed に変換
      - SOURCE_KIND=monsnode : そのまま（monsnode の twjn.php を想定）
    """
    u = sheet_url.strip()
    if not u:
        return None

    if SOURCE_KIND == "tktube":
        return _to_embed_url_from_video_url(u)

    if SOURCE_KIND == "monsnode":
        # 一応 monsnode っぽいものだけ通す
        if "monsnode.com" not in u:
            return None
        # 正規化だけしてそのまま使う
        return _normalize_url(u)

    # デフォルトは「そのまま」
    return _normalize_url(u)


# =========================
#   fetch_listing_pages（bot.py 互換用）
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,
) -> List[str]:
    """
    bot.py 互換用のダミー実装。
    シート上の「未投稿行」から URL のリストを返すだけ。
    実際の重複除外・短縮などは collect_fresh_gofile_urls 側で行う。
    """
    max_rows = RAW_LIMIT
    rows = _sheet_get_unposted(max_rows=max_rows)
    urls: List[str] = []

    for _, sheet_url in rows:
        converted = _convert_sheet_url(sheet_url)
        if converted:
            urls.append(converted)

    return urls[:RAW_LIMIT]


# =========================
#   collect_fresh_gofile_urls（bot.py から呼ばれるメイン）
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 3,
    num_pages: int = 100,               # 互換のためのダミー引数
    deadline_sec: Optional[int] = None, # 互換のためのダミー引数
) -> List[str]:
    """
    - Google Sheet の B列にある URL を SOURCE_KIND に応じて変換
      (tktube→embed, monsnode→そのまま) し、
      state.json 由来の already_seen で重複を除外
      （元URL・短縮後URL 両方をチェック）
    - v.gd で短縮
    - MIN_POST 未満ならシートも state.json も変更せず、 [] を返す
    - MIN_POST 以上あれば、使った行に D列で日付を入れて、want 件だけ返す
    """

    # MIN_POST を環境変数から取得（パースできなければ 1）
    try:
        min_post = int(os.getenv("MIN_POST", "1"))
    except ValueError:
        min_post = 1

    # シートから候補を取り出す件数（少し多めに取る）
    base = max(want, min_post, FILTER_LIMIT)
    max_rows = base * 2

    rows = _sheet_get_unposted(max_rows=max_rows)
    if not rows:
        return []

    results: List[str] = []
    used_rows: List[int] = []
    seen_now: Set[str] = set()

    for row_idx, sheet_url in rows:
        target_url = _convert_sheet_url(sheet_url)
        if not target_url:
            continue

        norm_target = _normalize_url(target_url)

        # この run 内での重複
        if norm_target in seen_now:
            continue

        # state.json（元URL）に既にあるならスキップ
        if norm_target in already_seen:
            continue

        # v.gd で短縮
        short = shorten_via_xgd(target_url)
        norm_short = _normalize_url(short)

        # state.json（短縮URL）に既にあるならスキップ
        if norm_short in already_seen:
            continue

        # 採用
        seen_now.add(norm_target)
        used_rows.append(row_idx)
        results.append(short)

        if len(results) >= want:
            break

    # ===== MIN_POST を満たさない場合は「何もなかった扱い」 =====
    if len(results) < min_post:
        print(f"[info] only {len(results)} urls collected (< MIN_POST={min_post}); do not mark sheet.")
        return []

    # MIN_POST 以上集まった場合だけ「投稿済み」としてシートを更新
    if used_rows:
        _sheet_mark_posted(used_rows)

    return results[:want] 