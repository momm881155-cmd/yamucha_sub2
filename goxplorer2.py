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
#   2. orevideo の gofile（ページ 1〜GOFILE_PRIORITY_MAX_PAGE を優先）
#   3. twimg で残りを埋める
#
# ・gofile は必ず「生存確認」してから採用
#   - HTTP ステータス
#   - HTML 本文 / JSロード後の HTML に
#       "This content does not exist",
#       "The content you are looking for could not be found",
#       "No items to display",
#       "This content is password protected",
#       "has been automatically removed",
#       "has been deleted by the owner"
#     などが出ていないか
# ・state.json（already_seen）＋このrun内で重複除外
# ・スプシー:
#   - B列: gofile URL（http でも可）
#   - D列: リンク切れなら「リンク切れ」
#   - E列: ツイート成功したら「post成功」
#   - D/E に何か書いてある行は再チェックしない
#   - 同じ URL が複数行にあっても、1つ目だけ使う

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

RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "200"))  # orevideo 用は 200 で十分
FILTER_LIMIT = int(os.getenv("FILTER_LIMIT", "80"))

# gofile を何本狙うか（1ツイート内の最大 gofile 本数）
GOFILE_TARGET = int(os.getenv("GOFILE_TARGET", "3"))

# gofile を「優先」する最大ページ（ここでは 1〜10 ページ目を優先）
GOFILE_PRIORITY_MAX_PAGE = int(os.getenv("GOFILE_PRIORITY_MAX_PAGE", "10"))

# 1run で「生存確認」を行う gofile の上限本数
MAX_GOFILE_CHECK = int(os.getenv("MAX_GOFILE_CHECK", "15"))

# twimg / gofile 抽出用
TWIMG_RE  = re.compile(r"https?://video\.twimg\.com/[^\s\"']+?\.mp4\?tag=\d+", re.I)
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

# =========================
#   スプレッドシート設定
# =========================
# ※ ファイル名は gofile_links ですが、コードでは URL の ID を使います
#   - GOOGLE_SHEETS_CREDENTIALS_JSON: サービスアカウント JSON の中身（Secrets）
#   - GOOGLE_SHEETS_ID: スプレッドシート ID（URL中の /d/xxxx/ の xxxx 部分）（Vars）
#   - GOOGLE_SHEETS_NAME: シート名（タブ名）省略時は「シート1」

SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_CREDENTIALS_JSON_ENV = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
SHEET_NAME = os.getenv("GOOGLE_SHEETS_NAME", "シート1")

# URL -> 行番号 の対応（同一 run 内で共有）
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
      - GOOGLE_SHEETS_CREDENTIALS_JSON: サービスアカウント JSON の中身をそのまま文字列で
      - GOOGLE_SHEETS_ID: スプレッドシート ID
      - GOOGLE_SHEETS_NAME: シート名（タブ名, デフォルト「シート1」）
    どれかが無い場合は None を返して、既存ロジックだけで動くようにする。
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


def _load_alive_urls_from_sheet(
    already_seen: Set[str],
    seen_now: Set[str],
    max_needed: int,
    gofile_checks_ref: list[int],
    deadline_ts: Optional[float],
) -> List[str]:
    """
    スプシー(B列)から gofile URL を読み取り、以下を行う:
      - D列 or E列に何か書いてある行はスキップ
      - B列が重複している場合は先に出てきた行だけ採用
      - state.json & この run 内の seen_now に含まれる URL はスキップ
      - gofile 生存確認 (_is_gofile_alive) で NG の場合は D列に「リンク切れ」
      - 生存しているものだけを返す（最大 max_needed 本）
    gofile_checks_ref[0] に、チェック回数を足し込む。
    """
    ws = _get_sheet()
    if ws is None:
        return []

    alive_urls: List[str] = []
    local_seen_urls: Set[str] = set()

    try:
        # B2:E の範囲をまとめて取得（2行目以降）
        rows = ws.get("B2:E")
    except Exception as e:
        print(f"[warn] failed to read sheet values: {e}")
        return []

    global _SHEET_URL_ROW
    _SHEET_URL_ROW = {}

    for idx, row in enumerate(rows, start=2):  # 行番号は 2 から
        if len(alive_urls) >= max_needed:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during sheet selection; stop.")
            break
        if gofile_checks_ref[0] >= MAX_GOFILE_CHECK:
            print(f"[info] reached MAX_GOFILE_CHECK={MAX_GOFILE_CHECK} in sheet; stop.")
            break

        b = row[0].strip() if len(row) >= 1 and row[0] else ""
        d = row[2].strip() if len(row) >= 3 and row[2] else ""
        e = row[3].strip() if len(row) >= 4 and row[3] else ""

        if not b:
            continue

        norm = _normalize_url(b)

        # gofile 以外は無視（念のため）
        if not GOFILE_RE.match(norm):
            continue

        # 行番号キャッシュ（重複でも上書きしない）
        if norm not in _SHEET_URL_ROW:
            _SHEET_URL_ROW[norm] = idx

        # D or E に何か書いてあれば「すでに処理済み」とみなしてスキップ
        if d or e:
            continue

        # 同じ URL がスプシー内で重複していたら 1つ目だけ使う（local_seen_urls）
        if norm in local_seen_urls:
            continue
        local_seen_urls.add(norm)

        # state.json & この run ですでに使った URL はスキップ
        if norm in already_seen or norm in seen_now:
            continue

        gofile_checks_ref[0] += 1
        if _is_gofile_alive(norm):
            seen_now.add(norm)
            alive_urls.append(norm)
        else:
            # リンク切れなら D列に「リンク切れ」
            try:
                ws.update(f"D{idx}", "リンク切れ")
            except Exception as e2:
                print(f"[warn] failed to mark dead in sheet (row={idx}): {e2}")

    print(f"[info] sheet selected: gofile={len(alive_urls)} (max_needed={max_needed})")
    return alive_urls


def mark_sheet_posted(urls: List[str], label: str = "post成功") -> None:
    """
    ツイートに成功した URL について、スプシーの E列に「post成功」を書き込む。
    - その run で _load_alive_urls_from_sheet を通っていない URL は、行番号が分からないので無視。
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
            ws.update(f"E{row}", label)
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

    # 0) twimg 用: popular 1ページ目からも twimg を拾う（gofile は捨てる）
    try:
        pop_url = f"{BASE_ORIGIN}/?page=1&sort=popular"
        resp = requests.get(pop_url, headers=HEADERS, timeout=20)
        if resp.status_code == 200:
            html = resp.text
            tw_pop, gf_pop = extract_links_from_html(html)
            print(f"[info] orevideo popular {pop_url}: twimg={len(tw_pop)}, gofile={len(gf_pop)}")
            twimg_all.extend(tw_pop)  # popular 由来の twimg を先頭に足す
        else:
            print(f"[warn] orevideo status {resp.status_code} (popular): {pop_url}")
    except Exception as e:
        print(f"[warn] orevideo request failed (popular): {pop_url} ({e})")

    # 1) 以降は従来どおり newest で 1..num_pages を巡回（gofile ロジックはそのまま）
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

        # twimg は newest 分も普通に足す
        twimg_all.extend(tw_list)

        # gofile は従来どおり newest 側からのみ集計
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
#   gofile 生存確認
# =========================

NOT_FOUND_KEYWORDS = [
    "This content does not exist",
    "The content you are looking for could not be found",
    "No items to display",
    "This content is password protected",
    "has been automatically removed",
    "has been deleted by the owner",
]


def _is_gofile_alive(url: str, timeout: int = 15) -> bool:
    """
    gofile のページを直接 GET して生存確認。
    - 200 以外: 基本 NG
    - HTML / JSロード後の HTML に NOT_FOUND_KEYWORDS が含まれていたら NG
    """
    # まずは普通の HTTP GET で判定
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

    # 念のため JS ロード後の HTML もチェック（Playwright 使用）
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
        for kw in NOT_FOUND_KEYWORDS:
            if kw in html:
                print(f"[info] gofile(not found text via JS): {url}")
                return False
    except Exception as e:
        # Playwright がコケても致命的にはしない
        print(f"[warn] gofile(playwright) failed: {url} ({e})")

    # 特に問題なければ「生きている」と判断
    print(f"[info] gofile alive: {url}")
    return True


# =========================
#   fetch_listing_pages（互換用／実際はあまり使わない）
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
      1. スプシー(B列)の gofile URL
      2. orevideo の gofile（ページ 1〜GOFILE_PRIORITY_MAX_PAGE 優先）
      3. twimg で残りを埋める

    - gofile 合計本数は GOFILE_TARGET 本（ただし want まで）
    - gofile は必ず _is_gofile_alive() で生存確認
    - スプシー:
        * B列: URL
        * D列: 「リンク切れ」
        * E列: 「post成功」
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

    # orevideo から raw リンク収集（従来どおり）
    tw_all_raw, gf_early_raw, gf_late_raw = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)

    # 重複削除（ページ全体として）
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

    # ------- 0) スプシー(B列)の gofile を優先して拾う -------

    gofile_checks_ref = [0]
    sheet_alive = _load_alive_urls_from_sheet(
        already_seen=already_seen,
        seen_now=seen_now,
        max_needed=go_target,
        gofile_checks_ref=gofile_checks_ref,
        deadline_ts=deadline_ts,
    )
    selected_gofile.extend(sheet_alive)
    gofile_checks = gofile_checks_ref[0]

    # ------- 1) gofile: 優先ページ (1〜GOFILE_PRIORITY_MAX_PAGE) -------

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
        if _is_gofile_alive(norm):
            seen_now.add(norm)
            selected_gofile.append(norm)

    # ------- 2) gofile: それ以降のページ（足りないときだけ） -------

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
            if _is_gofile_alive(norm):
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
