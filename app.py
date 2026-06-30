"""
GMB Lead Finder — Flask web app.

Run:  python app.py   then open  http://127.0.0.1:5000
"""

import os
import csv
import io
import json
import queue
import threading
from datetime import date, datetime

from flask import (Flask, render_template, request, jsonify, Response,
                   stream_with_context, send_file)
from dotenv import load_dotenv

import scraper

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
APOLLO_KEY = os.getenv("APOLLO_API_KEY", "").strip()
VIBE_KEY = (os.getenv("EXPLORIUM_API_KEY", "").strip()
            or os.getenv("VIBE_API_KEY", "").strip())
# Which firmographic provider is active (Vibe preferred).
ENRICH_PROVIDER = "Vibe / Explorium" if VIBE_KEY else ("Apollo" if APOLLO_KEY else "")

# DATA_DIR can be overridden (e.g. point it at a Railway persistent volume
# so data/seen.json survives restarts and redeploys).
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")

app = Flask(__name__)

# Keep the most recent results in memory so /export can serve them.
LAST_RESULTS = {"leads": [], "generated": ""}


# --------------------------------------------------------------------------
def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


CSV_COLUMNS = [
    ("name", "Business Name"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("emails", "All Emails"),
    ("address", "Address"),
    ("website", "Website"),
    ("website_status", "Website Status"),
    ("intent", "Buying Intent"),
    ("lead_quality", "Lead Quality"),
    ("lead_score", "Lead Score"),
    ("perf", "Speed Score"),
    ("seo", "SEO Score"),
    ("accessibility", "Accessibility"),
    ("best_practices", "Best Practices"),
    ("https", "HTTPS"),
    ("facebook", "Facebook"),
    ("instagram", "Instagram"),
    ("flags", "Issues"),
    ("employees", "Employees"),
    ("revenue_str", "Revenue"),
    ("industry", "Industry"),
    ("locations", "Locations"),
    ("rating", "Google Rating"),
    ("reviews", "Reviews"),
    ("niche", "Niche"),
    ("city", "City"),
    ("maps_url", "Maps URL"),
]


# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", has_key=bool(API_KEY),
                           has_enrich=bool(VIBE_KEY or APOLLO_KEY),
                           enrich_provider=ENRICH_PROVIDER)


@app.route("/api/search")
def api_search():
    """Streams progress + final results via Server-Sent Events."""
    if not API_KEY:
        return jsonify({"error": "No GOOGLE_API_KEY set. Create a .env file."}), 400

    niches = [n for n in request.args.get("niches", "").split("\n") if n.strip()]
    cities = [c for c in request.args.get("cities", "").split("\n") if c.strip()]
    max_per = int(request.args.get("max_per", 60))
    run_psi = request.args.get("pagespeed", "1") == "1"
    hide_seen = request.args.get("hide_seen", "0") == "1"
    keep_good = request.args.get("keep_good", "0") == "1"
    find_emails = request.args.get("find_emails", "1") == "1"

    def _int(name, default=0):
        try:
            return int(float(request.args.get(name, default) or 0))
        except (TypeError, ValueError):
            return default

    min_reviews = _int("min_reviews")
    max_reviews = _int("max_reviews")
    use_enrich = request.args.get("apollo", "0") == "1" and bool(VIBE_KEY or APOLLO_KEY)
    emp_min = _int("emp_min")
    emp_max = _int("emp_max")
    # revenue inputs arrive in $millions; convert to dollars.
    rev_min = int(float(request.args.get("rev_min", 0) or 0) * 1_000_000)
    rev_max = int(float(request.args.get("rev_max", 0) or 0) * 1_000_000)
    apollo_strict = request.args.get("apollo_strict", "0") == "1"

    if not niches or not cities:
        return jsonify({"error": "Enter at least one niche and one city."}), 400

    seen_ids = load_seen() if hide_seen else set()
    q = queue.Queue()

    def progress(stage, done, total, msg):
        q.put({"type": "progress", "stage": stage,
               "done": done, "total": total, "msg": msg})

    def worker():
        try:
            leads, errors = scraper.run_search(
                niches, cities, API_KEY,
                max_per_query=max_per, run_pagespeed=run_psi,
                seen_ids=seen_ids, progress=progress, keep_good=keep_good,
                find_emails=find_emails,
                min_reviews=min_reviews, max_reviews=max_reviews,
                vibe_key=VIBE_KEY if (use_enrich and VIBE_KEY) else None,
                apollo_key=APOLLO_KEY if (use_enrich and APOLLO_KEY) else None,
                emp_min=emp_min, emp_max=emp_max,
                rev_min=rev_min, rev_max=rev_max, apollo_strict=apollo_strict,
            )
            # remember everything we've ever seen
            all_seen = load_seen()
            all_seen.update(l["place_id"] for l in leads if l["place_id"])
            save_seen(all_seen)

            LAST_RESULTS["leads"] = leads
            LAST_RESULTS["generated"] = datetime.now().isoformat(timespec="seconds")
            q.put({"type": "done", "leads": leads, "errors": errors})
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "error": str(e)})
        finally:
            q.put(None)  # sentinel

    threading.Thread(target=worker, daemon=True).start()

    @stream_with_context
    def generate():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/export", methods=["GET", "POST"])
def export():
    """Download results as CSV. Optional ?quality= filter.

    POST with JSON {"leads": [...]} exports exactly the rows the browser is
    holding — this is the path the UI uses, so the download never depends on
    server-side in-memory state (which is lost on a restart or a second
    process/replica). GET falls back to the most recent in-memory results."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        leads = body.get("leads") or []
        quality = body.get("quality", "")
    else:
        leads = LAST_RESULTS["leads"]
        quality = request.args.get("quality", "")  # e.g. "hot"
    if quality == "hot":
        leads = [l for l in leads if "Hot" in l.get("lead_quality", "")]
    elif quality == "warm":
        leads = [l for l in leads if "Hot" in l.get("lead_quality", "")
                 or "Warm" in l.get("lead_quality", "")]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([h for _, h in CSV_COLUMNS])
    for l in leads:
        writer.writerow([l.get(k, "") for k, _ in CSV_COLUMNS])

    mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    fname = f"leads_{date.today().isoformat()}.csv"
    return send_file(mem, mimetype="text/csv",
                     as_attachment=True, download_name=fname)


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
