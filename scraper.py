"""
Core engine: pulls businesses from Google Places API (New),
scores their websites with PageSpeed Insights, and classifies leads.

A "good lead" = a business with a BAD or MISSING website, because that's
who needs our help. So the worse the website, the higher the lead score.
"""

import re
import time
import math
import requests
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/organizations/enrich"

# Only request the fields we actually use -> keeps Places API cost down.
PLACES_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.googleMapsUri",
    "places.businessStatus",
    "nextPageToken",
])


# --------------------------------------------------------------------------
# 1. PLACES SEARCH
# --------------------------------------------------------------------------
def search_places(query, api_key, max_results=60):
    """Return a list of raw business dicts for one text query (e.g.
    'dentists in Houston'). Paginates up to 60 results (20 per page)."""
    results = []
    page_token = None
    pages = min(3, max(1, math.ceil(max_results / 20)))

    for page in range(pages):
        body = {"textQuery": query, "pageSize": 20}
        if page_token:
            body["pageToken"] = page_token

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": PLACES_FIELD_MASK,
        }

        # The Places API (New) intermittently returns a transient 403/429/5xx
        # even with a valid key. Retry a few times with backoff before failing.
        resp = None
        for attempt in range(4):
            resp = requests.post(PLACES_URL, json=body, headers=headers, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code in (403, 429, 500, 502, 503):
                time.sleep(1.5 * (attempt + 1))
                continue
            break  # genuine error (e.g. 400) — don't retry
        if resp.status_code != 200:
            raise RuntimeError(
                f"Places API error {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        results.extend(data.get("places", []))

        page_token = data.get("nextPageToken")
        if not page_token or len(results) >= max_results:
            break
        # New Places API needs a brief pause before the next page token works.
        time.sleep(2)

    return results[:max_results]


def normalize_place(raw, niche, city):
    """Flatten a raw Places result into our lead shape."""
    return {
        "place_id": raw.get("id", ""),
        "name": (raw.get("displayName") or {}).get("text", ""),
        "niche": niche,
        "city": city,
        "address": raw.get("formattedAddress", ""),
        "phone": raw.get("nationalPhoneNumber")
        or raw.get("internationalPhoneNumber", ""),
        "website": raw.get("websiteUri", ""),
        "rating": raw.get("rating", ""),
        "reviews": raw.get("userRatingCount", 0),
        "maps_url": raw.get("googleMapsUri", ""),
        "business_status": raw.get("businessStatus", ""),
        "email": "",
        "emails": "",
        "intent": "",
        "intent_kind": "",
        "builder": "",
        "facebook": "",
        "instagram": "",
        "employees": "",
        "revenue": "",
        "revenue_str": "",
        "industry": "",
        # filled in later by scoring:
        "perf": None,
        "seo": None,
        "accessibility": None,
        "best_practices": None,
        "https": None,
        "website_status": "",
        "lead_quality": "",
        "lead_score": 0,
        "flags": "",
    }


# --------------------------------------------------------------------------
# 2. PAGESPEED SCORING
# --------------------------------------------------------------------------
def score_website(url, api_key):
    """Run PageSpeed Insights (mobile) on one URL. Returns a dict of
    category scores (0-100) or an 'error' key if the site is unreachable."""
    params = [
        ("url", url),
        ("strategy", "mobile"),
        ("key", api_key),
        ("category", "PERFORMANCE"),
        ("category", "SEO"),
        ("category", "ACCESSIBILITY"),
        ("category", "BEST_PRACTICES"),
    ]
    try:
        resp = requests.get(PSI_URL, params=params, timeout=90)
    except requests.RequestException as e:
        return {"error": f"unreachable: {e.__class__.__name__}"}

    if resp.status_code != 200:
        # 400/500 from PSI usually means the site failed to load = great lead.
        return {"error": f"psi {resp.status_code}"}

    data = resp.json()
    lh = data.get("lighthouseResult", {})
    cats = lh.get("categories", {})

    def pct(key):
        score = (cats.get(key) or {}).get("score")
        return round(score * 100) if isinstance(score, (int, float)) else None

    final_url = lh.get("finalUrl") or lh.get("requestedUrl") or url
    return {
        "perf": pct("performance"),
        "seo": pct("seo"),
        "accessibility": pct("accessibility"),
        "best_practices": pct("best-practices"),
        "https": final_url.lower().startswith("https://"),
    }


# --------------------------------------------------------------------------
# 2a. APOLLO ENRICHMENT (optional — real employee count + revenue by domain)
# --------------------------------------------------------------------------
def _domain_of(url):
    if not url:
        return ""
    net = urlparse(url).netloc or urlparse("//" + url).netloc
    return net.replace("www.", "").strip().lower()


def apollo_enrich(domain, apollo_key):
    """Look up a company's firmographics by domain. Returns
    {employees, revenue, revenue_str, industry} or {} when unavailable.
    Needs a paid Apollo plan with API access."""
    if not domain or not apollo_key:
        return {}
    try:
        r = requests.get(
            APOLLO_ENRICH_URL,
            params={"domain": domain},
            headers={"X-Api-Key": apollo_key,
                     "Content-Type": "application/json",
                     "Cache-Control": "no-cache"},
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        org = r.json().get("organization") or {}
    except (requests.RequestException, ValueError):
        return {}
    if not org:
        return {}
    return {
        "employees": org.get("estimated_num_employees"),
        "revenue": org.get("annual_revenue"),
        "revenue_str": org.get("annual_revenue_printed", "") or "",
        "industry": org.get("industry", "") or "",
    }


# --------------------------------------------------------------------------
# 2b. EMAIL SCRAPING (free — reads the business's own website)
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Substrings that mean "this isn't a real contact address".
_EMAIL_JUNK = (
    "example.com", "example.org", "yourdomain", "domain.com", "email.com",
    "sentry.", "wixpress.com", "@2x", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", "u003e", "react", "core-js", "@sentry",
    # platform/template defaults that aren't the real business inbox:
    "@mysite.com", "@vagaro.com", "@wix.com", "@wixsite.com",
    "@squarespace.com", "@godaddy.com", "@booksy.com", "@sentry.io",
    "@example", "name@", "your@", "user@", "@2x.png",
)
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_CONTACT_WORDS = ("contact", "about", "support", "reach", "connect", "team")
_UA = {"User-Agent": "Mozilla/5.0 (compatible; LeadFinder/1.0)"}


def _emails_from_html(html):
    found = set()
    for raw in EMAIL_RE.findall(html or ""):
        e = raw.strip().lower().rstrip(".")
        if any(j in e for j in _EMAIL_JUNK):
            continue
        if e.endswith(_IMG_EXT):
            continue
        found.add(e)
    return found


# --------------------------------------------------------------------------
# 2c. INTENT SIGNALS  ("is this business in-market for a website?")
# --------------------------------------------------------------------------
# Cheap DIY builders => owner cares but lacks skill => upgrade opportunity.
_DIY_BUILDERS = {
    "wix.com": "Wix", "parastorage.com": "Wix", "_wixcssimports": "Wix",
    "godaddysites.com": "GoDaddy", "godaddy.com/websites": "GoDaddy",
    "weebly.com": "Weebly", "editmysite.com": "Weebly",
    "jimdo": "Jimdo", "webnode": "Webnode",
    "sites.google.com": "Google Sites", "carrd.co": "Carrd",
}
_COMING_SOON = (
    "coming soon", "under construction", "site is coming", "launching soon",
    "be right back", "website coming", "page is under construction",
    "new website coming", "we are building",
)
_PARKED = (
    "domain is for sale", "buy this domain", "this domain may be for sale",
    "domain may be for sale", "parkingcrew", "sedoparking", "hugedomains",
    "afternic", "domain for sale", "is parked", "godaddy.com/domainsearch",
)
_COPYRIGHT_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:&#169;)?\s*((?:19|20)\d{2})", re.I)

# Social profile links (extracted from the website's own HTML).
_FB_RE = re.compile(r'https?://(?:www\.|m\.|web\.)?facebook\.com/[^"\'\s<>)]+', re.I)
_IG_RE = re.compile(r'https?://(?:www\.)?instagram\.com/[^"\'\s<>)]+', re.I)
_FB_SKIP = ("sharer", "plugins", "/tr", "dialog", "/login", "/help",
            "policies", "/privacy", "/terms", "/business", "/v2.")
_IG_SKIP = ("instagram.com/p/", "instagram.com/explore", "instagram.com/accounts",
            "instagram.com/about", "instagram.com/developer", "instagram.com/legal",
            "instagram.com/reel")


def _first_social(html, regex, skip):
    for m in regex.finditer(html or ""):
        url = m.group(0).rstrip("/\\\"'")
        low = url.lower()
        if any(s in low for s in skip):
            continue
        # bare domain with no page handle is not a real profile
        if low.rstrip("/").endswith(("facebook.com", "instagram.com")):
            continue
        return url
    return ""


def _detect_intent(html, final_url):
    """Return (kind, label, builder) describing buying-intent signals.
    kind is one of: coming_soon, parked, diy, stale, '' (none)."""
    low = (html or "").lower()
    short = len(low) < 4000  # placeholder pages are tiny

    if any(p in low for p in _PARKED):
        return "parked", "🅿 Domain parked (no real site)", ""
    if short and any(p in low for p in _COMING_SOON):
        return "coming_soon", "🚧 Coming soon / building now", ""
    # Coming-soon phrase on a normal page is weaker but still notable.
    if any(p in low for p in _COMING_SOON):
        return "coming_soon", "🚧 'Coming soon' mentioned", ""

    builder = next((name for sig, name in _DIY_BUILDERS.items() if sig in low), "")
    if builder:
        return "diy", f"🛠 DIY site ({builder})", builder

    years = [int(y) for y in _COPYRIGHT_RE.findall(low)]
    if years:
        newest = max(years)
        this_year = time.localtime().tm_year
        if newest <= this_year - 2:
            return "stale", f"📅 Stale site (© {newest})", ""

    return "", "", ""


def analyze_website(url, max_extra_pages=3, timeout=15):
    """Fetch a business website ONCE and return both contact emails and
    buying-intent signals. Returns a dict:
        {emails: [...], intent_kind, intent, builder}"""
    out = {"emails": [], "intent_kind": "", "intent": "", "builder": "",
           "facebook": "", "instagram": ""}
    if not url:
        return out
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        base = r.url
        html = r.text
    except requests.RequestException:
        return out

    # --- intent (from the homepage) ---
    kind, label, builder = _detect_intent(html, base)
    out["intent_kind"], out["intent"], out["builder"] = kind, label, builder

    # --- social profiles (from the homepage) ---
    out["facebook"] = _first_social(html, _FB_RE, _FB_SKIP)
    out["instagram"] = _first_social(html, _IG_RE, _IG_SKIP)

    # --- emails (homepage + contact/about pages) ---
    emails = set(_emails_from_html(html))
    links = []
    for href in _HREF_RE.findall(html):
        low = href.lower()
        if any(w in low for w in _CONTACT_WORDS):
            full = urljoin(base, href)
            if full not in links:
                links.append(full)
    for link in links[:max_extra_pages]:
        try:
            rr = requests.get(link, headers=_UA, timeout=timeout)
            emails |= _emails_from_html(rr.text)
        except requests.RequestException:
            continue

    dom = base.split("//")[-1].split("/")[0].replace("www.", "")

    def rank(e):
        on_domain = dom and e.endswith("@" + dom)
        role = e.split("@")[0] in ("info", "contact", "hello", "office", "sales", "admin")
        return (0 if on_domain else 1, 0 if role else 1, e)

    out["emails"] = sorted(emails, key=rank)
    return out


# --------------------------------------------------------------------------
# 3. LEAD CLASSIFICATION  (worse website => higher lead_score)
# --------------------------------------------------------------------------
def classify(lead):
    flags = []

    # No website at all = the hottest lead.
    if not lead["website"]:
        lead["website_status"] = "No Website"
        lead["lead_quality"] = "🔥 Hot"
        lead["lead_score"] = 100
        lead["flags"] = "No website"
        return lead

    # Had a website but PSI could not load it = broken/down site.
    if lead.get("perf") is None and lead.get("_psi_error"):
        lead["website_status"] = "Site Broken / Unreachable"
        lead["lead_quality"] = "🔥 Hot"
        lead["lead_score"] = 95
        lead["flags"] = lead.get("_psi_error", "unreachable")
        return lead

    # Strong buying-intent signals OVERRIDE the speed score — a "coming soon"
    # page is fast (would look "Good") but is actually the hottest lead.
    ik = lead.get("intent_kind")
    if ik == "coming_soon":
        lead["website_status"] = "🚧 Coming Soon page"
        lead["lead_quality"] = "🔥 Hot"
        lead["lead_score"] = 93
        lead["flags"] = "In-market: building a website now"
        return lead
    if ik == "parked":
        lead["website_status"] = "🅿 Domain parked (no real site)"
        lead["lead_quality"] = "🔥 Hot"
        lead["lead_score"] = 91
        lead["flags"] = "Has domain, no website yet"
        return lead

    perf = lead.get("perf") or 0
    seo = lead.get("seo") or 0

    if lead.get("https") is False:
        flags.append("No HTTPS")
    if perf < 50:
        flags.append("Slow (perf<50)")
    if seo < 80:
        flags.append("Weak SEO")
    if (lead.get("accessibility") or 100) < 70:
        flags.append("Poor accessibility")

    # DIY builder / stale site = weaker intent signals; note + small boost.
    if ik == "diy":
        flags.append(f"DIY builder ({lead.get('builder') or '—'})")
    elif ik == "stale":
        flags.append(lead.get("intent", "Stale site"))

    # Active on Facebook + a weak website = they value online presence and
    # would invest in a better site. Strong combined signal.
    if lead.get("facebook"):
        flags.append("📘 Active on Facebook")

    # Base score: the slower the site, the higher the lead score.
    score = 100 - perf
    if lead.get("https") is False:
        score += 15
    if seo < 80:
        score += 5
    if ik == "diy":
        score += 10
    elif ik == "stale":
        score += 8
    score = max(0, min(99, score))  # keep below no-website(100)/broken(95)

    # DIY/stale signals also bump a borderline lead up to at least Warm.
    if perf < 50 or lead.get("https") is False:
        quality = "🔥 Hot"
        status = "Bad Website"
    elif perf < 75 or ik in ("diy", "stale"):
        quality = "🟠 Warm"
        status = "Mediocre Website" if perf < 75 else "Upgrade candidate"
    else:
        quality = "🟢 Good (skip)"
        status = "Good Website"

    lead["website_status"] = status
    lead["lead_quality"] = quality
    lead["lead_score"] = score
    lead["flags"] = ", ".join(flags) if flags else "—"
    return lead


# --------------------------------------------------------------------------
# 4. ORCHESTRATION
# --------------------------------------------------------------------------
def run_search(niches, cities, api_key, max_per_query=60,
               run_pagespeed=True, seen_ids=None, progress=None,
               keep_good=False, find_emails=True,
               min_reviews=0, max_reviews=0,
               apollo_key=None, emp_min=0, emp_max=0,
               rev_min=0, rev_max=0, apollo_strict=False):
    """Loop niches x cities, collect + score + classify leads.

    `progress(stage, done, total, msg)` is an optional callback for UI updates.
    `keep_good=False` drops 🟢 Good (skip) leads from the result, so only
    🔥 Hot + 🟠 Warm leads remain — the businesses actually worth working.
    `find_emails=True` scrapes each website for contact emails (free).
    `min_reviews`/`max_reviews` filter by review count (size proxy; 0 = no limit).
    `apollo_key` enables firmographic enrichment; `emp_*`/`rev_*` set the target
    employee/revenue ranges. `apollo_strict=False` keeps leads Apollo has no
    data for (e.g. no-website shops) instead of dropping them.
    Returns (leads, errors)."""
    seen_ids = seen_ids or set()
    errors = []
    leads = []
    found_ids = set()

    combos = [(n.strip(), c.strip())
              for n in niches if n.strip()
              for c in cities if c.strip()]

    def emit(stage, done, total, msg=""):
        if progress:
            progress(stage, done, total, msg)

    # --- gather places ---
    for i, (niche, city) in enumerate(combos, 1):
        query = f"{niche} in {city}"
        emit("search", i, len(combos), query)
        if i > 1:
            time.sleep(0.5)  # gentle gap so bursts don't trip a 403/quota
        try:
            raw = search_places(query, api_key, max_per_query)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{query}: {e}")
            continue
        for r in raw:
            lead = normalize_place(r, niche, city)
            pid = lead["place_id"]
            if not pid or pid in found_ids or pid in seen_ids:
                continue
            found_ids.add(pid)
            leads.append(lead)

    # --- size proxy: filter by review count BEFORE the costly analysis ---
    if min_reviews or max_reviews:
        def in_review_band(l):
            rc = l.get("reviews") or 0
            if min_reviews and rc < min_reviews:
                return False
            if max_reviews and rc > max_reviews:
                return False
            return True
        leads = [l for l in leads if in_review_band(l)]

    # --- analyze websites concurrently (PageSpeed + intent + email scrape) ---
    # One homepage fetch per site covers BOTH intent signals and emails.
    with_site = [l for l in leads if l["website"]]
    if with_site:
        total = len(with_site)
        done = 0
        emit("score", done, total, "Analyzing websites...")

        def analyze(lead):
            out = {}
            if run_pagespeed:
                out["psi"] = score_website(lead["website"], api_key)
            out["site"] = analyze_website(lead["website"])
            if apollo_key:
                out["apollo"] = apollo_enrich(_domain_of(lead["website"]), apollo_key)
            return out

        with ThreadPoolExecutor(max_workers=8) as pool:
            future_map = {pool.submit(analyze, l): l for l in with_site}
            for fut in as_completed(future_map):
                lead = future_map[fut]
                try:
                    out = fut.result()
                except Exception as e:  # noqa: BLE001
                    out = {"psi": {"error": str(e)}}
                if "psi" in out:
                    res = out["psi"]
                    if "error" in res:
                        lead["_psi_error"] = res["error"]
                    else:
                        lead.update(res)
                site = out.get("site", {})
                lead["intent_kind"] = site.get("intent_kind", "")
                lead["intent"] = site.get("intent", "")
                lead["builder"] = site.get("builder", "")
                lead["facebook"] = site.get("facebook", "")
                lead["instagram"] = site.get("instagram", "")
                if find_emails:
                    ems = site.get("emails", [])
                    lead["email"] = ems[0] if ems else ""
                    lead["emails"] = ", ".join(ems)
                apo = out.get("apollo") or {}
                if apo:
                    lead["employees"] = apo.get("employees") or ""
                    lead["revenue"] = apo.get("revenue") or ""
                    lead["revenue_str"] = apo.get("revenue_str") or ""
                    lead["industry"] = apo.get("industry") or ""
                done += 1
                emit("score", done, total, lead["name"])

    for lead in leads:
        classify(lead)
        lead.pop("_psi_error", None)

    # Drop the good websites — we have to score them to know they're good,
    # but there's no reason to keep/export them. Only Hot + Warm remain.
    if not keep_good:
        leads = [l for l in leads if "Good" not in l["lead_quality"]]

    # Apollo size filter (only when enrichment ran AND a range is set).
    # Soft by default: leads with NO Apollo data are kept (they're often the
    # best — no-website shops Apollo doesn't track). Strict drops them.
    if apollo_key and (emp_min or emp_max or rev_min or rev_max):
        def size_ok(l):
            emp = l.get("employees")
            rev = l.get("revenue")
            has_data = isinstance(emp, (int, float)) or isinstance(rev, (int, float))
            if not has_data:
                return not apollo_strict
            if isinstance(emp, (int, float)):
                if emp_min and emp < emp_min:
                    return False
                if emp_max and emp > emp_max:
                    return False
            if isinstance(rev, (int, float)):
                if rev_min and rev < rev_min:
                    return False
                if rev_max and rev > rev_max:
                    return False
            return True
        leads = [l for l in leads if size_ok(l)]

    leads.sort(key=lambda x: x["lead_score"], reverse=True)
    return leads, errors
