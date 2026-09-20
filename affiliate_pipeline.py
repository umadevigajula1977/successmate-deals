#!/usr/bin/env python3
"""
SuccessMate Deals — Amazon affiliate content pipeline.
Reads data/pending_deals.json -> Gemini enriches -> renders deal pages +
index -> posts to Telegram -> updates data/deals_list.json.
Output HTML goes to output/ ; a GitHub Action rsyncs output/ to Hostinger.
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
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

jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def retry(fn, *args, what="call", **kwargs):
    """Simple exponential-backoff retry: 2s, 4s, 8s."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
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


def build_telegram_text(enriched, affiliate_url, deal_price, original_price, discount_pct):
    caption = escape_html(enriched["telegram_caption"])
    return (
        f"{caption}\n\n"
        f"💰 <b>₹{deal_price}</b> <s>₹{original_price}</s> ({discount_pct}% OFF)\n"
        f"👉 {affiliate_url}\n\n"
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


# ---------- rendering ----------
def render_deal_page(deal):
    tpl = jinja_env.get_template("deal_page_template.html")
    return tpl.render(**deal)


def render_index(deals):
    tpl = jinja_env.get_template("index_template.html")
    ordered = sorted(deals, key=lambda d: d.get("posted_at", ""), reverse=True)
    return tpl.render(deals=ordered)


# ---------- main ----------
def process_deal(raw_deal, existing_slugs):
    enriched = enrich_with_gemini(raw_deal)

    original_price = int(raw_deal["original_price"])
    deal_price = int(raw_deal["deal_price"])
    discount_pct = round((1 - deal_price / original_price) * 100) if original_price else 0
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
    }

    # Telegram
    tg_text = build_telegram_text(enriched, affiliate_url, deal_price, original_price, discount_pct)
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

    if not pending:
        log("No pending deals. Regenerating output from existing catalog only.")
    else:
        log(f"{len(pending)} pending deal(s) to process.")

    still_pending = []
    for raw_deal in pending:
        try:
            record = process_deal(raw_deal, existing_slugs)
            deals.append(record)
            existing_slugs.add(record["slug"])
            time.sleep(2)  # gentle pacing between Gemini/Telegram calls
        except Exception as e:
            log(f"FAILED to process deal ({raw_deal.get('product_url')}): {e}")
            still_pending.append(raw_deal)  # keep for next run / manual review

    # Render all deal pages fresh from the catalog (cheap, no API calls)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for d in deals:
        html = render_deal_page(d)
        (OUTPUT_DIR / f"{d['slug']}.html").write_text(html, encoding="utf-8")

    (OUTPUT_DIR / "index.html").write_text(render_index(deals), encoding="utf-8")

    save_json(DEALS_FILE, deals)
    save_json(PENDING_FILE, still_pending)

    log(f"Done. {len(deals)} total deals live. {len(still_pending)} left pending (failed/needs review).")


if __name__ == "__main__":
    main()
