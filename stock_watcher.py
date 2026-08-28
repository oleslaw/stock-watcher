#!/usr/bin/env python3
"""
Watches one or more shop category/tag pages and alerts when any item whose name
matches a wanted pattern flips to "In Stock".

All targeting is configured through the environment so the target shop is not
disclosed in this source:

    WATCH_URLS          newline- or comma-separated pages to check
    WATCH_SELF_TEST_URLS  page(s) to scrape for --self-test (something usually
                          in stock, to prove the whole chain end to end)
    WATCH_HOMEPAGE      page hit first to pick up any CDN clearance cookie;
                          defaults to the scheme+host of the first WATCH_URLS entry
    WANTED_PATTERN      regex (IGNORECASE) matched against product names

Usage:
    python3 stock_watcher.py                # single check (used by CI cron)
    python3 stock_watcher.py --watch        # poll every 5 min until in-stock
    python3 stock_watcher.py --watch --interval 120   # poll every 2 min

Notifications go to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are
set in the environment, otherwise it falls back to a macOS notification.

Exits 1 if the site couldn't be fetched or parsed, so a CI cron surfaces a
broken scraper instead of silently reporting "nothing in stock" forever.

No third-party deps — stdlib only.
"""
import argparse
import gzip
import http.cookiejar
import json
import os
import re
import zlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape  # imported bare: `html` is used as a local for page bodies


def _load_dotenv(path=".env"):
    # Local convenience only: let a gitignored .env supply the target config so
    # `python3 stock_watcher.py` works without exporting vars by hand. CI passes
    # real values as secrets, so a missing file is fine.
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _urls(name):
    raw = os.environ.get(name, "")
    return [u.strip() for u in re.split(r"[\n,]", raw) if u.strip()]


def _config():
    _load_dotenv()
    urls = _urls("WATCH_URLS")
    self_test_urls = _urls("WATCH_SELF_TEST_URLS")
    homepage = os.environ.get("WATCH_HOMEPAGE", "").strip()
    if not homepage and urls:
        p = urllib.parse.urlsplit(urls[0])
        homepage = f"{p.scheme}://{p.netloc}/"
    pattern = os.environ.get("WANTED_PATTERN", "").strip()
    return urls, self_test_urls, homepage, pattern


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# The CDN 403s requests that only carry a User-Agent. A real browser also sends
# Accept / Accept-Language / etc., so send the full set to clear the bot filter.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

PRODUCT_RE = re.compile(
    r'<h3[^>]*>\s*<a href="(?P<url>[^"]+)"[^>]*>(?P<name>[^<]+)</a>\s*</h3>'
    r'.*?<p class="stock (?P<status>in-stock|out-of-stock)">(?P<label>[^<]+)</p>',
    re.DOTALL,
)


def _read_body(resp):
    # We advertise gzip/deflate to look like a browser, but urllib doesn't
    # decompress for us, so undo whatever Content-Encoding the CDN applied.
    raw = resp.read()
    encoding = resp.headers.get("Content-Encoding", "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        # Some servers omit the zlib header; fall back to a raw deflate stream.
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="ignore")


SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"


def _fetch_target(url):
    # Route through ScraperAPI when a key is set: it rotates proxy IPs until one
    # clears the shop's CDN bot filter, which a datacenter runner IP can't do.
    # Returns (fetch_url, timeout, direct). ScraperAPI retries internally and can
    # take up to ~70s, so it needs a much longer timeout than a direct hit.
    key = os.environ.get("SCRAPERAPI_KEY", "").strip()
    if not key:
        return url, 20, True
    params = {"api_key": key, "url": url}
    if os.environ.get("SCRAPERAPI_PREMIUM", "").strip().lower() in ("1", "true", "yes"):
        params["premium"] = "true"
    return SCRAPERAPI_ENDPOINT + "?" + urllib.parse.urlencode(params), 70, False


def _open(opener, url, referer=None):
    fetch_url, timeout, direct = _fetch_target(url)
    headers = dict(BROWSER_HEADERS)
    # Referer / same-origin hints only make sense on a direct hit; through the
    # proxy the target is a query param, so leave them off.
    if referer and direct:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    req = urllib.request.Request(fetch_url, headers=headers)
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # Can't reproduce the CDN's 401/403 locally, so surface what it tells us:
        # the auth challenge header and a snippet of the body are the evidence.
        challenge = e.headers.get("WWW-Authenticate")
        try:
            body = _read_body(e)[:400]
        except Exception:
            body = "<unreadable>"
        print(
            f"fetch {url} -> HTTP {e.code}; "
            f"WWW-Authenticate={challenge!r}; body[:400]={body!r}",
            file=sys.stderr,
        )
        raise


def fetch_products(urls, homepage=None):
    if not urls:
        raise RuntimeError("No target URLs configured — set WATCH_URLS.")
    # A cookie jar so we carry any clearance cookie the CDN sets on the homepage
    # into the category requests, the way a browser would.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # Priming only helps a direct hit; through ScraperAPI it just burns a credit.
    _, _, direct = _fetch_target(homepage or "")
    if homepage and direct:
        # Prime cookies from the homepage before hitting the category pages.
        with _open(opener, homepage) as resp:
            _read_body(resp)

    seen_urls = set()
    products = []
    for url in urls:
        with _open(opener, url, referer=homepage) as resp:
            html = _read_body(resp)

        for m in PRODUCT_RE.finditer(html):
            if m["url"] in seen_urls:
                continue
            seen_urls.add(m["url"])
            products.append({
                # Product names carry HTML entities (&#8211; for the en dash).
                "name": unescape(m["name"].strip()),
                "url": m["url"],
                "in_stock": m["status"] == "in-stock",
                "label": m["label"],
            })

    if not products:
        raise RuntimeError("No products parsed — page markup may have changed.")
    return products


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False

    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    return True


def notify(title, summary, detail=None):
    print(f"\a{title}: {summary}")

    if send_telegram(f"{title}\n\n{detail or summary}"):
        return

    if sys.platform == "darwin":
        # AppleScript string literals can't hold raw newlines or bare quotes.
        flat = summary.replace('"', "'").replace("\n", " ")
        script = f'display notification "{flat}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)
        return

    # Nowhere to send it. Never swallow this: an alert that reached no one is
    # indistinguishable from no stock, which defeats the whole point.
    raise RuntimeError(
        "Wanted stock found but no notification channel worked — "
        "are TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set?"
    )


def _wanted_matcher(pattern):
    if not pattern:
        raise RuntimeError("No wanted pattern configured — set WANTED_PATTERN.")
    wanted_re = re.compile(pattern, re.IGNORECASE)
    return lambda name: bool(wanted_re.search(name))


def check_once(urls, homepage, is_wanted):
    products = fetch_products(urls, homepage)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    in_stock = [p for p in products if p["in_stock"]]
    wanted_in_stock = [p for p in in_stock if is_wanted(p["name"])]

    print(f"[{timestamp}] checked {len(products)} variants:")
    for p in products:
        flag = "IN STOCK" if p["in_stock"] else "sold out"
        tag = " *wanted*" if is_wanted(p["name"]) else ""
        print(f"  {flag:9} - {p['name']}{tag}")

    if wanted_in_stock:
        names = ", ".join(p["name"] for p in wanted_in_stock)
        detail = "\n\n".join(f"{p['name']}\n{p['url']}" for p in wanted_in_stock)
        notify("🔔 Wanted item in stock!", names, detail)
        for p in wanted_in_stock:
            print(f"  -> {p['url']}")
    elif in_stock:
        print("  (other variants in stock, but none you're watching for)")
    return bool(wanted_in_stock)


def self_test(self_test_urls, homepage, is_wanted):
    """Fire a real alert off a real in-stock product, to prove the whole chain."""
    if not self_test_urls:
        raise RuntimeError("No self-test URLs configured — set WATCH_SELF_TEST_URLS.")
    products = fetch_products(self_test_urls, homepage)
    in_stock = [p for p in products if p["in_stock"]]
    print(f"self-test: parsed {len(products)} products, {len(in_stock)} in stock")

    if not in_stock:
        raise RuntimeError(
            "Self-test found nothing in stock across the whole shop — that's "
            "implausible, so treat the in-stock parser as broken."
        )

    # Prefer models you aren't watching, so a test alert can never be read as
    # "not a real alert" while quietly sitting on a genuine restock.
    sample = ([p for p in in_stock if not is_wanted(p["name"])] or in_stock)[:3]
    names = ", ".join(p["name"] for p in sample)
    detail = "\n\n".join(f"{p['name']}\n{p['url']}" for p in sample)
    notify(
        "🧪 Watcher self-test",
        names,
        f"Not a real alert — these are in-stock items you are NOT watching.\n"
        f"A genuine restock will look like this.\n\n{detail}",
    )
    for p in sample:
        print(f"  -> {p['name']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="send a test alert using a real in-stock product")
    parser.add_argument("--watch", action="store_true", help="poll repeatedly until something is in stock")
    parser.add_argument("--interval", type=int, default=300, help="seconds between checks in watch mode (default 300)")
    args = parser.parse_args()

    urls, self_test_urls, homepage, pattern = _config()
    is_wanted = _wanted_matcher(pattern)

    if args.self_test or not args.watch:
        try:
            if args.self_test:
                self_test(self_test_urls, homepage, is_wanted)
            else:
                check_once(urls, homepage, is_wanted)
        except Exception as e:
            print(f"check failed: {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    try:
        while True:
            try:
                if check_once(urls, homepage, is_wanted):
                    print("A wanted item is in stock - stopping.")
                    break
            except Exception as e:
                print(f"check failed: {e}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
