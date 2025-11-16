# goxplorer2.py — orevideo 専用スクレイパ（生URL版 / gofile生存チェック付き）
#
# ・https://orevideo.pythonanywhere.com/?sort=newest&page=N から
#   - https://video.twimg.com/...mp4?tag=xx  （twimg）
#   - https://gofile.io/d/XXXXXX             （gofile）
#   を収集
#
# ・ロジック概要
#   1. orevideo から twimg / gofile をまとめて収集
#   2. gofile は state.json 由来 already_seen で重複を事前に除外
#   3. 残った gofile のうち最大 MAX_GOFILE_CHECKS_PER_RUN 本だけ生存確認
#        - 生きてる gofile が GOFILE_TARGET 本 集まったら即打ち切り
#   4. gofile が GOFILE_TARGET 未満なら、残りは twimg で埋めて WANT_POST 本にする
#   5. twimg / gofile ともに生URLのまま bot.py に渡す（短縮しない）
#
# ・環境変数
#   RAW_LIMIT                  ... orevideo から拾う最大URL数（twimg+gofile）
#   FILTER_LIMIT               ... （今回は主に内部計算用）
#   NUM_PAGES                  ... orevideo を何ページまで見るか（デフォ 50）
#   GOFILE_TARGET              ... 1ツイート中の gofile 最大本数（デフォ 5）
#   MAX_GOFILE_CHECKS_PER_RUN  ... 1run中に「生存確認リクエスト」を行う最大本数（デフォ 15）
#   MIN_POST                   ... これ未満なら [] を返してツイートしない
#   SCRAPE_TIMEOUT_SEC         ... 全体の締切（秒）
#
# ・bot.py 側からは collect_fresh_gofile_urls() が呼ばれる想定。

import os
import re
import time
from typing import List, Set, Optional, Tuple

import requests

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

# gofile を狙う最大本数（デフォ 5 = WANT_POST と同じ想定）
GOFILE_TARGET = int(os.getenv("GOFILE_TARGET", "5"))

# 1 run で「生存確認(API叩く)」を行う上限本数
MAX_GOFILE_CHECKS_PER_RUN = int(os.getenv("MAX_GOFILE_CHECKS_PER_RUN", "15"))

# twimg / gofile 抽出用
TWIMG_RE  = re.compile(r"https?://video\.twimg\.com/[^\s\"']+?\.mp4\?tag=\d+", re.I)
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)


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
) -> Tuple[List[str], List[str]]:
    """
    orevideo のページを 1..num_pages まで巡回してリンクを集める。
    戻り値: (twimg_all, gofile_all)
    """
    twimg_all: List[str] = []
    gofile_all: List[str] = []

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
        gofile_all.extend(gf_list)

        total_raw = len(twimg_all) + len(gofile_all)
        if total_raw >= RAW_LIMIT:
            print(f"[info] orevideo early stop at RAW_LIMIT={RAW_LIMIT}")
            break

        time.sleep(0.3)

    return twimg_all, gofile_all


# =========================
#   gofile 生存チェック（requests）
# =========================

def check_gofile_alive(url: str) -> bool | None:
    """
    gofile のURLが生きているかざっくり判定する。

    戻り値:
      True  = 生きてそう
      False = 明らかに死んでいる（404/410/「This content does not exist」など）
      None  = レート制限(429) or よく分からない失敗 → このURLは採用しないが、致命的エラー扱いもしない
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        print(f"[warn] gofile(requests) failed: {url} ({e})")
        return None

    if r.status_code == 429:
        print(f"[info] gofile status 429: {url}")
        return None

    if r.status_code in (404, 410):
        print(f"[info] gofile status {r.status_code}: {url}")
        return False

    # ページ本文に "This content does not exist" が含まれていれば死亡扱い
    if "This content does not exist" in r.text:
        print(f"[info] gofile(not found markers via requests): {url}")
        return False

    # 200 などでエラー文言も無ければ「生きてるっぽい」とする
    return True


# =========================
#   fetch_listing_pages（bot.py 互換用）
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
    tw, gf = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)
    all_urls = tw + gf
    return all_urls[:RAW_LIMIT]


# =========================
#   collect_fresh_gofile_urls（bot.py から呼ばれるメイン）
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
    - gofile は state.json 由来の already_seen で「生存確認前に」重複除外
    - その gofile 候補のうち、最大 MAX_GOFILE_CHECKS_PER_RUN 本だけ生存確認
      ・生きてる gofile が GOFILE_TARGET 本 集まったら即打ち切り
    - 5本未満で終わったら、残りは twimg で埋めて合計 want 本にする
    - twimg / gofile ともに生URLのまま返す（短縮しない）
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
    tw_all_raw, gf_all_raw = _collect_orevideo_links(num_pages=num_pages, deadline_ts=deadline_ts)

    # =========================
    #  1. gofile 候補: まず JSON 重複で絞る
    # =========================

    # すでに seen に入っているURLは「生存確認もしない」
    normalized_already_seen = { _normalize_url(u) for u in already_seen }

    candidate_gofile: List[str] = []
    seen_raw_gf: Set[str] = set()

    for url in gf_all_raw:
        n = _normalize_url(url)
        if not n:
            continue
        if n in seen_raw_gf:
            continue
        if n in normalized_already_seen:
            # すでに state.json で使った or 最近使ったもの → スキップ
            continue
        seen_raw_gf.add(n)
        candidate_gofile.append(url)

    # =========================
    #  2. gofile の生存確認（最大 N本まで）
    # =========================

    go_target = min(GOFILE_TARGET, want)
    selected_gofile: List[str] = []
    selected_twimg: List[str] = []
    seen_now: Set[str] = set()  # この run 内で新たに採用したURL

    gofile_checks = 0

    for raw_url in candidate_gofile:
        if len(selected_gofile) >= go_target:
            # 目標本数に到達したら即終了
            break
        if gofile_checks >= MAX_GOFILE_CHECKS_PER_RUN:
            # 429 を避けるため、生存確認回数に上限
            print(f"[info] reached MAX_GOFILE_CHECKS_PER_RUN={MAX_GOFILE_CHECKS_PER_RUN}; stop gofile checks.")
            break
        if _deadline_passed(deadline_ts):
            print("[info] deadline reached during gofile checks; stop.")
            break

        raw_norm = _normalize_url(raw_url)
        if raw_norm in seen_now or raw_norm in normalized_already_seen:
            continue

        gofile_checks += 1
        alive = check_gofile_alive(raw_url)

        if alive is False:
            # 死亡確定 → 採用せず次へ
            continue
        if alive is None:
            # 429 やその他の怪しいエラー → このURLはスキップ
            continue

        # alive True → 採用
        seen_now.add(raw_norm)
        selected_gofile.append(raw_url)

        if len(selected_gofile) >= go_target:
            break

    # =========================
    #  3. twimg で残りを埋める
    # =========================

    remaining = max(0, want - len(selected_gofile))

    if remaining > 0:
        for raw_url in tw_all_raw:
            if len(selected_twimg) >= remaining:
                break
            if _deadline_passed(deadline_ts):
                print("[info] deadline reached during twimg selection; stop.")
                break

            raw_norm = _normalize_url(raw_url)
            if raw_norm in seen_now or raw_norm in normalized_already_seen:
                continue

            seen_now.add(raw_norm)
            selected_twimg.append(raw_url)

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