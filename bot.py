# bot.py — 即投稿モード（5件そろい次第ツイート＆終了）
# ・各サイトごとに起動。5件集まれば即投稿 → 終了
# ・1日16投稿まで（state.json管理）
# ・本文は Gofile 5件 + Amazonリンク4件を交互
# ・収集/死活判定ロジックは goxplorer.py 側に依存

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from dateutil import tz
import tweepy
from playwright.sync_api import sync_playwright

from goxplorer import collect_fresh_gofile_urls, is_gofile_alive

# ===== 設定 =====
AFFILIATE_URL = "https://amzn.to/3Kq0QGm"
STATE_FILE = "state.json"
DAILY_LIMIT = 16
JST = tz.gettz("Asia/Tokyo")
TWEET_LIMIT = 280
TCO_URL_LEN = 23
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

# 不可視（重複回避の最小署名）
ZWSP = "\u200B"
ZWNJ = "\u200C"
INVISIBLES = [ZWSP, ZWNJ]

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

# 実行時間の上限（ウォッチドッグ）
HARD_LIMIT_SEC = int(os.getenv("HARD_LIMIT_SEC", "240"))  # デフォルト4分

# ===== state =====
def _default_state():
    return {
        "posted_urls": [],
        "last_post_date": None,
        "posts_today": 0,
        "recent_urls_24h": [],
        "line_seq": 1,
    }

def load_state():
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = _default_state()
    for k, v in _default_state().items():
        if k not in data:
            data[k] = v
    return data

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def reset_if_new_day(state, now_jst):
    today_str = now_jst.date().isoformat()
    if state.get("last_post_date") != today_str:
        state["last_post_date"] = today_str
        state["posts_today"] = 0

def within_posting_window(now_jst):
    return True

def can_post_more_today(state):
    return state.get("posts_today", 0) < DAILY_LIMIT

# ====== 12時間保持 ======
def purge_recent_12h(state, now_utc: datetime):
    cutoff = now_utc - timedelta(hours=12)
    buf = []
    for item in state.get("recent_urls_24h", []):
        try:
            ts = datetime.fromisoformat(item.get("ts"))
        except Exception:
            continue
        if ts >= cutoff:
            buf.append(item)
    state["recent_urls_24h"] = buf

# ===== 正規化＆除外集合 =====
def normalize_url(u: str) -> str:
    if not u:
        return u
    u = u.strip()
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    u = u.rstrip("/")
    return u

def build_seen_set_from_state(state) -> set:
    seen = set()
    for u in state.get("posted_urls", []):
        seen.add(normalize_url(u))
    for item in state.get("recent_urls_24h", []):
        seen.add(normalize_url(item.get("url")))
    return seen

# ===== ユーティリティ =====
def estimate_tweet_len_tco(text: str) -> int:
    def repl(m): return "U" * TCO_URL_LEN
    replaced = re.sub(r"https?://\S+", repl, text)
    return len(replaced)

def is_alive_retry(url: str, retries: int = 1, delay_sec: float = 0.5) -> bool:
    for i in range(retries + 1):
        if is_gofile_alive(url):
            return True
        if i < retries:
            time.sleep(delay_sec)
    return False

# ===== ツイート本文生成 =====
def compose_fixed5_text(gofile_urls, start_seq: int, salt_idx: int = 0, add_sig: bool = True):
    invis = INVISIBLES[salt_idx % len(INVISIBLES)]
    lines = []
    seq = start_seq
    take = min(5, len(gofile_urls))
    sel = gofile_urls[:take]
    for i, u in enumerate(sel):
        lines.append(f"{seq}{invis}. {u}")
        if i < take - 1:
            lines.append(AFFILIATE_URL)
        seq += 1
    text = "\n".join(lines)
    if add_sig:
        seed = (start_seq * 1315423911) ^ int(time.time() // 60)
        sig = "".join(INVISIBLES[(seed >> i) & 1] for i in range(16))
        text = text + sig
    return text, take

# ===== X API =====
def get_client():
    wait_flag = _env_bool("WAIT_ON_RATE_LIMIT", False)
    client = tweepy.Client(
        bearer_token=None,
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=wait_flag,
    )
    return client

def fetch_recent_urls_via_api(client, max_tweets=100) -> tuple[set, str | None]:
    seen = set()
    me = client.get_me(user_auth=True)
    user = me.data if me and me.data else None
    if not user:
        return seen, None
    user_id = user.id
    username = getattr(user, "username", None)
    resp = client.get_users_tweets(
        id=user_id,
        max_results=min(max_tweets, 100),
        tweet_fields=["entities", "text"],
        exclude=["retweets", "replies"]
    )
    if resp and resp.data:
        for t in resp.data:
            text = t.text or ""
            for m in GOFILE_RE.findall(text):
                seen.add(normalize_url(m))
            ent = getattr(t, "entities", None)
            if ent and "urls" in ent and ent["urls"]:
                for u in ent["urls"]:
                    for key in ("expanded_url", "unwound_url", "display_url", "url"):
                        val = u.get(key)
                        if isinstance(val, str) and "gofile.io/d/" in val:
                            for mm in GOFILE_RE.findall(val):
                                seen.add(normalize_url(mm))
    return seen, username

def fetch_recent_urls_via_web(username: str, scrolls: int = 3, wait_ms: int = 1000) -> set:
    if not username:
        return set()
    url = f"https://x.com/{username}"
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0.0.0 Safari/123.0.0.0"),
            locale="ja-JP"
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait_ms)
        for _ in range(scrolls):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(wait_ms)
        html = page.content()
        context.close()
        browser.close()
    for m in GOFILE_RE.findall(html):
        seen.add(normalize_url(m))
    return seen

def post_to_x_v2(client, status_text: str):
    return client.create_tweet(text=status_text)

# ===== main =====
def main():
    start_ts = time.monotonic()

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)

    state = load_state()
    purge_recent_12h(state, now_utc)
    reset_if_new_day(state, now_jst)

    if not within_posting_window(now_jst):
        print("Not within posting window; skip.")
        return
    if not can_post_more_today(state):
        print("Daily limit reached; skip.")
        return

    already_seen = build_seen_set_from_state(state)

    # --- タイムライン重複チェック：API or WEB 強制 ---
    use_api_tl = _env_bool("USE_API_TIMELINE", False)
    username = os.getenv("X_SCREEN_NAME") or None
    timeline_seen = set()
    if use_api_tl:
        try:
            client = get_client()
            timeline_seen, api_user = fetch_recent_urls_via_api(client, max_tweets=100)
            print(f"[info] recent timeline gofiles via API: {len(timeline_seen)} (user={api_user})")
        except Exception as e:
            print(f"[warn] API timeline failed ({e}); fallback to WEB")
            web_seen = fetch_recent_urls_via_web(username=username, scrolls=3, wait_ms=1000) if username else set()
            timeline_seen = web_seen
            print(f"[info] recent timeline gofiles via WEB (fallback): {len(timeline_seen)} (user={username})")
    else:
        web_seen = fetch_recent_urls_via_web(username=username, scrolls=3, wait_ms=1000) if username else set()
        timeline_seen = web_seen
        print(f"[info] recent timeline gofiles via WEB (forced): {len(timeline_seen)} (user={username})")

    if timeline_seen:
        already_seen |= timeline_seen

    # --- 収集：want=5 で揃い次第 return（goxplorer側が早期終了）---
    try:
        deadline_env = os.getenv("SCRAPE_TIMEOUT_SEC")
        deadline_sec = int(deadline_env) if deadline_env else None
    except Exception:
        deadline_sec = None

    candidates = collect_fresh_gofile_urls(
        already_seen=already_seen,
        want=5,  # ★ 即投稿モード
        num_pages=int(os.getenv("NUM_PAGES", "100")),
        deadline_sec=deadline_sec
    )
    print(f"[info] collected candidates: {len(candidates)}")
    if len(candidates) < 5:
        print("Not enough fresh URLs found; skip.")
        save_state(state)
        return

    # --- 最終プレフライト（ごく軽い再チェック）---
    target = 5
    tested = set()
    preflight = []

    def add_if_alive(u: str):
        if time.monotonic() - start_ts > HARD_LIMIT_SEC:
            return False
        n = normalize_url(u)
        if n in tested or n in already_seen or n in preflight:
            return False
        tested.add(n)
        if is_alive_retry(n, retries=1, delay_sec=0.3):
            preflight.append(n)
            return True
        return False

    for u in candidates:
        if len(preflight) >= target or (time.monotonic() - start_ts) > HARD_LIMIT_SEC:
            break
        add_if_alive(u)

    if len(preflight) < target:
        print("Final preflight could not assemble 5 URLs; skip.")
        save_state(state)
        return

    # --- 投稿 ---
    client = get_client()
    start_seq = int(state.get("line_seq", 1))
    salt = (now_jst.hour + now_jst.minute) % len(INVISIBLES)
    status_text, taken = compose_fixed5_text(preflight, start_seq=start_seq, salt_idx=salt, add_sig=True)

    if estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
        status_text = status_text.replace(". https://", ".https://")
    while estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
        status_text = status_text.rstrip(ZWSP + ZWNJ)

    for attempt in range(3):
        try:
            resp = post_to_x_v2(client, status_text)
            tweet_id = resp.data.get("id") if resp and resp.data else None
            print(f"[info] tweeted id={tweet_id}")

            for u in preflight[:5]:
                if u not in state["posted_urls"]:
                    state["posted_urls"].append(u)
                state["recent_urls_24h"].append({"url": u, "ts": now_utc.isoformat()})
            state["posts_today"] = state.get("posts_today", 0) + 1
            state["line_seq"] = start_seq + taken
            save_state(state)
            print(f"Posted (5 gofiles + 4 amazon):", status_text)
            return

        except tweepy.Forbidden as e:
            body = ""
            try:
                body = e.response.json()
            except Exception:
                body = str(e)
            s = str(body).lower()
            if "duplicate content" in s:
                salt = (salt + 1) % len(INVISIBLES)
                status_text, taken = compose_fixed5_text(preflight, start_seq=start_seq, salt_idx=salt, add_sig=True)
                if estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
                    status_text = status_text.replace(". https://", ".https://")
                while estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
                    status_text = status_text.rstrip(ZWSP + ZWNJ)
                print("[warn] duplicate content; retry with new invisible salt.")
                time.sleep(1.0)
                continue
            else:
                print(f"[error] Forbidden: {e}")
                raise
        except Exception as e:
            print(f"[error] create_tweet failed: {e}")
            raise

if __name__ == "__main__":
    main()
