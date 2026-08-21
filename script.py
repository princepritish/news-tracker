"""Daily solar / BESS news digest.

Runs once a day on Railway. Walks every configured feed, keeps articles that match
a product keyword AND one of the tracked states, collapses the same story reported
by multiple outlets into one line, and sends a single email.

Design rules, in priority order:
  1. Never drop news silently. Every skip is either logged or counted in the email.
  2. Never repeat news. Two layers: a per-URL seen table, and story-level dedup.
  3. Stay far inside free tiers. Hard caps on scrapes and LLM calls per run.
  4. Always send. A daily email arrives even with no news, so silence means the
     cron itself broke rather than "nothing happened today".
"""

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

# ---------------- CONFIG ----------------
with open("config.json") as f:
    config = json.load(f)

SITES = config["sites"]
FILTERS = config["filters"]

KEYWORDS = [k.lower() for k in FILTERS.get("keywords", [])]

# Spelling variants seen in Indian trade press. The state list itself comes from
# config.json so it stays in one place.
STATE_VARIANTS = {
    "odisha": ["orissa"],
    "chhattisgarh": ["chattisgarh"],
    "uttarakhand": ["uttaranchal"],
}

STATE_ALIASES = {}
for _state in FILTERS.get("allowed_states", []):
    key = _state.lower().strip()
    STATE_ALIASES[key] = [key] + STATE_VARIANTS.get(key, [])

# ---------------- ENV ----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
EMAIL_TO = os.getenv("EMAIL_TO")
EMAIL_BCC = os.getenv("EMAIL_BCC")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "princepritish26@gmail.com")

# Cron runs on Railway share the deployment's filesystem, so a plain relative path
# persists across runs - that is what the current deployment does and it works. A
# new *deployment* replaces the filesystem and the history starts empty again;
# that case is detected at runtime and reported, rather than guessed at from the
# path. Mount a volume and set DB_PATH=/data/seen.db to survive deploys too.
DB_PATH = os.getenv("DB_PATH", "seen.db")

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "llama-3.1-8b-instant")
CLUSTER_MODEL = os.getenv("CLUSTER_MODEL", "llama-3.3-70b-versatile")

MAX_ENTRIES_PER_SITE = int(os.getenv("MAX_ENTRIES_PER_SITE", "40"))
MAX_SCRAPES_PER_RUN = int(os.getenv("MAX_SCRAPES_PER_RUN", "60"))
MAX_LLM_CALLS_PER_RUN = int(os.getenv("MAX_LLM_CALLS_PER_RUN", "60"))
MAX_ITEMS_PER_EMAIL = int(os.getenv("MAX_ITEMS_PER_EMAIL", "60"))
DEDUP_WINDOW_DAYS = int(os.getenv("DEDUP_WINDOW_DAYS", "7"))
FEED_TIMEOUT = int(os.getenv("FEED_TIMEOUT", "20"))

# --- testing switches (all default off, safe to leave unset in production) ---
# DRY_RUN=1      print the email instead of sending it
# SKIP_SEEDING=1 process news even when history is empty, instead of seeding
# ONLY_SITES=n   use just the first n feeds, for a fast rehearsal
DRY_RUN = os.getenv("DRY_RUN") == "1"
SKIP_SEEDING = os.getenv("SKIP_SEEDING") == "1"
ONLY_SITES = int(os.getenv("ONLY_SITES", "0"))

if not GROQ_API_KEY or not BREVO_API_KEY:
    raise Exception("Missing API keys")
if not EMAIL_TO:
    raise Exception("EMAIL_TO not set")

TO_EMAILS = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
if not TO_EMAILS:
    raise Exception("EMAIL_TO contained no usable addresses")

BCC_EMAILS = [EMAIL_BCC.strip()] if EMAIL_BCC and EMAIL_BCC.strip() else []

if ONLY_SITES > 0:
    SITES = SITES[:ONLY_SITES]
    print(f"[INIT] ONLY_SITES={ONLY_SITES} — using first {len(SITES)} feed(s)")

client = Groq(api_key=GROQ_API_KEY)

print(f"[INIT] {len(SITES)} feeds · {len(KEYWORDS)} keywords · {len(STATE_ALIASES)} states")
print(f"[INIT] TO: {TO_EMAILS} · BCC: {BCC_EMAILS}")


# ---------------- MODEL RESOLUTION ----------------
# Model IDs get retired without notice - a hardcoded one silently 404s every call
# and the whole LLM tier goes dead while the run still "succeeds". So ask the API
# what exists and pick from a preference list.
FAST_CANDIDATES = [
    SUMMARY_MODEL, "llama-3.1-8b-instant", "llama-3.3-70b-versatile",
    "llama3-8b-8192", "gemma2-9b-it", "mixtral-8x7b-32768",
]
STRONG_CANDIDATES = [
    CLUSTER_MODEL, "llama-3.3-70b-versatile", "llama3-70b-8192",
    "llama-3.1-8b-instant", "mixtral-8x7b-32768",
]


def resolve_models():
    """Pick usable model IDs, or (None, None) if the catalogue can't be read."""
    try:
        available = {m.id for m in client.models.list().data}
        print(f"[MODELS] {len(available)} available: {sorted(available)}")
    except Exception as e:
        print(f"[MODELS] Could not list models ({e}); using configured defaults")
        return SUMMARY_MODEL, CLUSTER_MODEL, None

    def pick(candidates):
        for name in candidates:
            if name in available:
                return name
        return None

    # The named lists can go stale as Groq retires model IDs. Rather than give up
    # and disable the LLM tier while usable models sit in the catalogue, fall back
    # to anything that looks like a chat model - excluding the speech, safety and
    # embedding models, which cannot answer a prompt.
    NON_CHAT = ("whisper", "tts", "guard", "embed", "moderation", "rerank")

    def any_chat(prefer_large):
        chat = sorted(m for m in available
                      if not any(x in m.lower() for x in NON_CHAT))
        if not chat:
            return None
        large = [m for m in chat if any(s in m.lower() for s in ("70b", "-large", "32b"))]
        if prefer_large and large:
            return large[0]
        small = [m for m in chat if any(s in m.lower() for s in ("8b", "9b", "instant", "mini"))]
        return (small or chat)[0]

    fast = pick(FAST_CANDIDATES) or any_chat(prefer_large=False)
    strong = pick(STRONG_CANDIDATES) or any_chat(prefer_large=True) or fast

    if fast and fast not in FAST_CANDIDATES:
        print(f"[MODELS] No known model available; falling back to {fast!r}")

    note = None
    if not fast:
        note = (
            "LLM UNAVAILABLE: none of the expected Groq models are accessible on "
            "this API key, so articles that do not name a state in their text "
            "could not be classified and story-grouping was skipped. Check "
            "console.groq.com/docs/models and set SUMMARY_MODEL / CLUSTER_MODEL."
        )
        print("[MODELS] WARNING: no usable model found")
    else:
        if fast != SUMMARY_MODEL:
            print(f"[MODELS] SUMMARY_MODEL {SUMMARY_MODEL!r} unavailable -> {fast!r}")
        if strong != CLUSTER_MODEL:
            print(f"[MODELS] CLUSTER_MODEL {CLUSTER_MODEL!r} unavailable -> {strong!r}")

    return fast, strong, note


FAST_MODEL, STRONG_MODEL, model_warning = resolve_models()


# ---------------- RUN STATE ----------------
class Budget:
    """Hard caps so a bad day cannot run up API usage or run time."""

    def __init__(self):
        self.scrapes = 0
        self.llm_calls = 0
        self.deferred = 0      # articles left unjudged, deliberately not marked seen

    def can_scrape(self):
        return self.scrapes < MAX_SCRAPES_PER_RUN

    def can_call_llm(self):
        return self.llm_calls < MAX_LLM_CALLS_PER_RUN


budget = Budget()
feed_errors = []


# ---------------- DATABASE ----------------
def open_db():
    """Return (connection, health_note). Never raises - a storage problem must
    still produce an email, with the problem stated in it.

    Whether the file survives is a property of the deployment, not of the path,
    so this makes no attempt to infer it. An empty table is reported by the run
    itself, which is the observable fact that actually matters.
    """
    directory = os.path.dirname(DB_PATH) or "."

    if directory != "." and not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception:
            pass

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1")
        print(f"[DB] Using: {DB_PATH}")
        return conn, None
    except Exception as e:
        fallback = os.path.basename(DB_PATH) or "seen.db"
        print(f"[DB] {DB_PATH} unusable ({e}); falling back to {fallback}")
        return sqlite3.connect(fallback), (
            f"STORAGE PROBLEM: {DB_PATH} could not be opened ({e}), so history was "
            f"written to '{fallback}' instead. If this repeats, the same news may "
            f"be sent again."
        )


conn, storage_warning = open_db()
c = conn.cursor()
c.execute("CREATE TABLE IF NOT EXISTS articles (hash TEXT PRIMARY KEY)")
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


def already_seen(url):
    h = hashlib.md5(url.encode()).hexdigest()
    c.execute("SELECT 1 FROM articles WHERE hash=?", (h,))
    return c.fetchone() is not None


def mark_seen(url):
    """Record a URL as fully handled.

    Only called once an article has actually been judged. A scrape failure or an
    exhausted budget leaves it unmarked so the next run retries it.
    """
    h = hashlib.md5(url.encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO articles VALUES (?)", (h,))
    conn.commit()


def seen_count():
    c.execute("SELECT COUNT(*) FROM articles")
    return c.fetchone()[0]


# ---------------- MATCHING ----------------
def flatten(text):
    """Lowercase and reduce punctuation to single spaces.

    Product terms are written inconsistently - "pre-GI", "pre GI", "Hot-Dip" - so
    both the text and the keyword are flattened before comparison.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


FLAT_KEYWORDS = [flatten(k) for k in KEYWORDS]


def matches_topic(text):
    flat = flatten(text)
    return any(keyword in flat for keyword in FLAT_KEYWORDS)


def find_state(text):
    flat = flatten(text)
    for state, aliases in STATE_ALIASES.items():
        for name in aliases:
            if name in flat:
                return state
    return None


def llm_extract_state(title, content):
    """Ask which tracked state an article concerns, for articles that name a city
    or project but never the state itself."""
    if not FAST_MODEL or not budget.can_call_llm():
        return None

    budget.llm_calls += 1
    states = ", ".join(STATE_ALIASES.keys())

    try:
        prompt = f"""
Reply with EXACTLY ONE line and nothing else.

If the article is about solar energy, battery storage, or related equipment AND
concerns one of these places - including when the place is only implied by a
city, district, project or region within it - reply with that name:
{states}

Otherwise reply: NONE

Article:
{title}
{content[:2000]}
"""
        response = client.chat.completions.create(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.choices[0].message.content.strip().lower()

        for state, aliases in STATE_ALIASES.items():
            if any(name in answer for name in aliases):
                return state
        return None

    except Exception as e:
        print("[LLM ERROR]", e)
        return None


# ---------------- FINGERPRINT ----------------
UNIT_TO_MW = {"gw": 1000, "mw": 1, "kw": 0.001, "gwh": 1000, "mwh": 1, "kwh": 0.001}
MONEY_MULT = {"crore": 1e7, "cr": 1e7, "lakh": 1e5,
              "billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6}

NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(gwh|mwh|kwh|gw|mw|kw)\b")
MONEY_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d.]+)\s*(crore|cr|lakh|billion|bn|million|mn)\b")


def numeric_fingerprint(text):
    """Capacity and money figures normalized to common units.

    Numbers are the highest-signal feature in this corpus: "1200 MW" is specific
    and survives rewording, while "solar" is in nearly every headline. Only
    figures carrying units are captured, so a bare year cannot match everything.
    """
    text = re.sub(r"(\d),(\d)", r"\1\2", text.lower())
    out = set()

    for value, unit in NUM_UNIT_RE.findall(text):
        kind = "e" if unit.endswith("h") else "p"
        out.add(f"{kind}{round(float(value) * UNIT_TO_MW[unit])}")

    for value, unit in MONEY_RE.findall(text):
        out.add(f"m{round(float(value) * MONEY_MULT[unit])}")

    return out


def seen_recently(fingerprint):
    """True only on an exact fingerprint match inside the window.

    A story with no extractable figures is never suppressed - when unsure, send.
    """
    if not fingerprint:
        return False

    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    c.execute("SELECT fingerprint FROM stories WHERE first_seen >= ?", (cutoff,))
    key = "|".join(sorted(fingerprint))
    return any(row[0] == key for row in c.fetchall())


def record_story(item):
    key = "|".join(sorted(item["fingerprint"]))
    if not key:
        return
    c.execute("INSERT OR IGNORE INTO stories VALUES (?, ?, ?, ?, ?)",
              (key, item["title"], item["url"], item["state"],
               datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ---------------- CLUSTERING ----------------
def cluster_stories(items):
    """Group headlines reporting the same event. One LLM call for the whole run.

    Fails open: bad JSON, a missing or repeated index, or any API error yields one
    cluster per item. A duplicate line in the email is a nuisance; a story dropped
    because a parse failed is real damage.
    """
    singletons = [[i] for i in range(len(items))]

    if len(items) < 2 or not STRONG_MODEL or not budget.can_call_llm():
        return singletons

    budget.llm_calls += 1
    listing = "\n".join(f"{i}. {it['title']}" for i, it in enumerate(items))

    prompt = f"""
Group these energy-news headlines by the underlying EVENT.

Two headlines belong together only if they report the SAME specific event - the
same tender, project, commissioning or announcement. Same topic is NOT enough:
two different 500 MW solar tenders are different events.

Return ONLY JSON in this exact form:
{{"groups": [[0, 3], [1], [2, 4]]}}

Every index from 0 to {len(items) - 1} must appear exactly once.

{listing}
"""

    try:
        response = client.chat.completions.create(
            model=STRONG_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        groups = json.loads(response.choices[0].message.content.strip()).get("groups", [])

        if sorted(i for g in groups for i in g) != list(range(len(items))):
            print("[CLUSTER] Invalid partition -> failing open")
            return singletons

        print(f"[CLUSTER] {len(items)} items -> {len(groups)} stories")
        return groups

    except Exception as e:
        print("[CLUSTER ERROR]", e, "-> failing open")
        return singletons


# ---------------- FEEDS ----------------
def process_site(site, seeding):
    """Collect matching articles from one feed.

    In seeding mode every entry is marked seen and nothing is returned, so the
    first run does not email weeks of back content.
    """
    name = site["name"]
    matches = []

    try:
        r = requests.get(site["rss"], headers={"User-Agent": "Mozilla/5.0"},
                         timeout=FEED_TIMEOUT)
        if r.status_code >= 400:
            feed_errors.append(f"{name} (HTTP {r.status_code})")
            return matches
        feed = feedparser.parse(r.content)
    except Exception as e:
        feed_errors.append(f"{name} ({type(e).__name__})")
        return matches

    if not feed.entries:
        feed_errors.append(f"{name} (no entries)")
        return matches

    fresh = 0
    for entry in feed.entries[:MAX_ENTRIES_PER_SITE]:
        title = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        desc = entry.get("summary") or ""

        if not url or already_seen(url):
            continue

        fresh += 1

        if seeding:
            mark_seen(url)
            continue

        blurb = f"{title} {desc}"

        if not matches_topic(blurb):
            mark_seen(url)
            continue

        state = find_state(blurb)
        content = desc

        if not state:
            # Keyword hit but no state named. Worth the full text - this is where
            # "NTPC commissions 300 MW at Rihand" gets resolved to Uttar Pradesh.
            if not budget.can_scrape():
                budget.deferred += 1     # not marked seen: retried next run
                continue

            budget.scrapes += 1
            try:
                article = Article(url)
                article.download()
                article.parse()
                content = article.text or desc
            except Exception:
                budget.deferred += 1     # transient failure stays retryable
                continue

            state = find_state(content)

            if not state:
                if not budget.can_call_llm():
                    budget.deferred += 1
                    continue
                state = llm_extract_state(title, content)

            if not state:
                mark_seen(url)
                continue

        mark_seen(url)
        matches.append({
            "site": name,
            "title": title,
            "url": url,
            "state": state,
            "fingerprint": numeric_fingerprint(f"{title} {content[:1500]}"),
        })

    print(f"[FEED] {name}: {fresh} new, {len(matches)} matched")
    return matches


# ---------------- EMAIL ----------------
def send_email(subject, body):
    # DRY_RUN prints the exact email instead of sending it, so a run can be
    # rehearsed against live feeds without anything reaching an inbox.
    if DRY_RUN:
        print("\n" + "=" * 70)
        print(f"[DRY RUN] would send to {TO_EMAILS} bcc {BCC_EMAILS}")
        print(f"[DRY RUN] subject: {subject}")
        print("=" * 70)
        print(body)
        print("=" * 70 + "\n")
        return

    try:
        payload = {
            "sender": {"name": "Solar Alerts", "email": SENDER_EMAIL},
            "to": [{"email": e} for e in TO_EMAILS],
            "subject": subject,
            "textContent": body,
        }
        if BCC_EMAILS:
            payload["bcc"] = [{"email": e} for e in BCC_EMAILS]

        res = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={"accept": "application/json",
                     "api-key": BREVO_API_KEY,
                     "content-type": "application/json"},
            timeout=30,
        )
        print("[EMAIL]", res.status_code, res.text[:200])
        return res.status_code < 300

    except Exception as e:
        print("[EMAIL ERROR]", e)
        return False


def build_email(clusters, reviewed, seeding):
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y")
    lines = [f"SOLAR & BESS DAILY — {today}", ""]

    for warning in (storage_warning, model_warning):
        if warning:
            lines += ["!" * 60, warning, "!" * 60, ""]

    if seeding:
        lines += [
            "History was empty at the start of this run, so no news is listed.",
            "",
            f"{seen_count()} existing articles across {len(SITES)} feeds have been "
            "recorded as already reviewed. From tomorrow you will receive only "
            "genuinely new items.",
            "",
            "This happens on the very first run, and again after a deployment — "
            "a new deployment replaces the container filesystem, so the database "
            "starts empty. Cron runs between deployments keep their history. To "
            "carry history across deployments too, mount a Railway volume and set "
            "DB_PATH to a path inside it.",
            "",
        ]
    elif not clusters:
        lines += ["No new matching news today.", ""]
    else:
        by_state = {}
        for cluster in clusters:
            by_state.setdefault(cluster["state"], []).append(cluster)

        n = 0
        for state, entries in sorted(by_state.items()):
            lines.append(state.upper())
            for cluster in entries:
                n += 1
                lines.append(f" {n:>2}. {cluster['title']}")
                lines.append(f"     {cluster['url']}")

                sources = sorted({s for s in cluster["sources"]})
                if len(sources) > 1:
                    lines.append(f"     {sources[0]} · also: {', '.join(sources[1:])}")
                else:
                    lines.append(f"     {sources[0]}")
            lines.append("")

    # Health footer: makes a quiet failure visible instead of looking like a slow
    # news day.
    lines += ["-" * 60]
    lines.append(f"{reviewed} new article(s) reviewed · {len(clusters)} story(ies) after dedup")
    lines.append(f"Feeds: {len(SITES) - len(feed_errors)} ok, {len(feed_errors)} failed")

    if feed_errors:
        for err in feed_errors[:12]:
            lines.append(f"  ! {err}")
        if len(feed_errors) > 12:
            lines.append(f"  ! ...and {len(feed_errors) - 12} more")

    if budget.deferred:
        lines.append(f"Deferred to next run (budget/scrape limits): {budget.deferred}")

    lines.append(f"LLM calls: {budget.llm_calls}/{MAX_LLM_CALLS_PER_RUN} · "
                 f"scrapes: {budget.scrapes}/{MAX_SCRAPES_PER_RUN}")
    lines.append(f"Storage: {DB_PATH} · {seen_count()} article(s) on record")

    return "\n".join(lines)


# ---------------- MAIN ----------------
def main():
    # An empty table means either the very first run or a fresh deployment. Either
    # way the feeds are full of already-published back content, so record it as
    # reviewed and start clean rather than emailing weeks of history.
    seeding = seen_count() == 0 and not SKIP_SEEDING

    if SKIP_SEEDING and seen_count() == 0:
        print("[SYSTEM] SKIP_SEEDING set — processing news on an empty history")

    if seeding:
        print("[SYSTEM] History empty (first run or new deployment) — seeding")

    collected = []
    for site in SITES:
        collected.extend(process_site(site, seeding))

    reviewed = len(collected)

    # Cross-run: drop stories whose exact figures were already sent this week.
    fresh = []
    for item in collected:
        if seen_recently(item["fingerprint"]):
            print(f"[DEDUP] Already sent: {item['title'][:60]}")
            continue
        fresh.append(item)

    clusters = []
    for group in cluster_stories(fresh):
        members = [fresh[i] for i in group]
        primary = members[0]
        clusters.append({
            "title": primary["title"],
            "url": primary["url"],
            "state": primary["state"],
            "sources": [m["site"] for m in members],
            "fingerprint": primary["fingerprint"],
        })

    if len(clusters) > MAX_ITEMS_PER_EMAIL:
        print(f"[LIMIT] {len(clusters)} stories, capping at {MAX_ITEMS_PER_EMAIL}")
        clusters = clusters[:MAX_ITEMS_PER_EMAIL]

    body = build_email(clusters, reviewed, seeding)
    print("\n" + body + "\n")

    if send_email(f"Solar & BESS Daily — {len(clusters)} story(ies)", body):
        # Only record after a successful send, so a Brevo outage does not silently
        # consume the day's news.
        for cluster in clusters:
            record_story(cluster)
    else:
        print("[SYSTEM] Send failed — stories not recorded, will retry next run")

    print("[SYSTEM] Done")


if __name__ == "__main__":
    main()
