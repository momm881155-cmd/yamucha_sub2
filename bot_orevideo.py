# bot_orevideo.py — orevideo 用（ロジックは元の bot.py と同じ、goxplorer2 を使うだけ）

import json, os, re, time, random
from datetime import datetime, timezone, timedelta
from dateutil import tz
from typing import List
import tweepy
from playwright.sync_api import sync_playwright

from goxplorer2 import collect_fresh_gofile_urls  # ← ここだけ違う

# =========================
#   Amazon アフィリエイトリンク
# =========================

AFFILIATE_URLS: List[str] = [
    "https://amzn.to/3XlQH0F",
    "https://amzn.to/483idoA",
    "https://amzn.to/3K4XlVQ",
    "https://amzn.to/4okLzoB",
    "https://amzn.to/448R2Y5",
    "https://amzn.to/48lbyqY",
    "https://amzn.to/47MpOsL",
    "https://amzn.to/4oKKGX5",
    "https://amzn.to/4oQRFhm",
    "https://amzn.to/43xl8Ev",
    "https://amzn.to/4r6DshP",
    "https://amzn.to/3XFETpP",
    "https://amzn.to/4pepNn5",
    "https://amzn.to/4r1tuyp",
    "https://amzn.to/487dSAN",
    "https://amzn.to/4i72lWG",
    "https://amzn.to/4i3iyfj",
    "https://amzn.to/4874X25",
    "https://amzn.to/49pCoiF",
    "https://amzn.to/4plPKkR",
    "https://amzn.to/4pekiVk",
]

# =========================
#   セリフ一覧（30字以下版）
# =========================

_SERIF_SOURCE = """
このタイミングで拾ってきた！,今ちょうど手に入れたやつだ！,さっき拾ってきたブツだ！,ちょい前に集めた分だ！,このへんで確保してきた！,今の成果ってとこだな！,いま仕入れてきたセットだ！,直前に漁ってきたブツだ！,このあたりで拾ってきた！,ついさっきゲットしてきた！,ちょうど入手したやつだ！,今持ってきた仕込みだ！,さっきストックしてきた！,いま確保したやつだ！,最近かき集めた分だ！,今んとこのラインナップ！,ちょうど用意した分だ！,たった今拾ってきた！,さっき運んできたぶんだ！,今の仕入れ品ってわけだ！,このところ集めたやつ！,さっき選んできたやつだ！,今入手できたやつだ！,ここしばらくの収穫だ！,直前まで探してきた！,ちょい前に回収してきた！,いま仕上げたまとめだ！,この時点で拾えたぶんだ！,今捕まえてきたやつだ！,最近の収穫を持ってきた！,今持って来れた分だ！,ちょうど揃えた材料だ！,この流れで拾ってきた！,さっきバッグにつめた！,ちょい前の分を出すぜ！,間に合ったやつだ！,手元にあったやつだ！,最近集めたまとめだ！,今の収穫ぶんだ！,さっき拾った素材だ！,ついこないだの分だ！,いま追加で拾った！,ここまでのブツだ！,この瞬間拾えた分だ！,さっきの成果だ！,手元のぶんだけ持参だ！,最近寄せ集めたやつ！,今仕入れた分だ！,このへんで拾ったやつ！,さっき引っ張ってきた！,今ちょいと集めてきた！,このへんで拾ったネタだ！,さっき仕入れた素材だ！,いま届けに来たぶんだ！,最近のピック品だ！,さっき運んだアイテムだ！,今あるぶん全部だ！,この状況で集めた分だ！,今ちょうど拾ったやつ！,漁って見つけたやつだ！,直前に調達した分だ！,今運べたセットだ！,今ストックのぶんだ！,最近拾ったアイテムだ！,この頃手に入れたやつ！,さっき確保したやつだ！,今すぐ持ってきたぶん！,さっき集めた分だ！,直前に集めたブツだ！,最近の仕入れ品だ！,ここで確保したやつだ！,たった今集めたセットだ！,いまバッグの中のぶんだ！,さっきチョイスした！,最近確保した成果だ！,ちょい前の回収品だ！,今いける範囲で拾った！,この流れで集めた素材！,最近ガサ入れした成果だ！,ここで集まったぶんだ！,ちょうど拾えた分だ！,今の状況で入手した！,最近調達したぶんだ！,いま仕留めてきた！,このへんで確保した分！,さっき仕上げた素材だ！,今そろったやつだ！,最近手に入れたぶん！,直前に捕まえた分！,いま追加で確保した！,この流れで集めたやつ！,最近仕込んだアイテムだ！,今確保したラインだ！,この瞬間拾ったぶん！,さっき回収した素材だ！,今準備できたぶん！,このへんで回収した分！,最近のパック品だ！,さっき詰め込んだ分だ！,今ある分全部だ！,このタイミングの品だ！,最近の収集ぶんだ！,今間に合ったやつ！,このへんで集めた素材！,ちょい前から集めた分！,急いで拾ってきた！,このとき仕入れたやつ！,最近まとめたぶん！,さっき整えたやつだ！,今できた回収セット！,この場で見つけた成果だ！,いま配置したネタだ！,最近のコンプ品だ！,ちょうど集まった分だ！,直前の収穫だ！,今そろったぶんだ！,この限りで拾った分！,最近漁ってきた素材！,いま調達してきた！,ここで揃えたやつだ！,さっき拾った素材だ！,今の仕入れ分だ！,このエリアで拾った分！,最近の材料だ！,いま運び出したセット！,この状態で集まった！,今捕まえたアイテム！,最近ゲットした素材！,さっき拾ったやつ！,今間に合ったセット！,この流れの収穫だ！,最近整えておいた！,いま詰めてきたやつだ！,ここいらの拾い物だ！,直前に集めたパックだ！,今の材料だ！,この頃拾った戦利品！,最近の回収ログだ！,今揃ったぶんだ！,このステップで拾った！,さっきの仕入れ分だ！,今調達した結果だ！,この一帯で見つけた！,最近の収穫物だ！,今キャッチしたやつだ！,この状態で集めた！,最近持ってきたぶんだ！,さっき拾い上げた！,今フィールドで確保！,このへんで集めたブツ！,最近ガッと集めた！,いま回収ミッション帰り！,このミッションの成果だ！,さっきゲットした分だ！,今まとめて持ち帰った！,この近辺で拾った！,最近仕入れたてだ！,今そろった素材だ！,この界隈で見つけた！,さっきまで集めた分！,いま回収してきた！,このチャンスで拾った！,最近ゲットしたまとめ！,いま届いたアイテム！,この間に合った素材！,最近寄せ集めたぶん！,ここで拾えた成果だ！,さっきの回収ぶんだ！,いまある素材まとめだ！,この場で拾ったアイテム！,最近完成したセット！,ここで集まった素材！
"""

SERIF_LIST: List[str] = [
    s.strip() for s in _SERIF_SOURCE.split(",") if s.strip()
]

STATE_FILE = "state.json"
DAILY_LIMIT = 16
JST = tz.gettz("Asia/Tokyo")
TWEET_LIMIT = 280
TCO_URL_LEN = 23
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

ZWSP = "\u200B"; ZWNJ = "\u200C"; INVISIBLES = [ZWSP, ZWNJ]

def _env_int(key, default):
    try:
        return int(os.getenv(key, str(default)))
    except:
        return default

WANT_POST = _env_int("WANT_POST", 5)
MIN_POST  = _env_int("MIN_POST", 3)
HARD_LIMIT_SEC = _env_int("HARD_LIMIT_SEC", 600)
USE_API_TIMELINE = _env_int("USE_API_TIMELINE", 0)

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
    today = now_jst.date().isoformat()
    if state.get("last_post_date") != today:
        state["last_post_date"] = today
        state["posts_today"] = 0

def purge_recent_12h(state, now_utc):
    cutoff = now_utc - timedelta(hours=12)
    buf = []
    for item in state.get("recent_urls_24h", []):
        try:
            ts = datetime.fromisoformat(item.get("ts"))
            if ts >= cutoff:
                buf.append(item)
        except:
            pass
    state["recent_urls_24h"] = buf

def normalize_url(u):
    if not u:
        return u
    u = u.strip()
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    return u.rstrip("/")

def build_seen_set_from_state(state):
    seen = set()
    for u in state.get("posted_urls", []):
        seen.add(normalize_url(u))
    for it in state.get("recent_urls_24h", []):
        seen.add(normalize_url(it.get("url")))
    return seen

def estimate_tweet_len_tco(text: str) -> int:
    def repl(m): return "U" * TCO_URL_LEN
    return len(re.sub(r"https?://\S+", repl, text))

def compose_fixed5_text(
    gofile_urls,
    start_seq: int,
    salt_idx: int = 0,
    add_sig: bool = True,
):
    """
    1ツイートの形:
      セリフ1行
      1. URL1
      AFF1
      2. URL2
      AFF2
      ...
    ・URL 本数 = WANT_POST (最大)
    ・Amazon 本数 = URL本数 - 1
    ・アフィリンク & セリフは毎回ランダム（ツイート内で被りなし）
    """
    urls = gofile_urls[:WANT_POST]
    if not urls:
        return "", 0

    invis = INVISIBLES[salt_idx % len(INVISIBLES)]

    # セリフ1つランダム
    serif = random.choice(SERIF_LIST) if SERIF_LIST else ""

    # このツイートで必要な Amazon 本数 = URL 本数 - 1
    need_aff = max(0, len(urls) - 1)
    if need_aff > 0 and AFFILIATE_URLS:
        aff_list = random.sample(AFFILIATE_URLS, k=min(need_aff, len(AFFILIATE_URLS)))
    else:
        aff_list = []

    lines: List[str] = []
    if serif:
        lines.append(serif)

    seq = start_seq
    aff_idx = 0

    for i, u in enumerate(urls):
        lines.append(f"{seq}{invis}. {u}")
        seq += 1

        if i < len(urls) - 1 and aff_idx < len(aff_list):
            lines.append(aff_list[aff_idx])
            aff_idx += 1

    text = "\n".join(lines)

    if add_sig:
        seed = (start_seq * 1315423911) ^ int(time.time() // 60)
        sig = "".join(INVISIBLES[(seed >> i) & 1] for i in range(16))
        text += sig

    return text, len(urls)

def get_client():
    return tweepy.Client(
        bearer_token=None,
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
        wait_on_rate_limit=bool(_env_int("WAIT_ON_RATE_LIMIT", 0)),
    )

def fetch_recent_urls_via_web(username: str, scrolls: int = 1, wait_ms: int = 800) -> set:
    if not username:
        return set()
    url = f"https://x.com/{username}"
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/123.0.0.0"
            ),
            locale="ja-JP",
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(wait_ms)
        for _ in range(scrolls):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(wait_ms)
        html = page.content()
        ctx.close()
        browser.close()
    for m in GOFILE_RE.findall(html):
        seen.add(normalize_url(m))
    return seen

def post_to_x_v2(client, text: str):
    return client.create_tweet(text=text)

def main():
    start_ts = time.monotonic()
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)

    state = load_state()
    purge_recent_12h(state, now_utc)
    reset_if_new_day(state, now_jst)

    if state.get("posts_today", 0) >= DAILY_LIMIT:
        print("Daily limit reached; skip.")
        return

    already_seen = build_seen_set_from_state(state)

    if USE_API_TIMELINE:
        try:
            client = get_client()
            me = client.get_me(user_auth=True)
            user = me.data if me and me.data else None
            username = getattr(user, "username", None)
        except Exception:
            username = os.getenv("X_SCREEN_NAME", None)
        web_seen = fetch_recent_urls_via_web(username, scrolls=1, wait_ms=800) if username else set()
        if web_seen:
            already_seen |= web_seen
        print(f"[info] recent timeline gofiles via WEB (opt): {len(web_seen)} (user={username})")
    else:
        print("[info] timeline check skipped (USE_API_TIMELINE=0)")

    if (time.monotonic() - start_ts) > HARD_LIMIT_SEC:
        print("[warn] time budget exceeded before collection; abort.")
        return

    try:
        deadline_env = os.getenv("SCRAPE_TIMEOUT_SEC")
        deadline_sec = int(deadline_env) if deadline_env else None
    except Exception:
        deadline_sec = None

    urls = collect_fresh_gofile_urls(
        already_seen=already_seen,
        want=WANT_POST,
        num_pages=int(os.getenv("NUM_PAGES", "50")),
        deadline_sec=deadline_sec,
    )
    print(f"[info] collected alive urls: {len(urls)}")
    if len(urls) < MIN_POST:
        print("Not enough alive URLs; skip.")
        return

    start_seq = int(state.get("line_seq", 1))
    salt = (now_jst.hour + now_jst.minute) % len(INVISIBLES)
    status_text, taken = compose_fixed5_text(
        urls,
        start_seq=start_seq,
        salt_idx=salt,
        add_sig=True,
    )

    if estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
        status_text = status_text.replace(". https://", ".https://")
    while estimate_tweet_len_tco(status_text) > TWEET_LIMIT:
        status_text = status_text.rstrip(ZWSP + ZWNJ)

    client = get_client()
    resp = post_to_x_v2(client, status_text)
    tweet_id = resp.data.get("id") if resp and resp.data else None
    print(f"[info] tweeted id={tweet_id}")

    for u in urls[:WANT_POST]:
        if u not in state["posted_urls"]:
            state["posted_urls"].append(u)
        state["recent_urls_24h"].append({"url": u, "ts": now_utc.isoformat()})
    state["posts_today"] = state.get("posts_today", 0) + 1
    state["line_seq"] = start_seq + min(WANT_POST, len(urls))
    save_state(state)

    used_urls = min(WANT_POST, len(urls))
    used_aff  = max(0, used_urls - 1)
    print(f"Posted ({used_urls} urls + {used_aff} amazon):", status_text)

if __name__ == "__main__":
    main()