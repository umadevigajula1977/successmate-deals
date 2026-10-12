#!/usr/bin/env python3
"""
SuccessMate Deals — Amazon affiliate content pipeline.
Reads data/pending_deals.json -> Gemini enriches -> renders deal pages +
index -> posts to Telegram -> updates data/deals_list.json.
Output HTML goes to output/ ; a GitHub Action rsyncs output/ to Hostinger.
"""
import base64
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

import requests
from jinja2 import Environment, FileSystemLoader

try:
    from google import genai
except ImportError:
    genai = None

# ---------- paths ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TEMPLATE_DIR = ROOT / "templates"
OUTPUT_DIR = ROOT / "output"
PENDING_FILE = DATA_DIR / "pending_deals.json"
DEALS_FILE = DATA_DIR / "deals_list.json"

# ---------- config ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "successmate-21")
GEMINI_MODEL = "gemini-3.6-flash"
MAX_RETRIES = 3
TELEGRAM_CAPTION_LIMIT = 1024
SITE_BASE_URL = "https://successmate.in/deals"
MAX_DEALS_PER_RUN = 5          # agreed cap: don't spam Telegram, don't blow the free Gemini quota
GEMINI_CALL_DELAY_SEC = 8      # spacing between calls within a run (helps RPM limits)
MIN_DISCOUNT_PCT = int(os.getenv("MIN_DISCOUNT_PCT", "20"))  # weak deals are never published

# Pinterest (optional: skipped silently if the two secrets are not set)
PINTEREST_ACCESS_TOKEN = os.getenv("PINTEREST_ACCESS_TOKEN")
PINTEREST_BOARD_ID = os.getenv("PINTEREST_BOARD_ID")
PINTEREST_API_BASE = os.getenv("PINTEREST_API_BASE", "https://api.pinterest.com/v5")
PIN_MAX_PER_RUN = int(os.getenv("PIN_MAX_PER_RUN", "5"))
PIN_MAX_ATTEMPTS = 3
PIN_MAX_AGE_DAYS = int(os.getenv("PIN_MAX_AGE_DAYS", "14"))   # older unpinned deals are ignored
PIN_INCLUDE_LEGACY = os.getenv("PIN_INCLUDE_LEGACY", "0") == "1"  # also pin deals made before this feature
PIN_DELAY_SEC = 2

jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


class QuotaExhausted(Exception):
    """Raised when the Gemini free-tier daily/rate quota is hit. Retrying
    immediately won't help — the whole run should stop and wait for the
    next scheduled trigger instead of burning through the pending queue."""
    pass


class WeakDeal(Exception):
    """Raised when a pending deal's discount is below MIN_DISCOUNT_PCT.
    Rejected BEFORE Gemini enrichment (saves quota), never rendered,
    never posted, and dropped from the pending queue."""
    pass


def _is_quota_error(e):
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


def retry(fn, *args, what="call", **kwargs):
    """Simple exponential-backoff retry: 2s, 4s, 8s. Quota errors skip the
    backoff entirely and raise QuotaExhausted immediately — waiting a few
    seconds never helps against a daily quota, so failing fast preserves
    the remaining retry budget for genuinely transient errors."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if _is_quota_error(e):
                log(f"{what}: Gemini quota exhausted, stopping this run early: {e}")
                raise QuotaExhausted(str(e)) from e
            last_err = e
            log(f"{what} failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    raise last_err


# ---------- helpers ----------
def slugify(text, maxlen=60):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-") or "deal"


def unique_slug(base, existing_slugs):
    slug = base
    i = 2
    while slug in existing_slugs:
        slug = f"{base}-{i}"
        i += 1
    return slug


def calc_discount_pct(original_price, deal_price):
    return round((1 - deal_price / original_price) * 100) if original_price else 0


def is_live(deal):
    """A catalog deal is live (page, index, sitemap) only if it meets the cutoff."""
    try:
        return int(deal.get("discount_pct", 0)) >= MIN_DISCOUNT_PCT
    except (TypeError, ValueError):
        return False


def normalize_url(url):
    """Re-encode the path portion properly. Copy-pasted URLs sometimes carry
    literal special characters (e.g. a raw '+') that should be percent-encoded
    — unquote first so we don't double-encode an already-correct URL, then
    quote so any raw special character gets encoded exactly once."""
    parts = urlsplit(url)
    safe_path = quote(unquote(parts.path), safe="/")
    return urlunsplit((parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment))


def add_affiliate_tag(url, tag):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["tag"] = tag
    new_query = urlencode(query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- Gemini ----------
GEMINI_PROMPT = """You are writing content for an Amazon India affiliate deal page (SuccessMate Deals) and a Telegram channel for Telugu-speaking shoppers.

RAW PRODUCT INFO (pasted from Amazon, may be messy):
---
{raw_text}
---
Original price: INR {original_price}
Deal price: INR {deal_price}

Return ONLY valid JSON (no markdown fences, no commentary) with exactly these keys:
{{
  "clean_title": "short clean product title, max 70 chars, no ALL CAPS spam",
  "specs": ["4 to 6 short bullet-point specs, plain text, no leading dash"],
  "pros": ["3 to 5 short original pros, written in your own words, not copied from Amazon"],
  "cons": ["1 to 3 short honest cons or things to check before buying"],
  "meta_description": "1 sentence, under 155 chars, for SEO meta description",
  "telugu_tip": "2 to 3 sentences in natural Telugu script giving a genuine buying tip or who this product suits best",
  "telegram_caption": "A SHORT, punchy Telegram deal alert. Product name + 1 emoji, then exactly 2 key highlights as short phrases (not full sentences), then one line starting with '💡 తెలుగులో:' with a 1-line Telugu tip. Do NOT include the URL or price in this field, those are added separately. Hard limit: 250 characters total."
}}
The pros/cons and telugu_tip must be original analysis, not copied Amazon marketing text — this is required for Amazon Associates policy compliance."""


def enrich_with_gemini(deal):
    if genai is None:
        raise RuntimeError("google-genai package not installed")
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = GEMINI_PROMPT.format(
        raw_text=deal["raw_text"],
        original_price=deal["original_price"],
        deal_price=deal["deal_price"],
    )

    def call():
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = resp.text.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(text)

    return retry(call, what="Gemini enrichment")


# ---------- Telegram ----------
def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_telegram_text(enriched, deal_page_url, deal_price, original_price, discount_pct):
    caption = escape_html(enriched["telegram_caption"])
    return (
        f"{caption}\n\n"
        f"💰 <b>₹{deal_price}</b> <s>₹{original_price}</s> ({discount_pct}% OFF)\n"
        f"👉 పూర్తి వివరాలు & బెస్ట్ ఆఫర్ కొరకు క్లిక్ చేయండి: {deal_page_url}\n\n"
        f"<i>As an Amazon Associate, SuccessMate earns from qualifying purchases.</i>"
    )


def _post_telegram(base, method, payload, files=None):
    r = requests.post(f"{base}/{method}", data=payload, files=files, timeout=30)
    if not r.ok:
        # Telegram's actual reason lives in the response body, not the status line.
        raise RuntimeError(f"Telegram {method} failed: HTTP {r.status_code} — {r.text}")
    return r.json()


def download_image(image_url):
    """Amazon's CDN blocks Telegram's own server from hotlinking the image
    (no browser-like headers), so we fetch the bytes ourselves and upload
    them to Telegram directly instead of passing the URL. Tries a couple of
    realistic header sets before giving up — a genuinely dead/placeholder
    URL will still correctly fail after this."""
    header_variants = [
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.amazon.in/",
        },
        {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
            "Accept": "*/*",
        },
    ]
    last_err = None
    for headers in header_variants:
        try:
            r = requests.get(image_url, headers=headers, timeout=20, allow_redirects=True)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code} fetching image — body: {r.text[:200]!r}")
            content_type = r.headers.get("Content-Type", "")
            if "image" not in content_type:
                raise RuntimeError(f"URL did not return an image (Content-Type: {content_type})")
            return r.content
        except Exception as e:
            last_err = e
    raise last_err


def send_telegram(image_url, text):
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    image_bytes = None
    if image_url:
        try:
            image_bytes = retry(download_image, image_url, what="Image download")
        except Exception as e:
            log(f"Could not fetch product image, posting as text-only: {e}")

    def call():
        if image_bytes and len(text) <= TELEGRAM_CAPTION_LIMIT:
            return _post_telegram(base, "sendPhoto",
                {"chat_id": TELEGRAM_CHAT_ID, "caption": text, "parse_mode": "HTML"},
                files={"photo": ("deal.jpg", image_bytes)})
        elif image_bytes:
            _post_telegram(base, "sendPhoto",
                {"chat_id": TELEGRAM_CHAT_ID},
                files={"photo": ("deal.jpg", image_bytes)})
            return _post_telegram(base, "sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            })
        else:
            return _post_telegram(base, "sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            })

    return retry(call, what="Telegram post")


# ---------- Pinterest ----------
def pinterest_enabled():
    return bool(PINTEREST_ACCESS_TOKEN and PINTEREST_BOARD_ID)


def build_pin_description(deal):
    text = (
        f"{deal['title']}\n\n"
        f"{deal['discount_pct']}% OFF - \u20b9{deal['deal_price']} (MRP \u20b9{deal['original_price']})\n"
        "Price may change. Full details and buying link on SuccessMate Deals.\n\n"
        "#AmazonDeals #Ad #CommissionsEarned #SuccessMateDeals"
    )
    return text[:800]


def _sniff_image_type(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def page_is_live(url):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code == 200
    except requests.RequestException:
        return False


def create_pin(deal):
    """Returns ("ok", pin_id) | ("failed", message) | ("auth", message).
    Pin links to OUR deal page (ads + full content), never straight to Amazon.
    Uses its own small retry loop: the shared retry() treats any '429' as a
    Gemini quota error and would abort the whole run."""
    page_url = f"{SITE_BASE_URL}/{deal['slug']}.html"

    media = None
    try:
        img = download_image(deal["image_url"])
        ctype = _sniff_image_type(img)
        if ctype:
            media = {"source_type": "image_base64", "content_type": ctype,
                     "data": base64.b64encode(img).decode("ascii")}
    except Exception as e:
        log(f"Pinterest: image download failed, falling back to URL: {e}")
    if media is None:
        media = {"source_type": "image_url", "url": deal["image_url"]}

    body = {
        "board_id": PINTEREST_BOARD_ID,
        "title": deal["title"][:100],
        "description": build_pin_description(deal),
        "alt_text": deal["title"][:500],
        "link": page_url,
        "media_source": media,
    }
    headers = {"Authorization": f"Bearer {PINTEREST_ACCESS_TOKEN}",
               "Content-Type": "application/json"}

    last = "unknown error"
    for attempt in range(1, 4):
        try:
            r = requests.post(f"{PINTEREST_API_BASE}/pins", headers=headers, json=body, timeout=60)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 * attempt)
            continue
        if r.status_code in (200, 201):
            return "ok", str(r.json().get("id", ""))
        if r.status_code in (401, 403):
            return "auth", f"HTTP {r.status_code}: {r.text[:200]}"
        last = f"HTTP {r.status_code}: {r.text[:200]}"
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2 * attempt
            time.sleep(min(wait, 30))
            continue
        return "failed", last
    return "failed", last


def run_pinterest(deals):
    """Pins live deals whose page is already deployed. New deals start as
    'pending' and are pinned on a later run (after the site deploy), so
    Pinterest never crawls a 404. Never raises: Pinterest problems must not
    break the Telegram/site pipeline."""
    if not pinterest_enabled():
        log("Pinterest not configured (PINTEREST_ACCESS_TOKEN / PINTEREST_BOARD_ID); skipping pins.")
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=PIN_MAX_AGE_DAYS)
    todo = []
    for d in deals:
        if not is_live(d) or d.get("pinterest_status") == "ok":
            continue
        if d.get("pinterest_status") is None and not PIN_INCLUDE_LEGACY:
            continue
        if int(d.get("pinterest_attempts", 0)) >= PIN_MAX_ATTEMPTS:
            continue
        try:
            posted = datetime.fromisoformat(d.get("posted_at", ""))
        except ValueError:
            continue
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if posted < cutoff:
            continue
        todo.append((posted, d))
    todo.sort(key=lambda x: x[0])  # oldest first, so nothing starves
    todo = todo[:PIN_MAX_PER_RUN]
    if not todo:
        log("Pinterest: nothing to pin this run.")
        return

    for i, (_, d) in enumerate(todo):
        page_url = f"{SITE_BASE_URL}/{d['slug']}.html"
        if not page_is_live(page_url):
            log(f"Pinterest: page not live yet, will pin next run: {page_url}")
            continue
        status, info = create_pin(d)
        if status == "ok":
            d["pinterest_status"] = "ok"
            d["pinterest_pin_id"] = info
            log(f"Pinned to Pinterest: {d['title']} ({info})")
        elif status == "auth":
            log(f"Pinterest auth/permission error, stopping pins this run: {info}")
            return  # attempts not counted: retried once the token is fixed
        else:
            d["pinterest_status"] = "failed"
            d["pinterest_attempts"] = int(d.get("pinterest_attempts", 0)) + 1
            log(f"Pinterest pin failed ({d['title']}): {info}")
        if i < len(todo) - 1:
            time.sleep(PIN_DELAY_SEC)


# ---------- rendering ----------
def render_deal_page(deal):
    tpl = jinja_env.get_template("deal_page_template.html")
    return tpl.render(**deal)


def render_index(deals):
    tpl = jinja_env.get_template("index_template.html")
    ordered = sorted(deals, key=lambda d: d.get("posted_at", ""), reverse=True)
    return tpl.render(deals=ordered)


def generate_sitemap(deals):
    """Simple sitemap.xml covering the deals index + every live deal page.
    Deployed to public_html/deals/sitemap.xml alongside the pages."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [(f"{SITE_BASE_URL}/", today, "1.0")]
    for d in deals:
        lastmod = d.get("posted_at", "")[:10] or today
        urls.append((f"{SITE_BASE_URL}/{d['slug']}.html", lastmod, "0.8"))

    entries = "\n".join(
        f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{lastmod}</lastmod>"
        f"\n    <priority>{priority}</priority>\n  </url>"
        for loc, lastmod, priority in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n"
    )


# ---------- main ----------
def process_deal(raw_deal, existing_slugs):
    original_price = int(raw_deal["original_price"])
    deal_price = int(raw_deal["deal_price"])
    discount_pct = calc_discount_pct(original_price, deal_price)

    # Cutoff check FIRST: no Gemini call, no page, no Telegram for weak deals.
    if discount_pct < MIN_DISCOUNT_PCT:
        raise WeakDeal(f"{discount_pct}% off is below the {MIN_DISCOUNT_PCT}% minimum")

    enriched = enrich_with_gemini(raw_deal)

    affiliate_url = add_affiliate_tag(normalize_url(raw_deal["product_url"]), AFFILIATE_TAG)
    image_url = normalize_url(raw_deal["image_url"])

    slug = unique_slug(slugify(enriched["clean_title"]), existing_slugs)

    deal_record = {
        "slug": slug,
        "title": enriched["clean_title"],
        "meta_description": enriched["meta_description"],
        "specs": enriched["specs"],
        "pros": enriched["pros"],
        "cons": enriched["cons"],
        "telugu_tip": enriched["telugu_tip"],
        "image_url": image_url,
        "affiliate_url": affiliate_url,
        "original_price": original_price,
        "deal_price": deal_price,
        "discount_pct": discount_pct,
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "pinterest_status": "pending",
        "pinterest_attempts": 0,
    }

    # Telegram — link goes to OUR deal page (ads + full content), not straight to Amazon
    deal_page_url = f"{SITE_BASE_URL}/{slug}.html"
    tg_text = build_telegram_text(enriched, deal_page_url, deal_price, original_price, discount_pct)
    send_telegram(image_url, tg_text)
    log(f"Posted to Telegram: {deal_record['title']}")

    return deal_record


def main():
    if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log("Missing required env vars (GEMINI_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Exiting.")
        sys.exit(1)

    pending = load_json(PENDING_FILE, [])
    deals = load_json(DEALS_FILE, [])
    existing_slugs = {d["slug"] for d in deals}

    batch = pending[:MAX_DEALS_PER_RUN]
    overflow = pending[MAX_DEALS_PER_RUN:]  # untouched, waits for a later run
    if not pending:
        log("No pending deals. Regenerating output from existing catalog only.")
    else:
        log(f"{len(pending)} pending deal(s) queued; processing up to {len(batch)} this run "
            f"(cap: {MAX_DEALS_PER_RUN}/run).")

    still_pending = []
    quota_hit = False
    rejected_weak = 0
    for i, raw_deal in enumerate(batch):
        if quota_hit:
            still_pending.append(raw_deal)
            continue
        try:
            record = process_deal(raw_deal, existing_slugs)
            deals.append(record)
            existing_slugs.add(record["slug"])
            if i < len(batch) - 1:
                time.sleep(GEMINI_CALL_DELAY_SEC)  # spacing between calls, helps RPM limits
        except WeakDeal as e:
            rejected_weak += 1
            log(f"SKIPPED weak deal ({raw_deal.get('product_url')}): {e}. Dropped from queue.")
        except QuotaExhausted:
            quota_hit = True
            still_pending.append(raw_deal)
        except Exception as e:
            log(f"FAILED to process deal ({raw_deal.get('product_url')}): {e}")
            still_pending.append(raw_deal)  # keep for next run / manual review

    if quota_hit:
        log(f"Gemini quota hit — {len(still_pending)} batch item(s) deferred, "
            f"plus {len(overflow)} already-queued overflow item(s).")
    still_pending.extend(overflow)

    # Only deals meeting the cutoff get pages / index entry / sitemap entry.
    # Weak legacy deals stay in deals_list.json (data kept) but are not rendered.
    live_deals = [d for d in deals if is_live(d)]
    hidden = len(deals) - len(live_deals)
    if hidden:
        log(f"{hidden} catalog deal(s) below {MIN_DISCOUNT_PCT}% hidden from pages/index/sitemap.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for d in live_deals:
        html = render_deal_page(d)
        (OUTPUT_DIR / f"{d['slug']}.html").write_text(html, encoding="utf-8")

    (OUTPUT_DIR / "index.html").write_text(render_index(live_deals), encoding="utf-8")
    (OUTPUT_DIR / "sitemap.xml").write_text(generate_sitemap(live_deals), encoding="utf-8")

    try:
        run_pinterest(deals)
    except Exception as e:
        log(f"Pinterest step failed (ignored): {e}")

    save_json(DEALS_FILE, deals)
    save_json(PENDING_FILE, still_pending)

    log(f"Done. {len(live_deals)} live deals ({hidden} hidden, {rejected_weak} rejected this run). "
        f"{len(still_pending)} left pending (failed/needs review).")


if __name__ == "__main__":
    main()
