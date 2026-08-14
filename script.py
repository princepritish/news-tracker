import feedparser
import hashlib
import json
import re
import sqlite3
import requests
import os
from datetime import datetime, timedelta, timezone
from newspaper import Article
from groq import Groq

# ---------------- LOAD CONFIG ----------------
with open("config.json") as f:
    config = json.load(f)

SITES = config["sites"]
FILTERS = config["filters"]

KEYWORDS = [k.lower() for k in FILTERS.get("keywords", ["solar", "battery energy storage system", "bess"])]

# -------- STATE NORMALIZATION --------
STATE_ALIASES = {
    "jharkhand": ["jharkhand"],
    "bihar": ["bihar"],
    "odisha": ["odisha", "orissa"],
    "assam": ["assam"],
    "chhattisgarh": ["chhattisgarh", "chattisgarh"],
    "madhya pradesh": ["madhya pradesh"],
    "andhra pradesh": ["andhra pradesh"],
    "uttar pradesh": ["uttar pradesh"]
}

print("\n[INIT] Keywords:", KEYWORDS)

# ---------------- ENV VARIABLES ----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_BCC = os.getenv("EMAIL_BCC")

# DEDUP_MODE controls what happens with the clustering results:
#   shadow (default) - cluster and log, but email one-per-article as before
#   on               - cluster and send a single grouped digest
#   off              - skip clustering entirely, email one-per-article
# Shadow is the default so that deploying this never changes the inbox on its own.
DEDUP_MODE = os.getenv("DEDUP_MODE", "shadow").strip().lower()
if DEDUP_MODE not in ("shadow", "on", "off"):
    print(f"[INIT] Unknown DEDUP_MODE {DEDUP_MODE!r}, falling back to 'shadow'")
    DEDUP_MODE = "shadow"

# Clustering is one call per run, so it can afford a stronger model than the
# per-article calls. Strict JSON partitioning is where small models slip.
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "llama-3.1-8b-instant")
CLUSTER_MODEL = os.getenv("CLUSTER_MODEL", "llama-3.3-70b-versatile")
DEDUP_WINDOW_DAYS = int(os.getenv("DEDUP_WINDOW_DAYS", "7"))
MAX_ENTRIES_PER_SITE = int(os.getenv("MAX_ENTRIES_PER_SITE", "30"))

if not GROQ_API_KEY or not BREVO_API_KEY:
    raise Exception("Missing API keys")

if not EMAIL_TO:
    raise Exception("EMAIL_TO not set")

# -------- EMAIL PARSE --------
TO_EMAILS = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
if len(TO_EMAILS) < 2:
    raise Exception("EMAIL_TO must contain at least 2 comma-separated emails")

BCC_EMAILS = [EMAIL_BCC.strip()] if EMAIL_BCC and EMAIL_BCC.strip() else []

print("[EMAIL INIT] TO:", TO_EMAILS)
print("[EMAIL INIT] BCC:", BCC_EMAILS)
print("[INIT] DEDUP_MODE:", DEDUP_MODE)
print("[INIT] Cluster model:", CLUSTER_MODEL)

# ---------------- INIT CLIENT ----------------
client = Groq(api_key=GROQ_API_KEY)

# ---------------- DATABASE ----------------
DB_PATH = "/data/seen.db" if os.path.exists("/data") else "seen.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("CREATE TABLE IF NOT EXISTS articles (hash TEXT PRIMARY KEY)")

# Numeric fingerprints give stable cross-run identity for a story. LLM clustering
# is not deterministic between runs, so it cannot answer "did we send this before".
c.execute("""
CREATE TABLE IF NOT EXISTS stories (
    fingerprint TEXT PRIMARY KEY,
    title       TEXT,
    url         TEXT,
    state       TEXT,
    first_seen  TEXT
)
""")
conn.commit()

print("[DB] Using:", DB_PATH)


def already_seen(url):
    h = hashlib.md5(url.encode()).hexdigest()
    c.execute("SELECT 1 FROM articles WHERE hash=?", (h,))
    return c.fetchone() is not None


def mark_seen(url):
    """Record a URL as handled.

    Deliberately separate from already_seen(): an article is only marked once it
    has actually been judged, so a scrape failure stays retryable on the next run
    instead of being burned permanently.
    """
    h = hashlib.md5(url.encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO articles VALUES (?)", (h,))
    conn.commit()

# ---------------- BREVO EMAIL ----------------
def send_email(subject, body):
    print("[EMAIL] Sending via Brevo...")

    url = "https://api.brevo.com/v3/smtp/email"

    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }

    payload = {
        "sender": {
            "name": "Solar Alerts",
            "email": "princepritish26@gmail.com"   # 🔴 CHANGE THIS to your verified sender
        },
        "to": [{"email": e} for e in TO_EMAILS],
        "subject": subject,
        "textContent": body
    }

    if BCC_EMAILS:
        payload["bcc"] = [{"email": e} for e in BCC_EMAILS]

    try:
        res = requests.post(url, json=payload, headers=headers)

        print("[EMAIL STATUS]", res.status_code)
        print("[EMAIL RESPONSE]", res.text)

    except Exception as e:
        print("[EMAIL ERROR]", e)

# ---------------- STATE DETECTION ----------------
def flatten(text):
    """Lowercase and reduce punctuation to single spaces.

    Trade publications write these product terms inconsistently - "pre-GI",
    "pre GI", "Hot-Dip", "hot dip" - so both sides of the comparison get
    flattened rather than relying on the exact spelling in the keyword list.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


FLAT_KEYWORDS = [flatten(k) for k in KEYWORDS]


def matches_topic(text):
    flat = flatten(text)
    return any(keyword in flat for keyword in FLAT_KEYWORDS)


def find_state(text):
    text = text.lower()

    if not matches_topic(text):
        return None

    for state, aliases in STATE_ALIASES.items():
        for name in aliases:
            if name in text:
                return state

    return None

# ---------------- LLM STATE EXTRACTION ----------------
def llm_extract_state(title, content):
    """Ask the model which target state an article concerns.

    Returns a normalized state key or None. This replaces the old YES/NO
    llm_detect(): a YES verdict carried no state, and the email subject needs
    one, so every tier-3 article was discarded regardless of the answer.
    """
    print("[LLM] Extracting state...")

    states = ", ".join(STATE_ALIASES.keys())

    try:
        prompt = f"""
You are given a news article. Reply with EXACTLY ONE line and nothing else.

Reply with one of these state names if the article is about SOLAR energy or
battery energy storage (BESS) AND concerns that state - including when the state
is only implied by a city, district, project or region within it:
{states}

Otherwise reply: NONE

Article:
{title}
{content[:2000]}
"""

        response = client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )

        answer = response.choices[0].message.content.strip().lower()
        print("[LLM]", answer[:60])

        for state, aliases in STATE_ALIASES.items():
            if any(name in answer for name in aliases):
                return state

        return None

    except Exception as e:
        print("[LLM ERROR]", e)
        return None

# ---------------- NUMERIC FINGERPRINT ----------------
UNIT_TO_MW = {"gw": 1000, "mw": 1, "kw": 0.001, "gwh": 1000, "mwh": 1, "kwh": 0.001}
MONEY_MULT = {"crore": 1e7, "cr": 1e7, "lakh": 1e5,
              "billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6}

NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(gwh|mwh|kwh|gw|mw|kw)\b")
MONEY_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d.]+)\s*(crore|cr|lakh|billion|bn|million|mn)\b")


def numeric_fingerprint(text):
    """Capacity and money figures, normalized to common units.

    Numbers are the highest-signal, lowest-noise feature in energy trade news:
    "1200 MW" is specific and survives any rewording, while "solar" is in almost
    every headline. Bare years are never captured - only figures with units - so
    "2026" cannot match everything to everything.
    """
    text = re.sub(r"(\d),(\d)", r"\1\2", text.lower())

    out = set()

    for value, unit in NUM_UNIT_RE.findall(text):
        kind = "e" if unit.endswith("h") else "p"   # energy vs power
        out.add(f"{kind}{round(float(value) * UNIT_TO_MW[unit])}")

    for value, unit in MONEY_RE.findall(text):
        out.add(f"m{round(float(value) * MONEY_MULT[unit])}")

    return out


def fingerprint_key(fingerprint):
    return "|".join(sorted(fingerprint))


def classify_against_history(fingerprint):
    """Return one of 'new', 'suppress', 'update' for a story fingerprint.

    Suppression demands an exact fingerprint match inside the window. A story with
    no extractable numbers is never suppressed - when unsure, send.
    """
    if not fingerprint:
        return "new", None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    c.execute("SELECT fingerprint, title FROM stories WHERE first_seen >= ?", (cutoff,))

    for stored_key, stored_title in c.fetchall():
        stored = set(stored_key.split("|")) if stored_key else set()

        if stored == fingerprint:
            return "suppress", stored_title

        # Same story carrying figures we have not seen: a tender extended, a
        # winner named, capacity revised. That is news, not a duplicate.
        if stored and stored.issubset(fingerprint):
            return "update", stored_title

    return "new", None


def record_story(item):
    key = fingerprint_key(item["fingerprint"])
    if not key:
        return
    c.execute(
        "INSERT OR IGNORE INTO stories VALUES (?, ?, ?, ?, ?)",
        (key, item["title"], item["url"], item["state"],
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

# ---------------- SUMMARY ----------------
def summarize(title, content):
    print("[SUMMARY] Generating...")

    try:
        prompt = f"""
Summarize in:
- 3 bullet points
- 1 line why it matters

Article:
{title}
{content[:2000]}
"""

        response = client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        print("[SUMMARY ERROR]", e)
        return "Summary unavailable"

# ---------------- CLUSTERING ----------------
def cluster_stories(items):
    """Group items reporting the same underlying event. One LLM call per run.

    Fails open: on bad JSON, a missing or repeated index, or any API error, every
    item becomes its own cluster and nothing is merged. A duplicate email is a
    nuisance; a story silently dropped because a parse failed is the real damage.
    """
    singletons = [[i] for i in range(len(items))]

    if len(items) < 2:
        return singletons

    listing = "\n".join(
        f"{i}. {it['title']} — {it['summary_text'][:150]}"
        for i, it in enumerate(items)
    )

    prompt = f"""
Group these energy-news items by the underlying EVENT.

Two items belong together only if they report the SAME specific event - the same
tender, project, commissioning or announcement. Same topic is NOT enough: two
different 500 MW solar tenders are different events.

Return ONLY JSON in this exact form:
{{"groups": [[0, 3], [1], [2, 4]]}}

Every index from 0 to {len(items) - 1} must appear exactly once.

{listing}
"""

    try:
        response = client.chat.completions.create(
            model=CLUSTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        groups = json.loads(raw).get("groups", [])

        flat = sorted(i for g in groups for i in g)
        if flat != list(range(len(items))):
            print(f"[CLUSTER] Invalid partition ({len(flat)} indices for "
                  f"{len(items)} items) -> failing open")
            return singletons

        print(f"[CLUSTER] {len(items)} items -> {len(groups)} clusters")
        return groups

    except Exception as e:
        print("[CLUSTER ERROR]", e, "-> failing open")
        return singletons

# ---------------- PROCESS SITE ----------------
def process_site(site):
    """Collect matching articles. Sending happens once per run, in main()."""
    print(f"\n===== {site['name']} =====")

    matches = []

    try:
        r = requests.get(site["rss"], headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        feed = feedparser.parse(r.content)
        print("[FEED] Entries:", len(feed.entries))
    except Exception as e:
        print("[FEED ERROR]", e)
        return matches

    for i, entry in enumerate(feed.entries[:MAX_ENTRIES_PER_SITE], 1):
        print(f"\n--- Article {i} ---")

        title = entry.get("title", "")
        url = entry.get("link", "")
        desc = entry.get("summary", "")

        print("[TITLE]", title)

        if not url or already_seen(url):
            print("[DB] Duplicate")
            continue

        state = find_state(title + " " + desc)

        if state:
            print("[FAST MATCH]", state)
            content = desc

        elif not matches_topic(title + " " + desc):
            # Nothing in the headline or blurb suggests solar/BESS, so don't pay
            # for a scrape and an LLM call to find out. Without this gate every
            # off-topic article on every feed costs both - at 40+ feeds that is
            # thousands of scrapes and LLM calls per run.
            print("[SKIP] Off topic")
            mark_seen(url)
            continue

        else:
            print("[SCRAPE] Fetching full article...")
            try:
                article = Article(url)
                article.download()
                article.parse()
                content = article.text
            except Exception as e:
                # Not marked seen: a transient scrape failure must stay retryable.
                print("[SCRAPE ERROR]", e)
                continue

            state = find_state(content)

            if state:
                print("[FULL TEXT MATCH]", state)
            else:
                state = llm_extract_state(title, content)
                if not state:
                    print("[SKIP] Not relevant")
                    mark_seen(url)
                    continue
                print("[LLM MATCH]", state)

        mark_seen(url)

        matches.append({
            "site": site["name"],
            "title": title,
            "url": url,
            "state": state,
            "content": content,
            "summary_text": desc or content[:300],
            "fingerprint": numeric_fingerprint(f"{title} {content[:1500]}"),
        })

    return matches

# ---------------- DIGEST ----------------
def build_digest(clusters):
    by_state = {}
    for cluster in clusters:
        by_state.setdefault(cluster["primary"]["state"] or "unspecified", []).append(cluster)

    lines = []
    for state, entries in sorted(by_state.items()):
        lines.append("=" * 60)
        lines.append(state.upper())
        lines.append("=" * 60)

        for cluster in entries:
            primary = cluster["primary"]
            tag = "[UPDATE] " if cluster["is_update"] else ""
            lines.append(f"\n{tag}{primary['title']}\n")
            lines.append(cluster["summary"])
            lines.append(f"\nLink: {primary['url']}")

            others = sorted({o["site"] for o in cluster["others"]})
            if others:
                lines.append(f"Also covered by: {', '.join(others)}")
            lines.append("")

    return "\n".join(lines)


def send_individually(items):
    """The original one-email-per-article behaviour."""
    for item in items:
        summary = summarize(item["title"], item["content"])
        body = f"""
Title: {item['title']}

State: {item['state']}

Summary:
{summary}

Link: {item['url']}
"""
        send_email(f"Energy Alert - {item['state']}", body)

# ---------------- MAIN ----------------
def main():
    print("\n[SYSTEM] Running one cycle\n")

    collected = []
    for site in SITES:
        collected.extend(process_site(site))

    print(f"\n[PHASE 2] {len(collected)} matched article(s)")

    if not collected:
        print("[SYSTEM] Nothing to send\n")
        return

    if DEDUP_MODE == "off":
        send_individually(collected)
        for item in collected:
            record_story(item)
        print("\n[SYSTEM] Done\n")
        return

    # Cross-run suppression before clustering, so repeats never reach the LLM.
    fresh = []
    for item in collected:
        status, prior = classify_against_history(item["fingerprint"])
        item["is_update"] = status == "update"

        if status == "suppress":
            print(f"[DEDUP] Suppress (seen {DEDUP_WINDOW_DAYS}d): {item['title'][:60]}")
            if DEDUP_MODE == "shadow":
                fresh.append(item)      # shadow logs the decision, sends anyway
            continue

        if status == "update":
            print(f"[DEDUP] Update of: {str(prior)[:60]}")

        fresh.append(item)

    if not fresh:
        print("[PHASE 2] Everything suppressed as already-sent, no email")
        print("\n[SYSTEM] Done\n")
        return

    groups = cluster_stories(fresh)

    clusters = []
    for group in groups:
        members = [fresh[i] for i in group]
        # The fullest article makes the best summary source.
        primary = max(members, key=lambda m: len(m["content"]))
        clusters.append({
            "primary": primary,
            "others": [m for m in members if m is not primary],
            "is_update": any(m.get("is_update") for m in members),
            "summary": None,
        })

    duplicates_found = sum(len(c["others"]) for c in clusters)
    print(f"[PHASE 2] {len(fresh)} items -> {len(clusters)} stories "
          f"({duplicates_found} duplicate(s) merged)")

    for cluster in clusters:
        if cluster["others"]:
            print(f"[CLUSTER] {cluster['primary']['title'][:55]}")
            for other in cluster["others"]:
                print(f"          + [{other['site']}] {other['title'][:50]}")

    if DEDUP_MODE == "shadow":
        # Log only: the inbox stays exactly as it was before this change.
        print("[SHADOW] Clustering logged, sending one email per article as before")
        send_individually(collected)
        for item in collected:
            record_story(item)
        print("\n[SYSTEM] Done\n")
        return

    for cluster in clusters:
        primary = cluster["primary"]
        cluster["summary"] = summarize(primary["title"], primary["content"])

    send_email(
        f"Energy Digest - {len(clusters)} story(ies)",
        build_digest(clusters),
    )

    for cluster in clusters:
        record_story(cluster["primary"])

    print("\n[SYSTEM] Done\n")


if __name__ == "__main__":
    main()
