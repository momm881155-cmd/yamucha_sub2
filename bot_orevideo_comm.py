# bot_orevideo_comm.py — orevideo の twimg だけをコミュニティ用に 1日3回ポスト
#
# ・https://orevideo.pythonanywhere.com/?sort=newest&page=N から
#   https://video.twimg.com/...mp4?tag=xx  （twimg 生URL）だけを収集
# ・1回の実行で「新しめの twimg を 3本」選んで:
#     1. url
#     2. url
#     3. url
#   というテキストをポスト
# ・アフィリンクなし
# ・state_orevideo_comm.json に「コミュニティ用で使ったURL」を保存して重複防止
# ・X コミュニティに投稿したい場合は、環境変数 COMMUNITY_ID を設定
#   → client.create_tweet(text=..., community_id=COMMUNITY_ID)

import os
import re
import time
import json
from datetime import datetime, timezone
from typing import List, Set, Optional

import requests
import tweepy

# =========================
#   共通設定
# =========================

ORE_BASE = os.getenv("OREVIDEO_BASE", "https://orevideo.pythonanywhere.com").rstrip("/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/123.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": ORE_BASE,
    "Connection": "keep-alive",
}

STATE_FILE = "state_orevideo_comm.json"

# orevideo 側のページ巡回パラメータ
RAW_LIMIT    = int(os.getenv("RAW_LIMIT", "200"))
NUM_PAGES    = int(os.getenv("NUM_PAGES", "50"))

# 1回のポストで欲しい本数
WANT_POST = 3
MIN_POST  = 3  # 3本揃わなければスキップ

# twimg 抽出用
TWIMG_RE = re.compile(
    r"https?://video\.twimg\.com/[^\s\"']+?\.mp4\?tag=\d+",
    re.I,
)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default


def _now_monotonic() -> float:
    return time.monotonic()


def _deadline_passed(deadline_ts: Optional[float]) -> bool:
    return deadline_ts is not None and _now_monotonic() >= deadline_ts


def _normalize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")


# =========================
#   state_orevideo_comm.json
# =========================

def _default_state():
    return {
        "posted_urls": [],          # これまでコミュニティに投げた twimg URL
        "last_post_ts": None,       # 最終投稿UTC
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = _default_state()
    base = _default_state()
    for k, v in base.items():
        if k not in data:
            data[k] = v
    return data


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_seen_set(state) -> Set[str]:
    seen = set()
    for u in state.get("posted_urls", []):
        seen.add(_normalize_url(u))
    return seen


# =========================
#   X(Twitter / X Community)
# =========================

def get_client():
    return tweepy.Client(
        bearer_token=None,
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=bool(_env_int("WAIT_ON_RATE_LIMIT", 0)),
    )


def post_to_x(text: str):
    client = get_client()
    community_id = os.getenv("COMMUNITY_ID", "").strip()

    if community_id:
        # コミュニティ指定あり
        resp = client.create_tweet(text=text, community_id=community_id)
    else:
        # 通常ツイート
        resp = client.create_tweet(text=text)

    tweet_id = resp.data.get("id") if resp and resp.data else None
    print(f"[info] tweeted id={tweet_id}")
    return tweet_id


# =========================
#   orevideo から twimg 収集
# =========================

def extract_twimg_from_html(html: str) -> List[str]:
    if not html:
        return []
    found = TWIMG_RE.findall(html)

    # 重複排除（順序維持）
    seen = set()
    out: List[str] = []
    for u in found:
        u = u.strip()
        if not u:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def collect_twimg_candidates(
    already_seen: Set[str],
    want: int,
    num_pages: int,
    deadline_sec: Optional[int],
) -> List[str]:
    """
    orevideo を新しいページ順に巡回しながら、
    まだ使っていない twimg URL を want 本だけ拾う。
    """
    # デッドライン
    deadline_ts = None
    if deadline_sec:
        deadline_ts = _now_monotonic() + deadline_sec

    results: List[str] = []
    seen_now: Set[str] = set()

    for p in range(1, num_pages + 1):
        if _deadline_passed(deadline_ts):
            print(f"[info] orevideo twimg deadline at page={p}; stop.")
            break

        if p == 1:
            url = f"{ORE_BASE}/?sort=newest&page=1"
        else:
            url = f"{ORE_BASE}/?page={p}&sort=newest"

        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"[warn] orevideo request failed: {url} ({e})")
            continue

        if resp.status_code != 200:
            print(f"[warn] orevideo status {resp.status_code}: {url}")
            continue

        html = resp.text
        page_tw = extract_twimg_from_html(html)
        print(f"[info] orevideo twimg page={p}: {len(page_tw)} links")

        for raw in page_tw:
            if len(results) >= want:
                break

            norm = _normalize_url(raw)
            if norm in already_seen or norm in seen_now:
                continue

            seen_now.add(norm)
            results.append(norm)

        if len(results) >= want:
            break

        # RAW_LIMIT を超えるほど拾う可能性は低いが一応ガード
        if len(seen_now) >= RAW_LIMIT:
            print(f"[info] orevideo twimg early stop at RAW_LIMIT={RAW_LIMIT}")
            break

        time.sleep(0.2)

    print(f"[info] twimg candidates collected: {len(results)}")
    return results


# =========================
#   メイン
# =========================

def main():
    start_ts = _now_monotonic()
    now_utc = datetime.now(timezone.utc)

    # 締切（環境変数 SCRAPE_TIMEOUT_SEC に合わせる）
    try:
        deadline_sec = int(os.getenv("SCRAPE_TIMEOUT_SEC", "240"))
    except Exception:
        deadline_sec = None

    state = load_state()
    already_seen = build_seen_set(state)

    # twimg 候補を集める
    twimg_urls = collect_twimg_candidates(
        already_seen=already_seen,
        want=WANT_POST,
        num_pages=NUM_PAGES,
        deadline_sec=deadline_sec,
    )

    if len(twimg_urls) < MIN_POST:
        print(f"[info] only {len(twimg_urls)} twimg URLs (< MIN_POST={MIN_POST}); skip tweet.")
        return

    # 3本だけに切り詰め
    twimg_urls = twimg_urls[:WANT_POST]

    # テキスト組み立て（シンプルに 1〜3 行）
    lines = []
    for idx, u in enumerate(twimg_urls, start=1):
        lines.append(f"{idx}. {u}")
    text = "\n".join(lines)

    # 投稿
    elapsed = _now_monotonic() - start_ts
    print(f"[info] ready to tweet (elapsed={elapsed:.1f}s):\n{text}")
    tweet_id = post_to_x(text)

    # state 更新（投稿に使った URL を保存）
    if tweet_id:
        for u in twimg_urls:
            if u not in state["posted_urls"]:
                state["posted_urls"].append(u)
        state["last_post_ts"] = now_utc.isoformat()
        save_state(state)
        print(f"[info] state_orevideo_comm updated: +{len(twimg_urls)} urls")


if __name__ == "__main__":
    main()