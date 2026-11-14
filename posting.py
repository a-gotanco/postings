#!/usr/bin/env python3
"""
Posting — a live log of new LinkedIn job postings.

Polls LinkedIn's public (logged-out) job search for the job titles and
locations listed in config.json (editable from the website's Settings panel),
keeps a running log in jobs.json, and serves a dashboard at http://localhost:8787.

No login, no API keys, no installs: Python 3 standard library only.

Run locally:   python3 posting.py          (dashboard at http://localhost:8787)
Run once:      python3 posting.py --once   (used by GitHub Actions; updates jobs.json and exits)
"""

import html
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# Config — edit freely
# ----------------------------------------------------------------------------

PORT = 8787
POLL_MINUTES = 30
PAGES_PER_QUERY = 2          # 25 results per page
LOOKBACK = "r86400"          # only postings from the past 24h ("r3600" = past hour)
KEEP_DAYS = 30               # drop jobs from the log after this many days

# ----------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "jobs.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
INDEX_FILE = os.path.join(HERE, "index.html")
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

DEFAULT_CONFIG = {
    "titles": [
        {"id": "corpdev", "name": "Corporate Development", "also_match": ["corp dev", "corp development", "corpdev"]},
        {"id": "stratfin", "name": "Strategic Finance"},
        {"id": "invassoc", "name": "Investment Associate"},
    ],
    "locations": [
        {"id": "sf", "name": "San Francisco", "search": "San Francisco Bay Area"},
        {"id": "chi", "name": "Chicago", "search": "Greater Chicago Area"},
        {"id": "nyc", "name": "New York", "search": "New York City Metropolitan Area"},
    ],
}

lock = threading.Lock()
state = {"jobs": {}, "last_run": None, "next_run": None, "status": "starting", "error": None}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- config -----

def load_config():
    """Read config.json (re-read every check, so edits apply on the next run)."""
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    titles = [t for t in cfg.get("titles", []) if isinstance(t, dict) and t.get("id") and t.get("name", "").strip()]
    locs = [l for l in cfg.get("locations", []) if isinstance(l, dict) and l.get("id") and l.get("name", "").strip()]
    return {"titles": titles or DEFAULT_CONFIG["titles"], "locations": locs or DEFAULT_CONFIG["locations"]}


def title_pattern(t):
    """A job's title must contain the title phrase (punctuation between words is fine) or an also_match phrase."""
    phrases = [t["name"]] + list(t.get("also_match") or [])
    parts = []
    for ph in phrases:
        words = re.findall(r"[\w&+]+", ph.lower())
        if words:
            parts.append(r"\b" + r"\W+".join(map(re.escape, words)) + r"\b")
    return re.compile("|".join(parts) or r"$^", re.I)


def is_remote(loc):
    return loc.get("search", loc["name"]).strip().lower() in ("remote", "remote, us", "remote us")


# ---------------------------------------------------------------- storage ----

def load():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                jobs = json.load(f).get("jobs", [])
            state["jobs"] = {j["id"]: j for j in jobs} if isinstance(jobs, list) else jobs
        except (OSError, ValueError, KeyError, TypeError):
            log("jobs.json unreadable — starting fresh")


def payload():
    cfg = load_config()
    return {
        "jobs": sorted(state["jobs"].values(), key=lambda j: j["first_seen"], reverse=True),
        "categories": {t["id"]: t["name"] for t in cfg["titles"]},
        "metros": {l["id"]: l["name"] for l in cfg["locations"]},
        "last_run": state["last_run"], "next_run": state["next_run"],
        "status": state["status"], "error": state["error"],
        "poll_minutes": POLL_MINUTES,
    }


def save():
    cutoff = time.time() - KEEP_DAYS * 86400
    state["jobs"] = {k: j for k, j in state["jobs"].items()
                     if datetime.fromisoformat(j["first_seen"]).timestamp() >= cutoff}
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload(), f, indent=1)
    os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------- scraping ---

def text(fragment):
    """Strip tags + collapse whitespace."""
    if fragment is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def grab(pattern, chunk):
    m = re.search(pattern, chunk, re.S)
    return m.group(1) if m else None


def parse_cards(page_html):
    """Parse LinkedIn guest job-search HTML into dicts."""
    out = []
    for chunk in re.split(r"<li[\s>]", page_html)[1:]:
        jid = grab(r"urn:li:jobPosting:(\d+)", chunk) or grab(r"/jobs/view/[^\"?]*?-(\d+)(?:[?\"])", chunk)
        if not jid:
            continue
        title = text(grab(r'class="base-search-card__title"[^>]*>(.*?)</h3>', chunk))
        company = text(grab(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', chunk))
        location = text(grab(r'class="job-search-card__location"[^>]*>(.*?)</span>', chunk))
        posted = grab(r'<time[^>]*datetime="([^"]+)"', chunk)
        posted_label = text(grab(r"<time[^>]*>(.*?)</time>", chunk))
        url = grab(r'class="base-card__full-link[^"]*"[^>]*href="([^"]+)"', chunk) \
            or grab(r'href="(https://[^"]*/jobs/view/[^"]+)"', chunk)
        if url:
            url = html.unescape(url).split("?")[0]
        else:
            url = f"https://www.linkedin.com/jobs/view/{jid}"
        logo = grab(r'data-delayed-url="([^"]+)"', chunk)
        if title:
            out.append({"id": jid, "title": title, "company": company or "—",
                        "location": location, "posted": posted, "posted_label": posted_label,
                        "url": url, "logo": html.unescape(logo) if logo else None})
    return out


def fetch(keywords, location, start, remote=False):
    params = {"keywords": keywords, "location": location, "f_TPR": LOOKBACK,
              "sortBy": "DD", "start": start}
    if remote:
        params["f_WT"] = "2"         # LinkedIn's "Remote" workplace filter
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{SEARCH_URL}?{qs}", headers={
        "User-Agent": UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 400:          # past the last page
                return ""
            if e.code in (429, 999) and attempt < 2:
                wait = 30 * (attempt + 1)
                log(f"  rate-limited ({e.code}); waiting {wait}s")
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(5)
                continue
            raise
    return ""


def run_once():
    with lock:
        state["status"] = "checking"
    cfg = load_config()
    added, errors = 0, []
    searches = len(cfg["titles"]) * len(cfg["locations"])
    pages = PAGES_PER_QUERY if searches <= 20 else 1      # keep big configs inside the time limit
    for t in cfg["titles"]:
        cat_id, cat_name = t["id"], t["name"].strip()
        keywords = f'"{cat_name}"'
        pattern = title_pattern(t)
        for l in cfg["locations"]:
            metro_id, metro_name = l["id"], l["name"].strip()
            remote = is_remote(l)
            loc = "United States" if remote else (l.get("search") or metro_name).strip()
            for page in range(pages):
                try:
                    cards = parse_cards(fetch(keywords, loc, page * 25, remote))
                except Exception as e:  # keep going on other queries
                    errors.append(f"{cat_name} / {metro_name}: {e}")
                    break
                for c in cards:
                    if not pattern.search(c["title"]):
                        continue       # keyword matched the description, not the title
                    with lock:
                        if c["id"] in state["jobs"]:
                            continue
                        c.update(category=cat_id, metro=metro_id, first_seen=now_iso())
                        state["jobs"][c["id"]] = c
                        added += 1
                if len(cards) < 10:
                    break
                time.sleep(random.uniform(1, 2))
            time.sleep(random.uniform(1, 2))
    with lock:
        state["last_run"] = now_iso()
        state["status"] = "live"
        state["error"] = "; ".join(errors[:3]) if errors else None
        save()
    log(f"check done: {added} new, {len(state['jobs'])} total" + (f" ({len(errors)} errors)" if errors else ""))
    return len(errors) < searches   # True if at least one search worked


def scraper_loop():
    while True:
        try:
            run_once()
        except Exception as e:
            with lock:
                state["status"], state["error"] = "error", str(e)
            log(f"check failed: {e}")
        nxt = time.time() + POLL_MINUTES * 60
        with lock:
            state["next_run"] = datetime.fromtimestamp(nxt, timezone.utc).isoformat(timespec="seconds")
        time.sleep(max(0, nxt - time.time()))


# ---------------------------------------------------------------- server -----

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(INDEX_FILE, "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path in ("/jobs.json", "/api/jobs"):
            with lock:
                body = json.dumps(payload()).encode()
            self.send(200, body, "application/json")
        elif path == "/config.json":
            self.send(200, json.dumps(load_config()).encode(), "application/json")
        else:
            self.send(404, b"not found", "text/plain")


def main():
    load()
    if "--once" in sys.argv:
        state["next_run"] = datetime.fromtimestamp(time.time() + POLL_MINUTES * 60,
                                                   timezone.utc).isoformat(timespec="seconds")
        ok = run_once()
        if not ok:
            log("every search failed — LinkedIn may be blocking this machine")
        sys.exit(0 if ok else 1)
    threading.Thread(target=scraper_loop, daemon=True).start()
    log(f"Posting is running → http://localhost:{PORT}  (checks every {POLL_MINUTES} min, Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
