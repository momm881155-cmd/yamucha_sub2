# goxplorer2.py — orevideo 専用スクレイパ
#
# ・twimg:
#     https://orevideo.pythonanywhere.com/?page=1&sort=popular からも取得（人気順）
#     ＋ https://orevideo.pythonanywhere.com/?sort=newest&page=N からも取得（新着）
#   - https://video.twimg.com/...mp4?tag=xx  （twimg 生URL）
# ・gofile:
#   - https://orevideo.pythonanywhere.com/?sort=newest&page=N からのみ取得
#   - https://gofile.io/d/XXXXXX             （gofile 生URL）
#
# ・優先順位:
#   1. Googleスプレッドシート(B列)の gofile URL
#        - B列を「下から上」に読む（下の行ほど新しい）
#        - D/E 列の内容は「フィルタには使うが、死活チェックはしない」
#        - いまは「リンク切れチェックは一切しない」
#   2. orevideo の gofile（ページ 1〜GOFILE_PRIORITY_MAX_PAGE を優先）
#   3. twimg で残りを埋める
#
# ・gofile 生存確認:
#   - シート側: いまはチェックなし（そのまま採用）
#   - orevideo 側: HTTP + JS で従来どおりチェック
#
# ・state.json（already_seen）＋このrun内で重複除外

import os
import re
import time
import json
from typing import List, Set, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# =========================
#   基本設定
# =========================

BASE_ORIGIN = os.getenv("OREVIDEO_BASE", "https://orevideo.pythonanywhere.com").rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/123.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_ORIGIN,
    "Connection": "keep-alive",
}

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "200"))
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "80"))

# gofile を何本狙うか（1ツイート内の最大 gofile 本数）
GOFILE_TARGET = int(os.getenv("GOFILE_TARGET", "3"))

# gofile を「優先」する最大ページ（1〜このページを優先）
GOFILE_PRIORITY_MAX_PAGE = int(os.getenv("GOFILE_PRIORITY_MAX_PAGE", "10"))

# orevideo 側で gofile 生存確認を行う最大件数
MAX_GOFILE_CHECK = int(os.getenv("MAX_GOFILE_CHECK", "15"))

# スプシー側で B列から拾う最大件数（下から何行まで見るか）
MAX_SHEET_ROWS_LOOKUP = int(os.getenv("MAX_SHEET_ROWS_LOOKUP", "50"))

# twimg / gofile 抽出用
TWIMG_RE  = re.compile(r"https?://video\.twimg\.com/[^\s\"']+?\.mp4\?tag=\d+", re.I)
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

# =========================
#   スプレッドシート設定
# =========================

SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_CREDENTIALS_JSON_ENV = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
SHEET_NAME = os.getenv("GOOGLE_SHEETS_NAME", "シート1")

# URL -> 行番号 の対応（将来的に E列「post成功」を付けるとき用）
_SHEET_URL_ROW: dict[str, int] = {}


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


def _unique_preserve(seq: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for s in seq:
        s = s.strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# =========================
#   Google スプレッドシート
# =========================

def _get_sheet() -> Optional[gspread.Worksheet]:
    """
    環境変数:
      - GOOGLE_SHEETS_CREDENTIALS_JSON: サービスアカウント JSON の中身
      - GOOGLE_SHEETS_ID: スプレッドシート ID
      - GOOGLE_SHEETS_NAME: シート名（タブ名, デフォルト「シート1」）
    """
    if not (SHEET_CREDENTIALS_JSON_ENV and SPREADSHEET_ID):
        return None
    try:
        info = json.loads(SHEET_CREDENTIALS_JSON_ENV)
        creds = Credentials.from_service_account_info(info, scopes=SHEET_SCOPES)
        client = gspread.authorize(creds)
        sh = client.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet(SHEET_NAME)
        return ws
    except Exception as e:
        print(f"[warn] failed to init Google Sheet: {e}")
        return None


# =========================
#   gofile 判定（orevideo用・厳しめ）
# =========================

NOT_FOUND_KEYWORDS = [
    "This content does not exist",
    "The content you are looking for could not be found",
    "No items to display",
    "This content is password protected",
    "has been automatically removed",
    "has been deleted by the owner",
]


def _is_gofile_alive(
    url: str,
    timeout: int = 15,
    deadline_ts: Optional[float] = None,
) -> bool:
    """
    orevideo 用の「厳しめ」判定。
    gofile のページを直接 GET + JSロードして生存確認。
    - 200 以外: NG
    - HTML / JSロード後の HTML に NOT_FOUND_KEYWORDS が含まれていたら NG
    - 締切(deadline_ts)を超えそうなら即 False
    """
    if _deadline_passed(deadline_ts):
        print(f"[info] skip gofile check due to deadline: {url}")
        return False

    # まずは普通の HTTP GET
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
    except Exception as e:
        print(f"[warn] gofile(requests) failed: {url} ({e})")
        return False

    if r.status_code == 429:
        print(f"[info] gofile status 429: {url}")
        return False

    if r.status_code != 200:
        print(f"[info] gofile status {r.status_code}: {url}")
        return False

    text = (r.text or "")
    for kw in NOT_FOUND_KEYWORDS:
        if kw in text:
            print(f"[info] gofile(not found text): {url}")
            return False

    if _deadline_passed(deadline_ts):
        print(f"[info] skip gofile JS check due to deadline: {url}")
        return False

    # JS ロード後の HTML もチェック（短めの待ち時間）
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            page.wait_for_timeout(1200)  # 1.2秒だけ余分に待つ
            html = page.content()
            browser.close()
        for kw in NOT_FOUND_KEYWORDS:
            if kw in html:
                print(f"[info] gofile(not found text via JS): {url}")
                return False
    except Exception as e:
        # Playwright がコケても致命的にはしない
        print(f"[warn] gofile(playwright) failed: {url} ({e})")

    print(f"[info] gofile alive: {url}")
    return True


# =========================
#   スプシーから URL を読む（チェックなし版）
# =========================

def _load_urls_from_sheet_no_check(
    already_seen: Set[str],
    seen_now: Set[str],
    max_needed: int,
    deadline_ts: Optional[float],
) -> List[str]:
    """
    スプシー(B列)から gofile URL を読み取り、以下だけ行う:

      - B列を「下から上」に読む（下の行ほど新しい）
      - D列 or E列に何か書いてある行はスキップ（手動管理や今までの結果を尊重）
      - B列が重複している場合は、下の行を優先
      - state.json & この run 内の seen_now に含まれる URL はスキップ

    ※ gofile の「リンク切れチェック」は一切しない。
    ※ DEAD / post成功 の書き込みもいまはしない。
    """
    ws = _get_sheet()
    if ws is None:
        return []

    alive_urls: List[str] = []
    local_seen_urls: Set[str] = set()

    try:
        rows = ws.get("B2:E")  # B2〜E 末尾まで
    except Exception as e:
        print(f"[warn] failed to read sheet values: {e}")
        return []

    global _SHEET_URL_ROW
    _SHEET_URL_ROW = {}

    start_row = 2
    total = len(rows)

    # rows を「下から上」に読む
    for i, row in enumerate(reversed(rows)):
        if len(alive_urls) >= max_needed:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during sheet selection; stop.")
            break
        if i >= MAX_SHEET_ROWS_LOOKUP:
            print(f"[info] reached MAX_SHEET_ROWS_LOOKUP={MAX_SHEET_ROWS_LOOKUP}; stop sheet lookup.")
            break

        orig_i = total - 1 - i
        row_index = start_row + orig_i  # 実際のシート行番号

        b = row[0].strip() if len(row) >= 1 and row[0] else ""
        d = row[2].strip() if len(row) >= 3 and row[2] else ""
        e = row[3].strip() if len(row) >= 4 and row[3] else ""

        if not b:
            continue

        norm = _normalize_url(b)

        # gofile 以外は無視
        if not GOFILE_RE.match(norm):
            continue

        # URL -> 行番号（将来 E列更新したいとき用）
        if norm not in _SHEET_URL_ROW:
            _SHEET_URL_ROW[norm] = row_index

        # D or E に何か書いてあれば「すでに扱った or 手動管理中」とみなしてスキップ
        if d or e:
            continue

        # シート内重複（同じURLが上にもある場合は、下の行を優先）
        if norm in local_seen_urls:
            continue
        local_seen_urls.add(norm)

        # state.json / run 内で既に使用済み
        if norm in already_seen or norm in seen_now:
            continue

        # 生存チェックはせず、そのまま採用
        seen_now.add(norm)
        alive_urls.append(norm)

    print(f"[info] sheet(no-check) selected: gofile={len(alive_urls)} (max_needed={max_needed})")
    return alive_urls


def mark_sheet_posted(urls: List[str], label: str = "post成功") -> None:
    """
    将来的に、ツイート成功時に E列へ「post成功」を入れたいとき用の関数。
    いまは bot_orevideo.py から呼んでいないので実質未使用。
    """
    if not urls:
        return
    ws = _get_sheet()
    if ws is None:
        return

    global _SHEET_URL_ROW

    for u in urls:
        norm = _normalize_url(u)
        row = _SHEET_URL_ROW.get(norm)
        if not row:
            continue
        try:
            ws.update_acell(f"E{row}", label)
        except Exception as e:
            print(f"[warn] failed to mark post成功 in sheet (row={row}): {e}")


# =========================
#   HTML からリンク抽出
# =========================

def extract_links_from_html(html: str) -> Tuple[List[str], List[str]]:
    """
    orevideo のページ HTML から
      - twimg mp4
      - gofile
    を抜き出す。
    戻り値: (twimg_list, gofile_list)
    """
    if not html:
        return [], []

    tw = TWIMG_RE.findall(html)
    gf = GOFILE_RE.findall(html)

    tw_u = _unique_preserve(tw)
    gf_u = _unique_preserve(gf)

    print(f"[debug] extract_links_from_html: twimg={len(tw_u)}, gofile={len(gf_u)}")
    return tw_u, gf_u


# =========================
#   orevideo からリンク収集
# =========================

def _collect_orevideo_links(
    num_pages: int,
    deadline_ts: Optional[float],
) -> Tuple[List[str], List[str], List[str]]:
    """
    orevideo のページを巡回してリンクを集める。
    戻り値: (twimg_all, gofile_early, gofile_late)
      - twimg_all     … popular(1ページ目) + newest(1..num_pages)
      - gofile_early  … newest のうち page <= GOFILE_PRIORITY_MAX_PAGE の gofile（優先）
      - gofile_late   … newest のうち page >  GOFILE_PRIORITY_MAX_PAGE の gofile（予備）
    """
    twimg_all: List[str] = []
    gofile_early: List[str] = []
    gofile_late: List[str] = []

    total_raw = 0

    # 0) popular 1ページ目
    try:
        pop_url = f"{BASE_ORIGIN}/?page=1&sort=popular"
        resp = requests.get(pop_url, headers=HEADERS, timeout=20)
        if resp.status_code == 200:
            html = resp.text
            tw_pop, gf_pop = extract_links_from_html(html)
            print(f"[info] orevideo popular {pop_url}: twimg={len(tw_pop)}, gofile={len(gf_pop)}")
            twimg_all.extend(tw_pop)
        else:
            print(f"[warn] orevideo status {resp.status_code} (popular): {pop_url}")
    except Exception as e:
        print(f"[warn] orevideo request failed (popular): {pop_url} ({e})")

    # 1) newest 1..num_pages
    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] orevideo deadline at page={p}; stop.")
            break

        if p == 1:
            url = f"{BASE_ORIGIN}/?sort=newest&page=1"
        else:
            url = f"{BASE_ORIGIN}/?page={p}&sort=newest"

        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"[warn] orevideo request failed: {url} ({e})")
            continue

        if resp.status_code != 200:
            print(f"[warn] orevideo status {resp.status_code}: {url}")
            continue

        html = resp.text
        tw_list, gf_list = extract_links_from_html(html)
        print(f"[info] orevideo list {url}: twimg={len(tw_list)}, gofile={len(gf_list)}")

        twimg_all.extend(tw_list)

        if p <= GOFILE_PRIORITY_MAX_PAGE:
            gofile_early.extend(gf_list)
        else:
            gofile_late.extend(gf_list)

        total_raw = len(twimg_all) + len(gofile_early) + len(gofile_late)
        if total_raw >= RAW_LIMIT:
            print(f"[info] orevideo early stop at RAW_LIMIT={RAW_LIMIT}")
            break

        time.sleep(0.3)

    return twimg_all, gofile_early, gofile_late


# =========================
#   fetch_listing_pages（互換用）
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,
) -> List[str]:
    """
    bot.py 互換用のダミー実装。
    実際の URL 選別は collect_fresh_gofile_urls 側で行うため、
    ここでは twimg + gofile を全部まとめて返すだけ。
    """
    tw, gf_early, gf_late = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)
    all_urls = tw + gf_early + gf_late
    return all_urls[:RAW_LIMIT]


# =========================
#   collect_fresh_gofile_urls（bot_orevideo.py から呼ばれるメイン）
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 5,
    num_pages: int = 50,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    orevideo 用の URL 選別ロジック。

    優先順位:
      1. スプシー(B列)の gofile URL（B列を下から順に見る / チェックなし）
      2. orevideo の gofile（ページ 1〜GOFILE_PRIORITY_MAX_PAGE 優先・厳しめチェック）
      3. twimg で残りを埋める

    - gofile 合計本数は GOFILE_TARGET 本（ただし want まで）
    - orevideo の gofile は _is_gofile_alive() で生存確認
    - シート側 gofile は現在「チェックなし」で採用（D/E列更新なし）
    - already_seen / このrun内の seen_now で重複を避ける
    - MIN_POST 未満なら [] を返す（bot_orevideo.py 側でツイートしない）
    """

    # MIN_POST を環境変数から取得（パースできなければ 1）
    try:
        min_post = int(os.getenv("MIN_POST", "1"))
    except ValueError:
        min_post = 1

    # デッドライン設定
    if deadline_sec is None:
        env = os.getenv("SCRAPE_TIMEOUT_SEC")
        try:
            if env:
                deadline_sec = int(env)
        except Exception:
            deadline_sec = None

    deadline_ts = (_now() + deadline_sec) if deadline_sec else None

    # orevideo から raw リンク収集
    tw_all_raw, gf_early_raw, gf_late_raw = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)

    # 重複削除
    tw_all    = _unique_preserve(tw_all_raw)
    gf_early  = _unique_preserve(gf_early_raw)
    gf_late   = _unique_preserve(gf_late_raw)

    # 目標本数
    go_target = min(GOFILE_TARGET, want)

    results: List[str] = []
    selected_gofile: List[str] = []
    selected_twimg: List[str] = []
    seen_now: Set[str] = set()

    def can_use_url(raw_url: str) -> Optional[str]:
        """state.json & この run 内での重複をチェックして OK なら正規化URLを返す"""
        if not raw_url:
            return None
        norm = _normalize_url(raw_url)
        if norm in seen_now:
            return None
        if norm in already_seen:
            return None
        return norm

    # ------- 0) スプシー(B列)の gofile を「チェックなし」で優先して拾う -------

    sheet_urls = _load_urls_from_sheet_no_check(
        already_seen=already_seen,
        seen_now=seen_now,
        max_needed=go_target,
        deadline_ts=deadline_ts,
    )
    selected_gofile.extend(sheet_urls)

    # ------- 1) orevideo の gofile: 優先ページ (1〜GOFILE_PRIORITY_MAX_PAGE) -------

    gofile_checks = 0
    for url in gf_early:
        if len(selected_gofile) >= go_target:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during gofile-early selection; stop.")
            break
        if gofile_checks >= MAX_GOFILE_CHECK:
            print(f"[info] reached MAX_GOFILE_CHECK={MAX_GOFILE_CHECK}; stop gofile checks.")
            break

        norm = can_use_url(url)
        if not norm:
            continue

        gofile_checks += 1
        if _is_gofile_alive(norm, timeout=10, deadline_ts=deadline_ts):
            seen_now.add(norm)
            selected_gofile.append(norm)

    # ------- 2) orevideo の gofile: それ以降のページ -------

    if len(selected_gofile) < go_target:
        for url in gf_late:
            if len(selected_gofile) >= go_target:
                break
            if _deadline_passed(deadline_ts):
                print("[info] deadline reached during gofile-late selection; stop.")
                break
            if gofile_checks >= MAX_GOFILE_CHECK:
                print(f"[info] reached MAX_GOFILE_CHECK={MAX_GOFILE_CHECK}; stop gofile checks.")
                break

            norm = can_use_url(url)
            if not norm:
                continue

            gofile_checks += 1
            if _is_gofile_alive(norm, timeout=10, deadline_ts=deadline_ts):
                seen_now.add(norm)
                selected_gofile.append(norm)

    current_go = len(selected_gofile)
    remaining  = max(0, want - current_go)

    # ------- 3) twimg で埋める -------

    for url in tw_all:
        if len(selected_twimg) >= remaining:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during twimg selection; stop.")
            break

        norm = can_use_url(url)
        if not norm:
            continue

        seen_now.add(norm)
        selected_twimg.append(norm)

    results = selected_gofile + selected_twimg

    print(
        f"[info] orevideo+sheet selected: gofile={len(selected_gofile)}, "
        f"twimg={len(selected_twimg)}, total={len(results)} (target={want})"
    )

    # MIN_POST 未満なら「何も無かった扱い」
    if len(results) < min_post:
        print(f"[info] only {len(results)} urls collected (< MIN_POST={min_post}); return [].")
        return []

    return results[:want]
