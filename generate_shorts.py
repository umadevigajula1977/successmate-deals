#!/usr/bin/env python3
"""
SuccessMate Deals — Telegram Shorts generator (free: edge-tts + Pillow + FFmpeg).
Reads data/new_deals.json (written by affiliate_pipeline.py: deals created this
run) -> Telugu voiceover -> 1080x1920 MP4 -> posts to the Telegram
channel. Never fails the workflow: every error is logged and skipped.
"""
import asyncio
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
NEW_FILE = ROOT / "data" / "new_deals.json"
SITE_BASE_URL = "https://successmate.in/deals"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MAX_SHORTS = int(os.getenv("MAX_SHORTS_PER_RUN", "3"))
VOICE = os.getenv("SHORTS_VOICE", "te-IN-ShrutiNeural")   # male: te-IN-MohanNeural
USE_IMAGE = os.getenv("SHORTS_USE_IMAGE", "0") == "1"      # default: text-only frames (safe for Amazon image rules)
W, H = 1080, 1920


def log(msg):
    print(f"[shorts] {msg}", flush=True)


# ---------- fonts / drawing ----------
def find_font(bold=True):
    names = ["NotoSans-Bold.ttf", "NotoSans-Regular.ttf"] if bold else ["NotoSans-Regular.ttf", "NotoSans-Bold.ttf"]
    for n in names:
        hits = glob.glob(f"/usr/share/fonts/**/{n}", recursive=True)
        if hits:
            return hits[0]
    for n in (["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"]):
        hits = glob.glob(f"/usr/share/fonts/**/{n}", recursive=True)
        if hits:
            return hits[0]
    return None


def font(size, bold=True):
    path = find_font(bold)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


def wrap(draw, text, fnt, max_w, max_lines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=fnt) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return lines


def centered(draw, y, text, fnt, fill):
    w = draw.textlength(text, font=fnt)
    draw.text(((W - w) / 2, y), text, font=fnt, fill=fill)
    return y + fnt.size


def make_frame(deal, out_png):
    img = Image.new("RGB", (W, H), "#0b0f1a")
    d = ImageDraw.Draw(img)
    for y in range(H):  # subtle vertical gradient
        c = int(11 + 22 * y / H)
        d.line([(0, y), (W, y)], fill=(c, c + 4, c + 18))

    y = 130
    y = centered(d, y, "SuccessMate Deals", font(56), "#F97316")

    y += 70
    badge = f"{deal['discount_pct']}% OFF"
    bf = font(130)
    bw = d.textlength(badge, font=bf)
    d.rounded_rectangle([(W - bw) / 2 - 50, y, (W + bw) / 2 + 50, y + 190], radius=36, fill="#EF4444")
    d.text(((W - bw) / 2, y + 25), badge, font=bf, fill="white")
    y += 190 + 70

    if USE_IMAGE and deal.get("image_url"):
        try:
            from io import BytesIO
            from affiliate_pipeline import download_image
            pic = Image.open(BytesIO(download_image(deal["image_url"]))).convert("RGB")
            pic.thumbnail((640, 640))
            px = (W - pic.width) // 2
            d.rounded_rectangle([px - 8, y - 8, px + pic.width + 8, y + pic.height + 8], radius=20, fill="white")
            img.paste(pic, (px, y))
            y += pic.height + 60
        except Exception as e:
            log(f"image skipped: {e}")
    else:
        y += 40

    tf = font(70)
    for line in wrap(d, deal["title"], tf, W - 160, 4):
        y = centered(d, y, line, tf, "#f1f4f9") + 14

    y += 60
    y = centered(d, y, f"₹{deal['deal_price']}", font(190), "#22C55E")
    mrp = f"MRP ₹{deal['original_price']}"
    mf = font(52, bold=False)
    mw = d.textlength(mrp, font=mf)
    my = y + 10
    d.text(((W - mw) / 2, my), mrp, font=mf, fill="#93a0bd")
    d.line([((W - mw) / 2, my + 31), ((W + mw) / 2, my + 31)], fill="#93a0bd", width=4)

    centered(d, H - 330, "Full details: successmate.in/deals", font(46), "#F97316")
    centered(d, H - 240, "Link in the Telegram caption", font(40, bold=False), "#93a0bd")
    centered(d, H - 150, "As an Amazon Associate we earn from qualifying purchases", font(30, bold=False), "#93a0bd")
    img.save(out_png)


# ---------- voice / video ----------
def speech_text(deal):
    tip = (deal.get("telugu_tip") or "").strip()
    return (f"కేవలం {deal['deal_price']} రూపాయలు. {deal['discount_pct']} శాతం తగ్గింపు. "
            f"{tip} పూర్తి వివరాలకు సక్సెస్ మేట్ డీల్స్ చూడండి.")


def make_audio(text, out_mp3):
    import edge_tts
    last = None
    for attempt in range(1, 4):
        try:
            asyncio.run(edge_tts.Communicate(text, VOICE).save(str(out_mp3)))
            if Path(out_mp3).exists() and Path(out_mp3).stat().st_size > 1000:
                return
            raise RuntimeError("empty audio file")
        except Exception as e:
            last = e
            log(f"edge-tts attempt {attempt}/3 failed: {e}")
            time.sleep(3 * attempt)
    raise last


def make_video(png, mp3, out_mp4):
    import subprocess
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-loop", "1", "-framerate", "25", "-i", str(png), "-i", str(mp3),
           "-af", "apad=pad_dur=0.8", "-t", "58",
           "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out_mp4)]
    subprocess.run(cmd, check=True)


# ---------- telegram ----------
def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def caption(deal):
    url = f"{SITE_BASE_URL}/{deal['slug']}.html"
    return (f"🎬 <b>{esc(deal['title'])}</b>\n"
            f"💰 <b>₹{deal['deal_price']}</b> <s>₹{deal['original_price']}</s> ({deal['discount_pct']}% OFF)\n"
            f"👉 {url}\n\n"
            f"<i>#ad As an Amazon Associate, SuccessMate earns from qualifying purchases.</i>")[:1024]


def send_video(mp4, deal):
    last = None
    for attempt in range(1, 4):
        try:
            with open(mp4, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendVideo",
                    data={"chat_id": CHAT_ID, "caption": caption(deal), "parse_mode": "HTML",
                          "supports_streaming": "true", "width": W, "height": H},
                    files={"video": ("deal.mp4", f)}, timeout=180)
            if r.ok:
                return
            raise RuntimeError(f"HTTP {r.status_code} — {r.text[:300]}")
        except Exception as e:
            last = e
            log(f"telegram attempt {attempt}/3 failed: {e}")
            time.sleep(4 * attempt)
    raise last


# ---------- main ----------
def main():
    if not (TOKEN and CHAT_ID):
        log("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Skipping.")
        return 0
    if not NEW_FILE.exists():
        log("No data/new_deals.json. Nothing to do.")
        return 0
    deals = json.loads(NEW_FILE.read_text(encoding="utf-8") or "[]")[:MAX_SHORTS]
    if not deals:
        log("No new deals this run.")
        return 0
    done = 0
    for d in deals:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                make_frame(d, tmp / "frame.png")
                make_audio(speech_text(d), tmp / "voice.mp3")
                make_video(tmp / "frame.png", tmp / "voice.mp3", tmp / "short.mp4")
                send_video(tmp / "short.mp4", d)
            done += 1
            log(f"Posted short: {d['title']}")
            time.sleep(3)
        except Exception as e:
            log(f"FAILED short for {d.get('slug')}: {e}")
    log(f"Done. {done}/{len(deals)} shorts posted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
