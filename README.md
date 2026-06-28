# 🔎 GMB Lead Finder

Find local businesses on Google that have **bad or missing websites** — your best
leads. The app pulls businesses from **Google Places API (New)**, scores each
website with **PageSpeed Insights**, ranks them worst-website-first, shows them in
a table, and lets you **export to CSV**.

## What it does
1. You enter a list of **niches** (dentist, plumber…) and **cities** (Houston TX…).
2. It searches every niche × city combo on Google (up to 60 results each).
3. It grabs name, phone, address, website, rating, reviews, Maps link.
4. It scores each website (speed / SEO / accessibility / HTTPS).
5. It flags the leads worth chasing:
   - **🔥 Hot** — no website, broken site, very slow, or no HTTPS
   - **🟠 Warm** — mediocre website
   - **🟢 Good** — solid website (skip)
6. Sort, filter, and **Export CSV**.

---

## 1. Set up your Google API key (one time)
You need **one** key with **two** APIs enabled:

1. Go to https://console.cloud.google.com/ → create/select a project.
2. **APIs & Services → Library** → enable:
   - **Places API (New)**
   - **PageSpeed Insights API**
3. **APIs & Services → Credentials → Create credentials → API key.** Copy it.
4. (Recommended) Set up a billing account — Places API requires it. PageSpeed is free.

## 2. Install & configure
```bash
cd "d:/downloads/Antigravity Projct/Scrapper"
python -m venv venv
venv\Scripts\activate            # Windows PowerShell
pip install -r requirements.txt
```
Create a file named **`.env`** (copy `.env.example`) and paste your key:
```
GOOGLE_API_KEY=your-key-here
```

## 3. Run
```bash
python app.py
```
Open **http://127.0.0.1:5000** in your browser.

Enter your niches + cities → **Run Search** → watch the table fill → **Export CSV**.

---

## Daily workflow (60+ leads/day)
- Put 3 niches × 2 cities in the boxes → that's up to **360** raw businesses,
  plenty after filtering to the bad-website ones.
- Tick **"Hide businesses I've already pulled"** so you get *fresh* leads each day
  (the app remembers everyone you've seen in `data/seen.json`).
- Filter to **🔥 Hot only**, export, and start outreach.

## Deploy to Railway
The app runs as a normal long-lived process — no code changes needed beyond the
included `Procfile` / `railway.json`.

1. Push this repo to GitHub (`.env` and `data/` are git-ignored — keys stay local).
2. Go to https://railway.app → **New Project → Deploy from GitHub repo** → pick this repo.
   Railway auto-detects Python and uses the start command from `railway.json`.
3. **Variables** tab → add:
   - `GOOGLE_API_KEY` = your key
   - `APOLLO_API_KEY` = your key (optional)
   - `DATA_DIR` = `/data`
4. **Settings → Volumes** → add a volume mounted at **`/data`** so `seen.json`
   survives restarts and redeploys.
5. **Settings → Networking → Generate Domain** to get a public URL.

Notes on the start command (`gunicorn ... --workers 1 --threads 8 --timeout 0`):
- `--workers 1` is intentional — the most-recent results live in memory so
  `/export` can serve them; a single worker keeps `/api/search` and `/export`
  sharing the same memory.
- `--timeout 0` disables gunicorn's worker timeout so long searches and the
  SSE progress stream aren't killed mid-run.
- `--threads 8` lets the SSE stream, its background worker thread, and other
  requests run concurrently.

## Cost notes
- **PageSpeed Insights**: free (25,000 calls/day).
- **Places API (New)**: ~$30–40 per 1,000 searches when the website field is
  included. We use field masking to keep it minimal — a daily 60-lead run is cents.
- Set a **budget alert** in Google Cloud Billing to stay safe.

## Files
| File | Purpose |
|------|---------|
| `app.py` | Flask web server + CSV export |
| `scraper.py` | Places search, PageSpeed scoring, lead classification |
| `templates/index.html` | The web UI |
| `data/seen.json` | Remembers businesses already pulled (auto-created) |
| `.env` | Your API key (never commit this) |

## Troubleshooting
- **"No API key found"** → create the `.env` file and restart.
- **Places API error 403** → enable *Places API (New)* + billing; check key restrictions.
- **All sites show "Site Broken"** → usually a bad/missing key for PageSpeed, or rate limiting.
