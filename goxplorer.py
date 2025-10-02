# goxplorer.py — gofilelab/newest を100ページ巡回して Gofile リンクを収集
# ・年齢確認（Age Gate）を自動突破（複数パターンに対応）
# ・ダウンロード数などの指標は一切使わない
# ・死にリンクは is_gofile_alive() で必ず排除
# ・cloudscraper → 0件なら Playwright フォールバック

import os
import re
import time
import random
from typing import List, Set

import cloudscraper
import requests
from bs4 import BeautifulSoup

# Playwright フォールバック
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ★ 新スクレイピング先（1〜100ページ想定）
# 例: 2ページ目 → /newest?page=2 （スラッシュ無しのクエリ形式）
BASE_LIST_URL = "https://gofilelab.com/newest?page={page}"

# gofile URLパターン（生HTML/スクリプト内も対象）
GOFILE_RE = re.compile(r"https?://gofile\.io/d/[A-Za-z0-9]+", re.I)

# 本物ブラウザ風のヘッダ
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit(KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://gofilelab.com/newest",
    "Connection": "keep-alive",
}

def _build_scraper():
    """
    Cloudflare対策の cloudscraper を用意。
    可能なら環境変数の HTTP(S)_PROXY も拾う。
    """
    proxies = {}
    http_p = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_p = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if http_p:
        proxies["http"] = http_p
    if https_p:
        proxies["https"] = https_p

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    if proxies:
        scraper.proxies.update(proxies)
    scraper.headers.update(HEADERS)
    return scraper

def fix_scheme(url: str) -> str:
    """htps:// → https:// のようなタイポ救済"""
    if url.startswith("htps://"):
        return "https://" + url[len("htps://"):]
    return url

def _extract_urls_from_html(html: str) -> List[str]:
    """
    ページから gofile の URL を抽出する（ダウンロード数などの指標は一切使わない）。
    1) a[href] から抽出
    2) 生HTML全文（script含む）から強制抽出
    """
    urls: List[str] = []
    seen = set()

    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=GOFILE_RE):
        u = fix_scheme((a.get("href") or "").strip())
        if u and u not in seen:
            urls.append(u)
            seen.add(u)

    raw_urls = set(GOFILE_RE.findall(html or ""))
    for u in raw_urls:
        u = fix_scheme(u.strip())
        if u and u not in seen:
            urls.append(u)
            seen.add(u)

    return urls

def _get_with_retry(scraper, url: str, timeout: int = 20, max_retry: int = 4):
    """
    403/5xx等に備えて、指数バックオフ＋ジッターで数回リトライ。
    """
    for attempt in range(1, max_retry + 1):
        try:
            r = scraper.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} for {url}", response=r)
            return r
        except (requests.HTTPError, requests.RequestException) as e:
            if attempt == max_retry:
                raise
            base = 0.9 * (2 ** (attempt - 1))
            time.sleep(base + random.uniform(0, base))

def _bypass_age_gate(page) -> None:
    """
    gofilelab の年齢確認を複数手段で突破。
    - localStorage / cookie にフラグを書き込んでから再読込
    - それでも残る場合は「同意」「はい」「Enter」「I Agree」などのボタンをクリック
    """
    # 1) localStorage フラグで突破（想定キーを複数試す）
    age_js = """
    try {
      localStorage.setItem('ageVerified', '1');
      localStorage.setItem('adult', 'true');
      localStorage.setItem('age_verified', 'true');
      localStorage.setItem('age_verified_at', Date.now().toString());
    } catch (e) {}
    """
    page.evaluate(age_js)
    page.wait_for_timeout(200)
    page.reload(wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(500)

    # 2) 画面上のボタンで突破（存在すれば）
    selectors = [
        "text=はい",
        "text=同意",
        "text=Enter",
        "text=I Agree",
        "text=Agree",
        "button:has-text('はい')",
        "button:has-text('同意')",
        "button:has-text('Enter')",
        "button:has-text('I Agree')",
        "[data-testid='age-accept']",
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                page.wait_for_timeout(400)
                break
        except PWTimeout:
            pass

def _fetch_page_with_playwright(url: str, wait_ms: int = 4000) -> str:
    """
    Playwrightで実ページをレンダリングしてHTMLを取得（JS実行後のDOM）。
    年齢確認が出たら突破してからHTMLを返す。
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="ja-JP"
        )
        page = context.new_page()
        page.set_extra_http_headers({
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
            "Referer": HEADERS["Referer"],
            "Connection": HEADERS["Connection"],
        })
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(800)

        # 年齢確認を突破（表示されていそうな場合は2回まで）
        for _ in range(2):
            html_probe = page.content()
            if ("年齢確認" in html_probe) or ("Age" in html_probe and "confirm" in html_probe.lower()):
                _bypass_age_gate(page)
                page.wait_for_timeout(500)
            else:
                break

        # 本文の描画を待つ
        page.wait_for_timeout(wait_ms)
        html = page.content()

        context.close()
        browser.close()
        return html

def fetch_listing_pages(num_pages: int = 100) -> List[str]:
    """
    gofilelab の newest を1→num_pagesまで巡回し、Gofile URL を収集。
    cloudscraper で試し、0件なら Playwright で再取得（Age Gate 突破付き）。
    """
    scraper = _build_scraper()
    results: List[str] = []
    seen: Set[str] = set()

    for p in range(1, num_pages + 1):
        list_url = BASE_LIST_URL.format(page=p)
        urls: List[str] = []

        # 1) cloudscraper
        try:
            r = _get_with_retry(scraper, list_url, timeout=25, max_retry=4)
            urls = _extract_urls_from_html(r.text)
        except Exception as e:
            print(f"[warn] cloudscraper page {p} failed: {e}")

        # 2) Playwright フォールバック（0件の場合のみ）
        if not urls:
            try:
                html = _fetch_page_with_playwright(list_url)
                urls = _extract_urls_from_html(html)
            except Exception as e:
                print(f"[warn] playwright page {p} failed: {e}")

        # 重複を除いて順序維持のまま追加
        added = 0
        for u in urls:
            if u not in seen:
                results.append(u)
                seen.add(u)
                added += 1

        print(f"[info] page {p}: extracted {added} new urls (total {len(results)})")
        time.sleep(1.0)  # サイト負荷軽減
    return results

def is_gofile_alive(url: str, timeout: int = 20) -> bool:
    """
    gofile詳細ページの死活判定。
    指定の死亡文言や404等で死にリンクとみなす。
    """
    url = fix_scheme(url)
    scraper = _build_scraper()
    try:
        r = _get_with_retry(scraper, url, timeout=timeout, max_retry=3)
        text = r.text or ""
        death_markers = [
            # ご指定のエラーメッセージ類
            "This content does not exist",
            "The content you are looking for could not be found",
            "has been automatically removed",
            "has been deleted by the owner",
        ]
        if any(m.lower() in text.lower() for m in death_markers):
            return False
        if r.status_code >= 400:
            return False
        if len(text) < 500 and ("error" in text.lower() or "not found" in text.lower()):
            return False
        return True
    except Exception:
        return False

def collect_fresh_gofile_urls(
    already_seen: Set[str], want: int = 20, num_pages: int = 100
) -> List[str]:
    """
    gofilelab から gofile リンクを収集し、死にリンクと既知重複を除外して返す。
    並びは収集順（ページ巡回順）のまま。ダウンロード数等は一切不使用。
    """
    urls = fetch_listing_pages(num_pages=num_pages)

    uniq: List[str] = []
    seen_now: Set[str] = set()
    for url in urls:
        if url in already_seen or url in seen_now:
            continue
        if not is_gofile_alive(url):
            continue
        uniq.append(url)
        seen_now.add(url)
        if len(uniq) >= want:
            break
    return uniq
