# goxplorer2.py — orevideo 専用スクレイパ（短縮なし版＋Playwrightでgofileチェック）
#
# ・https://orevideo.pythonanywhere.com/?sort=newest&page=N から
#   - https://video.twimg.com/...mp4?tag=xx  （twimg / 生URL）
#   - https://gofile.io/d/XXXXXX             （gofile / 生URL）
#   を収集
#
# ・gofile は「新しいものを優先」
#   - orevideo の page=1 が一番新しい → page=1 から順に拾う
#   - GOFILE_PRIORITY_MAX_PAGE までは「優先バケット」（デフォルト 10）
#   - それ以降のページも含めて、最大 NUM_PAGES まで巡回
#
# ・1ツイートにつき:
#   - gofile : 最大 GOFILE_TARGET 本（デフォルト 5）
#   - twimg  : 残りを埋めて合計 WANT_POST 本（bot_orevideo側の env）
#
# ・gofile は投稿前にリンク切れチェック:
#   - まず requests でステータス確認
#   - その後 Playwright(Chromium) で実際にページを開き、
#     Bodyテキストに以下の文言が出ていたらリンク切れ扱い:
#       "This content does not exist"
#       "The content you are looking for could not be found"
#
# ・twimg / gofile ともに短縮なし（生URLのまま）
# ・state.json（posted_urls / recent_urls_24h）で重複を除外
#
# bot_orevideo.py から collect_fresh_gofile_urls() が呼ばれる想定。

import os
import re
import time
from typing import List, Set, Optional, Tuple

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# =========================
#   設定
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

# 1ツイート内で狙う gofile 本数（不足分は twimg で補充）
GOFILE_TARGET = int(os.getenv("GOFILE_TARGET", "5"))

# gofile を「優先」する最大ページ（1ページ目に近いほど新しい想定）
GOFILE_PRIORITY_MAX_PAGE = int(os.getenv("GOFILE_PRIORITY_MAX_PAGE", "10"))

# twimg / gofile 抽出用
TWIMG_RE  = re.compile(r"https?://video\.twimg\.com/[^\s\"']+?\.mp4\?tag=\d+", re.I)
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

# gofile の「コンテンツ無し」判定用フレーズ
GOFILE_NOT_FOUND_MARKERS = [
    "this content does not exist",
    "the content you are looking for could not be found",
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
#   gofile 生存チェック（requests + Playwright）
# =========================

def is_gofile_alive(url: str) -> bool:
    """
    gofile のページに実際にアクセスして、生存判定を行う。

    1. requests でステータス確認
       - 4xx / 5xx → リンク切れ扱い
       - HTML内に「Not Found」系メッセージがあればリンク切れ扱い
    2. 追加で Playwright(Chromium, headless) でページ表示
       - body のテキストに GOFILE_NOT_FOUND_MARKERS のいずれかが入っていればリンク切れ
    """

    # ---- まずは普通の HTTP で軽くチェック ----
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"[warn] gofile(requests) failed: {url} ({e})")
        return False

    if resp.status_code >= 400:
        print(f"[info] gofile status {resp.status_code}: {url}")
        return False

    text_lower = resp.text.lower()
    if any(m in text_lower for m in GOFILE_NOT_FOUND_MARKERS):
        print(f"[info] gofile(not found markers in HTML via requests): {url}")
        return False

    # ---- Playwright で実際のレンダリング結果をチェック ----
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="ja-JP",
                viewport={"width": 1280, "height": 720},
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except PlaywrightTimeoutError as e:
                print(f"[warn] gofile(Playwright) timeout: {url} ({e})")
                ctx.close()
                browser.close()
                return False
            except Exception as e:
                print(f"[warn] gofile(Playwright) goto failed: {url} ({e})")
                ctx.close()
                browser.close()
                return False

            # 少し待ってから body テキストを読む
            page.wait_for_timeout(1500)
            try:
                body_text = page.text_content("body") or ""
            except Exception:
                body_text = page.content() or ""

            body_lower = body_text.lower()
            ctx.close()
            browser.close()

            if any(m in body_lower for m in GOFILE_NOT_FOUND_MARKERS):
                print(f"[info] gofile(not found markers via Playwright): {url}")
                return False

    except Exception as e:
        # Playwright 自体のエラー → 安全側（リンク切れ扱い）に倒す
        print(f"[warn] gofile(Playwright outer) failed: {url} ({e})")
        return False

    # ここまで来たら「生きている」と判定
    return True


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

    # ページ内での重複排除（順序維持）
    def unique(seq: List[str]) -> List[str]:
        seen = set()
        out = []
        for s in seq:
            s = s.strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    tw_u = unique(tw)
    gf_u = unique(gf)

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
    orevideo のページを 1..num_pages まで巡回してリンクを集める。
    戻り値: (twimg_all, gofile_early, gofile_late)
      - gofile_early … page <= GOFILE_PRIORITY_MAX_PAGE の gofile
      - gofile_late  … page >  GOFILE_PRIORITY_MAX_PAGE の gofile

    ページ巡回は 1 → 2 → 3 → ... の順なので、
    gofile_early の先頭ほど「より新しい」URLになる。
    """
    twimg_all: List[str] = []
    gofile_early: List[str] = []
    gofile_late: List[str] = []

    total_raw = 0

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
#   fetch_listing_pages（互換用／実際はあまり使わない）
# =========================

def fetch_listing_pages(
    num_pages: int = 100,
    deadline_ts: Optional[float] = None,
) -> List[str]:
    """
    bot_orevideo 互換用のダミー実装。
    実際の URL 選別は collect_fresh_gofile_urls 側で行うため、
    ここでは twimg + gofile を全部まとめて返すだけ。
    """
    tw, gf_early, gf_late = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)
    all_urls = tw + gf_early + gf_late
    return all_urls[:RAW_LIMIT]


# =========================
#   collect_fresh_gofile_urls（bot_orevideo から呼ばれるメイン）
# =========================

def collect_fresh_gofile_urls(
    already_seen: Set[str],
    want: int = 5,
    num_pages: int = 50,
    deadline_sec: Optional[int] = None,
) -> List[str]:
    """
    orevideo 用の URL 選別ロジック。

    - orevideo から twimg / gofile を収集
    - gofile は page=1 に近いものほど新しい前提で順に採用
      （page <= GOFILE_PRIORITY_MAX_PAGE のものを優先）
    - 1ツイートあたり:
        gofile : 最大 GOFILE_TARGET 本（デフォルト 5）
        twimg  : 残りを埋めて合計 want 本
    - twimg / gofile ともに短縮せず、生URLのまま使う
    - gofile は is_gofile_alive() でリンク切れチェック
    - already_seen / このrun内の seen_now で重複を避ける
    - MIN_POST 未満なら [] を返す（bot_orevideo 側でツイートしない）
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
    tw_all, gf_early, gf_late = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)

    # 目標本数
    go_target = min(GOFILE_TARGET, want)

    results: List[str] = []
    selected_gofile: List[str] = []
    selected_twimg: List[str] = []
    seen_now: Set[str] = set()

    def pick_gofile(raw_url: str) -> Optional[str]:
        if not raw_url:
            return None
        raw_norm = _normalize_url(raw_url)

        # すでに使った / 過去に投稿した URL はスキップ
        if raw_norm in seen_now or raw_norm in already_seen:
            return None

        # リンク切れチェック（requests + Playwright）
        if not is_gofile_alive(raw_url):
            return None

        seen_now.add(raw_norm)
        return raw_url

    def pick_twimg(raw_url: str) -> Optional[str]:
        if not raw_url:
            return None
        raw_norm = _normalize_url(raw_url)

        # すでに使った / 過去に投稿した URL はスキップ
        if raw_norm in seen_now or raw_norm in already_seen:
            return None

        seen_now.add(raw_norm)
        return raw_url

    # 1) gofile (優先ページ 1〜GOFILE_PRIORITY_MAX_PAGE)
    for url in gf_early:
        if len(selected_gofile) >= go_target:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during gofile-early selection; stop.")
            break
        pick = pick_gofile(url)
        if pick:
            selected_gofile.append(pick)

    # 2) gofile (残りはそれ以降のページから、NUM_PAGES まで)
    if len(selected_gofile) < go_target:
        for url in gf_late:
            if len(selected_gofile) >= go_target:
                break
            if _deadline_passed(deadline_ts):
                print("[info] deadline reached during gofile-late selection; stop.")
                break
            pick = pick_gofile(url)
            if pick:
                selected_gofile.append(pick)

    current_go = len(selected_gofile)
    remaining = max(0, want - current_go)

    # 3) twimg （全ページから、残り本数だけ）
    for url in tw_all:
        if len(selected_twimg) >= remaining:
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during twimg selection; stop.")
            break
        pick = pick_twimg(url)
        if pick:
            selected_twimg.append(pick)

    results = selected_gofile + selected_twimg

    print(
        f"[info] orevideo selected: gofile={len(selected_gofile)}, "
        f"twimg={len(selected_twimg)}, total={len(results)} (target={want})"
    )

    # MIN_POST 未満なら「何も無かった扱い」
    if len(results) < min_post:
        print(f"[info] only {len(results)} urls collected (< MIN_POST={min_post}); return [].")
        return []

    return results[:want]