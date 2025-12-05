# bot_orevideo.py — orevideo 用（ロジックは元の bot.py と同じ、goxplorer2 を使うだけ）

import json, os, re, time, random
from datetime import datetime, timezone, timedelta
from dateutil import tz
from typing import List
import tweepy
from playwright.sync_api import sync_playwright

from goxplorer2 import collect_fresh_gofile_urls, mark_sheet_posted  # ← ここだけ増やした

import requests
try:
    from requests_oauthlib import OAuth1
except ImportError:
    OAuth1 = None

# =========================
#   Amazon アフィリエイトリンク
# =========================

AFFILIATE_URLS: List[str] = [
    "https://www.amazon.co.jp/Sunytree-【2025アップグレード版・大型6枚刃】けだまとり-液晶ディスプレイ付き-Type-C充電式-日本語取扱説明書付き/dp/B0FKRXZG62?ref=dlx_deals_dg_dcl_B0FKRXZG62_mw_sl13_ed_pi&pf_rd_r=VZEVCDPZGBF29JBNBPES&pf_rd_p=84196d3b-c469-4bca-9671-ced65d7a13ed"
]

# =========================
#   セリフ（超短い・1行だけ）
# =========================

_SERIF_SOURCE = """
いくぜ,いくぞ,やるぞ,やるぜ,こいよ,こいや,まかせ,まかす,いける,いけよ,やんぞ,やんぜ,すぐだ,すぐいく,すぐこい,まてよ,まって,おうよ,おれだ,きたぞ,みせろ,みせた,とばす,のるぜ,たのむ,よゆう,いそげ,いそぐ,やばい,やべぇ,あぶね,ほんとだ,まじか,まじだ,くるぞ,くるな,こっち,そっち,とおっ,でたぞ,きめる,きめた,つかめ,つかんだ,ねらえ,ねらう,ひろえ,ひろう,とれた,とった,つえぇ,つよい,すげぇ,すごい,おそい,はやい,いいぞ,よしだ,おっし,ふせろ,どけよ,ひけよ,まえへ,さがれ,まいる,のるか,こいこい,あつまれ,つづけ,かてる,かつぞ,まけん,まけね,やめろ,いこう,いくか,いけよ,やろか,おすぜ,とめろ,はしれ,はいる,でるぞ,でかけ,あがれ,さがれ,こえる,こえるぞ,すすめ,すすむ,ひけろ,ひける,やりぬ,やりき,かかれ,たてよ,とどけ,もらう,もらえ,もらえよ,おちろ,あがれよ,きてくれ,たすけて,ありがと,すまねぇ,すまん,ゆるせ,いけた,ひいた,ひろた,みつけ,みつけた,かくほ,かくほだ,もらた,もらたぜ,みたか,みろよ,みせろよ,あつめた,あつめたぞ,もってく,もってけ,もってこい,もってきた,ひらけ,ひらけよ,よけろ,よけた,のったぞ,つかんだ,ひろえた,とったな,まったな,おそろ,こわいな,しびれ,しびれた,のった,きまる,きまれ,やるき,だすぞ,だすぜ,でるぜ,はいるぞ,でたな,かかれ,おどれ,みおろせ,みあげろ,みろよ,まてよ,がんば,がんばれ,のりきれ,つかえ,つかえよ,たえろ,こたえろ,うけろ,うけた,ねばれ,ねばる,せめろ,せめる,もどれ,もどる,あがる,あがれ,さげろ,さげた,くぐれ,とどけた,はしれよ,こいとけ,ふんばれ,ふんばる,もどった,おいつけ,おいこせ,さがれよ,おちつけ,とりあえず,つえーな,やばいぞ,あぶねぇ,すぐいけ,いそげよ,いっけぇ,さあいく,いくいく,こっちだ,はよこい,だいじょ,へっちゃ,ばっちり,おっけー,ちょうど,みえる,みえた
"""

SERIF_LIST: List[str] = [s.strip() for s in _SERIF_SOURCE.split(",") if s.strip()]

STATE_FILE = "state.json"
DAILY_LIMIT = 16
JST = tz.gettz("Asia/Tokyo")
TWEET_LIMIT = 280
TCO_URL_LEN = 23
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

ZWSP = "\u200B"
ZWNJ = "\u200C"
INVISIBLES = [ZWSP, ZWNJ]

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
    ・セリフ & アフィリンクは毎回ランダム（ツイート内でアフィ被りなし）
    ※番号は 1〜99 の範囲でループ（内部カウンタはそのまま）
    """
    urls = gofile_urls[:WANT_POST]
    if not urls:
        return "", 0

    invis = INVISIBLES[salt_idx % len(INVISIBLES)]

    # 1〜99 に丸める関数
    def wrap_seq(n: int) -> int:
        n_int = int(n)
        if n_int < 1:
            n_int = 1
        return ((n_int - 1) % 99) + 1

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

    raw_seq = start_seq  # 内部カウンタ（state.line_seq）はそのまま使う
    aff_idx = 0

    for i, u in enumerate(urls):
        # 表示用番号は 1〜99 にラップ
        disp_seq = wrap_seq(raw_seq)
        lines.append(f"{disp_seq}{invis}. {u}")
        raw_seq += 1

        # 次の URL との間に Amazon リンクを挟む
        if i < len(urls) - 1 and aff_idx < len(aff_list):
            lines.append(aff_list[aff_idx])
            aff_idx += 1

    text = "\n".join(lines)

    # 署名（不可視文字）を末尾に追加
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

def post_to_x_v2(client, text: str, quote_tweet_id: str | None = None):
    if quote_tweet_id:
        return client.create_tweet(text=text, quote_tweet_id=quote_tweet_id)
    return client.create_tweet(text=text)

def _oauth1_session():
    if OAuth1 is None:
        raise RuntimeError("requests-oauthlib が必要です。requirements.txt に 'requests-oauthlib==1.3.1' を追加してください。")
    return OAuth1(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
        signature_type='auth_header'
    )

def post_to_community_via_undocumented_api(status_text: str, community_id: str):
    # 旧Twitterエンドポイント。環境によっては https://api.x.com/2/tweets でも可
    url = "https://api.twitter.com/2/tweets"
    payload = {"text": status_text, "community_id": str(community_id)}
    sess = _oauth1_session()
    headers = {"Content-Type": "application/json"}
    r = requests.post(url, headers=headers, data=json.dumps(payload), auth=sess, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = r.text
    if not r.ok:
        raise RuntimeError(f"community post failed {r.status_code}: {body}")
    return body

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
            client_tmp = get_client()
            me = client_tmp.get_me(user_auth=True)
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

    community_id = os.getenv("X_COMMUNITY_ID", "").strip()
    client = get_client()

    if community_id:
        # 1) コミュニティに投稿
        resp_comm = post_to_community_via_undocumented_api(status_text, community_id)
        comm_id = resp_comm.get("data", {}).get("id") if isinstance(resp_comm, dict) else None
        print(f"[info] community posted id={comm_id}")

        # 2) 自分のTLにも引用ポスト（IDが取れなかったら通常ポスト）
        if comm_id:
            resp = post_to_x_v2(client, status_text, quote_tweet_id=comm_id)
            tweet_id = resp.data.get("id") if resp and resp.data else None
            print(f"[info] tweeted id={tweet_id} (quote community)")
        else:
            resp = post_to_x_v2(client, status_text)
            tweet_id = resp.data.get("id") if resp and resp.data else None
            print(f"[info] tweeted id={tweet_id} (fallback normal)")
    else:
        # 通常ポストのみ
        resp = post_to_x_v2(client, status_text)
        tweet_id = resp.data.get("id") if resp and resp.data else None
        print(f"[info] tweeted id={tweet_id}")

    # ---- ここから下は既存ロジックどおり ----

    for u in urls[:WANT_POST]:
        if u not in state["posted_urls"]:
            state["posted_urls"].append(u)
        state["recent_urls_24h"].append({"url": u, "ts": now_utc.isoformat()})
    state["posts_today"] = state.get("posts_today", 0) + 1
    state["line_seq"] = start_seq + min(WANT_POST, len(urls))
    save_state(state)

    # ---- スプシー側の E列 に「post成功」を書き込む ----
    # （sheet に存在しない URL は無視される）
    try:
        if tweet_id:
            mark_sheet_posted(urls[:WANT_POST])
    except Exception as e:
        print(f"[warn] mark_sheet_posted failed: {e}")

    used_urls = min(WANT_POST, len(urls))
    used_aff  = max(0, used_urls - 1)
    print(f"Posted ({used_urls} urls + {used_aff} amazon):", status_text)

if __name__ == "__main__":
    main()
