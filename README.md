# successmate-deals — Amazon affiliate pipeline

## Architecture decision (read this first)
Deploy target is **successmate.in/deals/ via SSH/rsync**, NOT GitHub Pages.
Reason: GitHub Pages (username.github.io) is a separate domain with zero
existing search authority. successmate.in already has 114 indexed pages,
GA4, and AdSense approved on this exact domain — putting deals on GitHub
Pages would mean starting SEO from zero and fragmenting the site. The repo
still lives on GitHub (for Actions + version history); the *output* HTML
is pushed over SSH to the same Hostinger path the rest of the site uses:
`~/domains/successmate.in/public_html/deals/`.

Two existing manual pages already live there (`boat-airdopes-141-deal.html`,
`samsung-galaxy-m15-5g-offer.html`, `index.html`, `deals_list.json`) — this
pipeline's `index.html` will overwrite the old one. Add those two deals'
data manually into `data/deals_list.json` (see schema below) in your first
PR if you want them to keep showing up in the new automated listing;
otherwise they'll still work as standalone pages, just delisted from index.

## One-time setup

### 1. New GitHub secret needed: `HOSTINGER_SSH_KEY`
Your existing SSH access (`ssh -p 65002 u386985095@82.25.107.203`) is
password-based. GitHub Actions needs a key instead:

```bash
ssh-keygen -t ed25519 -f deploy_key -N ""
# on your machine, appends the PUBLIC key to the server:
ssh -p 65002 u386985095@82.25.107.203 "cat >> ~/.ssh/authorized_keys" < deploy_key.pub
```

Then in GitHub repo → Settings → Secrets and variables → Actions → New secret:
- Name: `HOSTINGER_SSH_KEY`
- Value: full contents of `deploy_key` (the PRIVATE key file)

Delete `deploy_key`/`deploy_key.pub` from your machine after this — they're
only needed once.

### 2. Confirm these secrets already exist (per your setup)
`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `AMAZON_AFFILIATE_TAG`

## How you feed it deals
Amazon has no free/legal scraping route that survives GitHub Actions IPs
(blocked fast, ToS violation). Instead: you paste the deal into
`data/pending_deals.json` (one entry per deal), commit, push (or just edit
it in the GitHub web UI). The Action runs on schedule, picks up anything
in this file, and clears it after processing.

Format (add as many objects as you want in one push):
```json
[
  {
    "product_url": "https://www.amazon.in/dp/B0XXXXXXX",
    "image_url": "https://m.media-amazon.com/images/I/xxxxx.jpg",
    "original_price": 21999,
    "deal_price": 12999,
    "raw_text": "Paste the Amazon title + bullet points here, as-is."
  }
]
```
Gemini turns `raw_text` into a clean spec table, writes original pros/cons
(required — bare affiliate links with no original content risk an Amazon
Associates ban), and drafts the hybrid English+Telugu Telegram post.

## What runs each cycle (`affiliate_pipeline.py`)
1. Read `data/pending_deals.json`.
2. For each: call Gemini (`gemini-2.5-flash`) once for clean spec JSON +
   pros/cons + Telegram caption.
3. Append `?tag=successmate-21` (or `&tag=...`) to the product URL.
4. Render `<slug>.html` (mobile-first deal page) from
   `templates/deal_page_template.html`.
5. Post to Telegram (`@successmatedeals`): photo + caption if it fits
   Telegram's 1024-char photo-caption limit, else photo + separate
   full-text message.
6. Append the deal to `data/deals_list.json`, regenerate `output/index.html`
   from `templates/index_template.html`.
7. Empty `data/pending_deals.json` (only entries that failed after 3
   retries are kept, so a bad entry doesn't silently vanish).

## Files
- `affiliate_pipeline.py` — the whole pipeline
- `templates/deal_page_template.html` — single deal page (Jinja2)
- `templates/index_template.html` — listing page (Jinja2)
- `data/pending_deals.json` — YOUR input queue (edit this)
- `data/deals_list.json` — script-maintained published catalog (don't hand-edit except to seed the 2 old deals)
- `.github/workflows/deploy.yml` — cron + manual trigger, runs script, commits data, rsyncs `output/` to Hostinger
