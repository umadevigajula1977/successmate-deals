#!/usr/bin/env python3
"""
SuccessMate Deals publisher.
Amazon Creators API (India) -> filter -> site feed -> Pinterest -> Telegram.

Env (GitHub Secrets):
  AMAZON_ACCESS_KEY        = Creators API Credential ID
  AMAZON_SECRET_KEY        = Creators API Credential Secret
  AMAZON_ASSOCIATE_TAG     = successmate-21
  AMAZON_CREDENTIAL_VERSION= 3.2 (India = EU group; default 3.2)
  PINTEREST_ACCESS_TOKEN, PINTEREST_BOARD_ID
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional: BATCH_SIZE=10 MIN_DISCOUNT=25 MIN_RATING=3.8 MIN_REVIEWS=50
          MAX_PER_CATEGORY=3 HISTORY_TTL_DAYS=30 FEED_MAX=400 DRY_RUN=1
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


FEED_FILE = Path(env("FEED_FILE", str(DATA_DIR / "deals.json")))
HISTORY_FILE = Path(env("HISTORY_FILE", str(DATA_DIR / "deals_history.json")))

CRED_ID = env("AMAZON_ACCESS_KEY")
CRED_SECRET = env("AMAZON_SECRET_KEY")
TAG = env("AMAZON_ASSOCIATE_TAG", "successmate-21")
CRED_VERSION = env("AMAZON_CREDENTIAL_VERSION", "3.2")

PIN_TOKEN = env("PINTEREST_ACCESS_TOKEN")
PIN_BOARD = env("PINTEREST_BOARD_ID")
PIN_BASE = env("PINTEREST_API_BASE", "https://api.pinterest.com/v5")

TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

BATCH_SIZE = int(env("BATCH_SIZE", "10"))
MIN_DISCOUNT = int(env("MIN_DISCOUNT", "25"))
MIN_RATING = float(env("MIN_RATING", "3.8"))
MIN_REVIEWS = int(env("MIN_REVIEWS", "50"))
MAX_PER_CATEGORY = int(env("MAX_PER_CATEGORY", "3"))
MAX_QUERIES = int(env("MAX_QUERIES", "8"))
HISTORY_TTL_DAYS = int(env("HISTORY_TTL_DAYS", "30"))
FEED_MAX = int(env("FEED_MAX", "400"))
DRY_RUN = env("DRY_RUN", "0") == "1"

MARKETPLACE = "www.amazon.in"
API_BASE = "https://creatorsapi.amazon"
TOKEN_ENDPOINTS = {
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (searchIndex, keywords) -- high-commission categories first
QUERIES = [
    ("HomeAndKitchen", "bestsellers kitchen appliances"),
    ("Fashion", "men casual wear"),
    ("Luggage", "travel essentials luggage trolley"),
    ("Beauty", "skincare face serum"),
    ("Electronics", "bluetooth earbuds accessories"),
    ("HomeAndKitchen", "trending home decor"),
    ("Apparel", "women kurta ethnic wear"),
    ("Luggage", "laptop backpack"),
    ("Beauty", "hair care combo"),
    ("Computers", "mouse keyboard laptop accessories"),
    ("HomeAndKitchen", "storage organizer"),
    ("Electronics", "smart home gadgets"),
    ("Appliances", "air fryer mixer grinder"),
    ("Fashion", "women handbag"),
]
CATEGORY_PRIORITY = ["HomeAndKitchen", "Fashion", "Apparel", "Luggage",
                     "Beauty", "Electronics", "Computers", "Appliances"]

STATE = {"pinterest": bool(PIN_TOKEN and PIN_BOARD), "telegram": bool(TG_TOKEN and TG_CHAT)}


def log(msg: str) -> None:
    print(msg, flush=True)


class FatalError(Exception):
    pass


# ---------------------------------------------------------------- http utils
def request_with_retry(method, url, *, retries=4, backoff=2.0, ok=(200, 201), **kw):
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, timeout=30, **kw)
        except requests.RequestException as e:
            last = e
            wait = backoff * (2 ** attempt)
            log(f"  net error {e}; retry in {wait:.0f}s")
            time.sleep(wait)
            continue
        if r.status_code in ok:
            return r
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = backoff * (2 ** attempt)
            wait = min(wait, 60)
            log(f"  HTTP {r.status_code}; retry in {wait:.0f}s")
            last = r
            time.sleep(wait)
            continue
        return r
    if isinstance(last, requests.Response):
        return last
    raise RuntimeError(f"{method} {url} failed: {last}")


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ------------------------------------------------------------------- amazon
class Amazon:
    def __init__(self):
        if not CRED_ID or not CRED_SECRET:
            raise FatalError("AMAZON_ACCESS_KEY / AMAZON_SECRET_KEY missing")
        self.endpoint = TOKEN_ENDPOINTS.get(CRED_VERSION)
        if not self.endpoint:
            raise FatalError(f"Unsupported credential version '{CRED_VERSION}'. "
                             "Use Creators API 3.x credentials (India = 3.2).")
        self.token = None
        self.exp = 0.0

    def _token(self, force=False) -> str:
        if self.token and not force and time.time() < self.exp - 120:
            return self.token
        r = request_with_retry(
            "POST", self.endpoint,
            headers={"Content-Type": "application/json"},
            json={"grant_type": "client_credentials", "client_id": CRED_ID,
                  "client_secret": CRED_SECRET, "scope": "creatorsapi::default"})
        if r.status_code != 200:
            raise FatalError(
                f"Amazon token failed HTTP {r.status_code}: {r.text[:200]}. "
                "AMAZON_ACCESS_KEY/SECRET_KEY must be Creators API Credential ID/Secret "
                "(old PA-API AWS keys do not work).")
        j = r.json()
        self.token = j["access_token"]
        self.exp = time.time() + int(j.get("expires_in", 3600))
        return self.token

    def search(self, index: str, keywords: str, page: int) -> list:
        body = {
            "keywords": keywords,
            "searchIndex": index,
            "itemCount": 10,
            "itemPage": page,
            "partnerTag": TAG,
            "marketplace": MARKETPLACE,
            "minReviewsRating": 3,  # API takes whole numbers only; exact 3.8 enforced client-side if data present
            "minSavingPercent": MIN_DISCOUNT,
            "languagesOfPreference": ["en_IN"],
            "resources": [
                "images.primary.large",
                "itemInfo.title",
                "offersV2.listings.price",
                "offersV2.listings.availability",
                "offersV2.listings.condition",
                "offersV2.listings.isBuyBoxWinner",
                "offersV2.listings.merchantInfo",
            ],
        }
        for attempt in range(2):
            r = request_with_retry(
                "POST", f"{API_BASE}/catalog/v1/searchItems",
                headers={"Authorization": f"Bearer {self._token(force=attempt == 1)}",
                         "Content-Type": "application/json",
                         "x-marketplace": MARKETPLACE},
                json=body)
            if r.status_code == 401 and attempt == 0:
                continue
            break
        if r.status_code == 403:
            raise FatalError(f"Amazon 403: {r.text[:200]} -- Creators API access/eligibility "
                             "not active for this Associates account.")
        if r.status_code != 200:
            log(f"  search '{keywords}' [{index}] HTTP {r.status_code}: {r.text[:200]}")
            return []
        return (r.json().get("searchResult") or {}).get("items") or []


def _num(v):
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick_listing(item: dict):
    listings = (item.get("offersV2") or {}).get("listings") or []
    for lst in listings:
        if lst.get("isBuyBoxWinner"):
            return lst
    return listings[0] if listings else None


def parse_item(item: dict, category: str):
    asin = item.get("asin")
    title = ((item.get("itemInfo") or {}).get("title") or {}).get("displayValue")
    image = ((((item.get("images") or {}).get("primary") or {}).get("large")) or {}).get("url")
    lst = pick_listing(item)
    if not (asin and title and image and lst):
        return None
    cond = (lst.get("condition") or {}).get("value")
    if cond and str(cond).lower() != "new":
        return None
    avail = (lst.get("availability") or {}).get("type")
    if avail and avail != "IN_STOCK":
        return None
    price = lst.get("price") or {}
    amount = _num((price.get("money") or {}).get("amount"))
    mrp = _num(((price.get("savingBasis") or {}).get("money") or {}).get("amount"))
    pct = _num((price.get("savings") or {}).get("percentage"))
    if amount is None:
        return None
    if pct is None and mrp and mrp > amount:
        pct = round((mrp - amount) / mrp * 100)
    if not pct:
        return None
    cr = item.get("customerReviews") or {}
    now = dt.datetime.now(IST)
    return {
        "asin": asin,
        "title": " ".join(str(title).split()),
        "image": image,
        "price": amount,
        "mrp": mrp,
        "discount": int(pct),
        "category": category,
        "rating": _num(cr.get("starRating")),
        "reviews": _num(cr.get("count")),
        "url": f"https://www.amazon.in/dp/{asin}?tag={TAG}",
        "price_checked_at": now.strftime("%d %b %Y, %I:%M %p IST"),
        "posted_at": now.isoformat(timespec="seconds"),
    }


def passes_filters(d: dict) -> bool:
    if d["discount"] < MIN_DISCOUNT:
        return False
    if d["rating"] is not None and d["rating"] < MIN_RATING:
        return False
    if d["reviews"] is not None and d["reviews"] < MIN_REVIEWS:
        return False
    return True


def rotation() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    slot_env = env("RUN_SLOT")
    if slot_env.isdigit():
        slot = int(slot_env) % 4
    else:  # cron fires ~02:30, 06:30, 11:30, 15:30 UTC
        slot = min(range(4), key=lambda i: abs(now.hour - (2, 6, 11, 15)[i]))
    return now.timetuple().tm_yday * 4 + slot


def gather(api: Amazon, history: dict, rot: int) -> list:
    n = len(QUERIES)
    page = 1 + (rot // n) % 3
    start = (rot * 3) % n
    target = BATCH_SIZE * 3
    seen, out = set(), []
    for i in range(min(MAX_QUERIES, n)):
        cat, kw = QUERIES[(start + i) % n]
        items = api.search(cat, kw, page)
        kept = 0
        for it in items:
            d = parse_item(it, cat)
            if not d or d["asin"] in seen or d["asin"] in history:
                continue
            seen.add(d["asin"])
            if passes_filters(d):
                out.append(d)
                kept += 1
        log(f"query [{cat}] '{kw}' p{page}: {len(items)} results, {kept} kept")
        time.sleep(1.2)
        if len(out) >= target:
            break
    return out


def select(cands: list) -> list:
    groups = {}
    for d in cands:
        groups.setdefault(d["category"], []).append(d)
    for g in groups.values():
        g.sort(key=lambda x: -x["discount"])
    order = sorted(groups, key=lambda c: CATEGORY_PRIORITY.index(c)
                   if c in CATEGORY_PRIORITY else 99)
    picked, counts = [], {c: 0 for c in groups}
    for enforce_cap in (True, False):
        progress = True
        while len(picked) < BATCH_SIZE and progress:
            progress = False
            for c in order:
                if len(picked) >= BATCH_SIZE:
                    break
                if enforce_cap and counts[c] >= MAX_PER_CATEGORY:
                    continue
                if groups[c]:
                    picked.append(groups[c].pop(0))
                    counts[c] += 1
                    progress = True
    return picked


# ------------------------------------------------------------- state / feed
def load_history() -> dict:
    raw = load_json(HISTORY_FILE, {})
    asins = raw.get("asins", {}) if isinstance(raw, dict) else {}
    cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_TTL_DAYS)).isoformat()
    return {a: d for a, d in asins.items() if str(d) >= cutoff}


def save_history(history: dict, picked: list) -> None:
    today = dt.date.today().isoformat()
    for d in picked:
        history[d["asin"]] = today
    save_json(HISTORY_FILE, {"updated": dt.datetime.now(IST).isoformat(timespec="seconds"),
                             "asins": history})


def update_feed(picked: list) -> None:
    raw = load_json(FEED_FILE, {"deals": []})
    old = raw if isinstance(raw, list) else raw.get("deals", [])
    merged, seen = [], set()
    for d in picked + old:
        if d.get("asin") in seen:
            continue
        seen.add(d.get("asin"))
        merged.append(d)
    save_json(FEED_FILE, {"updated": dt.datetime.now(IST).isoformat(timespec="seconds"),
                          "count": len(merged[:FEED_MAX]), "deals": merged[:FEED_MAX]})


# ---------------------------------------------------------------- formatting
def inr(x) -> str:
    s = str(int(round(x)))
    if len(s) <= 3:
        return "\u20b9" + s
    head, tail, parts = s[:-3], s[-3:], []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "\u20b9" + ",".join(parts + [tail])


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "\u2026"


# ----------------------------------------------------------------- pinterest
def pinterest_post(d: dict) -> bool:
    if not STATE["pinterest"]:
        return False
    mrp = f" (MRP {inr(d['mrp'])})" if d["mrp"] else ""
    desc = (f"{trunc(d['title'], 200)}\n\n{d['discount']}% OFF - {inr(d['price'])}{mrp}\n"
            f"Price as of {d['price_checked_at']}; may change.\n\n"
            "#AmazonDeals #Ad #CommissionsEarned #SuccessMateDeals")
    body = {
        "board_id": PIN_BOARD,
        "title": trunc(d["title"], 100),
        "description": trunc(desc, 800),
        "alt_text": trunc(d["title"], 500),
        "link": d["url"],
        "media_source": {"source_type": "image_url", "url": d["image"]},
    }
    r = request_with_retry("POST", f"{PIN_BASE}/pins", ok=(200, 201),
                           headers={"Authorization": f"Bearer {PIN_TOKEN}",
                                    "Content-Type": "application/json"},
                           json=body)
    if r.status_code in (200, 201):
        return True
    log(f"  pinterest {d['asin']} HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code in (401, 403):
        STATE["pinterest"] = False
        log("  pinterest disabled for this run (auth/permission)")
    return False


# ------------------------------------------------------------------ telegram
def tg_caption(d: dict) -> str:
    price = f"\U0001F4B0 <b>{inr(d['price'])}</b>"
    if d["mrp"]:
        price += f"  <s>{inr(d['mrp'])}</s>"
    return "\n".join([
        f"\U0001F525 <b>{d['discount']}% OFF</b>",
        f"<b>{html.escape(trunc(d['title'], 160))}</b>",
        price,
        f"<i>Price as of {d['price_checked_at']} - may change</i>",
        "",
        "#AmazonDeals #Ad",
        "<i>As an Amazon Associate I earn from qualifying purchases.</i>",
    ])


def download_image(url: str):
    try:
        r = requests.get(url, timeout=30, headers={
            "User-Agent": UA, "Referer": "https://www.amazon.in/",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        if r.ok and r.content:
            return r.content
    except requests.RequestException:
        pass
    return None


def _tg_call(method: str, **kw):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    for _ in range(3):
        try:
            r = requests.post(url, timeout=60, **kw)
        except requests.RequestException as e:
            log(f"  telegram net error {e}")
            time.sleep(3)
            continue
        if r.status_code == 429:
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            time.sleep(min(wait + 1, 60))
            continue
        return r
    return None


def telegram_post(d: dict) -> bool:
    if not STATE["telegram"]:
        return False
    markup = json.dumps({"inline_keyboard": [[{"text": "\U0001F6D2 Buy on Amazon", "url": d["url"]}]]})
    base = {"chat_id": TG_CHAT, "parse_mode": "HTML", "reply_markup": markup}
    cap = tg_caption(d)
    img = download_image(d["image"])
    r = None
    if img:
        r = _tg_call("sendPhoto", data={**base, "caption": cap},
                     files={"photo": ("deal.jpg", img, "image/jpeg")})
    if r is None or r.status_code != 200:
        r = _tg_call("sendPhoto", data={**base, "caption": cap, "photo": d["image"]})
    if r is None or r.status_code != 200:
        r = _tg_call("sendMessage", data={**base, "text": cap, "disable_web_page_preview": "true"})
    if r is not None and r.status_code == 200:
        return True
    log(f"  telegram {d['asin']} failed: {r.status_code if r is not None else 'no response'} "
        f"{r.text[:200] if r is not None else ''}")
    if r is not None and r.status_code in (401, 403):
        STATE["telegram"] = False
        log("  telegram disabled for this run (auth/permission)")
    return False


# ---------------------------------------------------------------------- main
def main() -> int:
    history = load_history()
    api = Amazon()
    cands = gather(api, history, rotation())
    picked = select(cands)
    log(f"candidates {len(cands)} -> picked {len(picked)}")
    if not picked:
        log("nothing to publish")
        return 0
    if DRY_RUN:
        print(json.dumps(picked, indent=2, ensure_ascii=False))
        return 0

    # state first: feed + history survive even if a channel dies midway
    update_feed(picked)
    save_history(history, picked)

    pins = tgs = 0
    for d in picked:
        if pinterest_post(d):
            pins += 1
        time.sleep(2)
        if telegram_post(d):
            tgs += 1
        time.sleep(3.5)  # Telegram channel limit ~20 msgs/min
    log(f"done: feed {len(picked)}, pinterest {pins}, telegram {tgs}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FatalError as e:
        log(f"FATAL: {e}")
        sys.exit(1)
