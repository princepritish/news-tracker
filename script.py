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
import html as html_lib
import json
import re
import sqlite3
import requests
import textwrap
import os
import sys
from datetime import datetime, timedelta, timezone
from newspaper import Article
from groq import Groq

import tenders

# Cloudflare bot management answers Python's TLS handshake with
# "cf-mitigated: challenge" and a 403, whatever User-Agent is sent - the
# fingerprint gives the client away before a header is read. curl_cffi replays
# Chrome's actual TLS fingerprint and recovers PV Tech, Energy Storage News and
# Solar Builder. Optional on purpose: without it those feeds simply stay 403,
# exactly as they were.
try:
    from curl_cffi import requests as impersonator
except Exception:
    impersonator = None

# Headlines carry rupee signs, en-dashes and Indic script. A Windows console
# defaults to cp1252, where printing any of them raises UnicodeEncodeError and
# kills the run on the final print - after every feed has been fetched and the
# digest built. Railway's console is UTF-8 already, so this only ever helps.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):      # not a real console; nothing to do
        pass

# ---------------- CONFIG ----------------
with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

SITES = config["sites"]
FILTERS = config["filters"]

KEYWORDS = [k.lower() for k in FILTERS.get("keywords", [])]

# The spreadsheet's own Monitoring Guide lists what to search portals for. These
# are matched against tender titles, which are far terser than article prose.
TENDER_KEYWORDS = [k.lower() for k in FILTERS.get("tender_keywords", [
    "solar", "spv", "rooftop solar", "pv", "solar pump", "epc",
    "photovoltaic", "renewable", "battery", "bess",
])]

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

# The subject line a client sees. Deliberately fixed and plain - a story count
# in the subject is an internal detail, and "0 story(ies)" is a bad thing to
# land in a client inbox on a quiet day.
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Today's Solar Alerts")

# Don't put an empty digest in a client's inbox. A run with no stories and no
# tenders writes its report and says so in the log, but sends nothing.
#
# The cost is the heartbeat: a silent day used to mean "the cron is fine, there
# was no news", and now it is indistinguishable from the cron being dead. The
# run log is the place to check that. Set SEND_WHEN_EMPTY=1 to get the old
# always-send behaviour back.
SEND_WHEN_EMPTY = os.getenv("SEND_WHEN_EMPTY", "0") == "1"

# Every run writes the digest to this file as Markdown. Set REPORT_PATH= (empty)
# to turn it off.
REPORT_PATH = os.getenv("REPORT_PATH", "report.md")

# The report goes to the client, so it carries no diagnostics: no feed or portal
# errors, no LLM or scrape counts, no database path, no explanation of why the
# history was empty. Those exist for the owner and live in the internal copy and
# in the run log, which is where the "silence means the cron broke" signal is
# actually read from.
# Off by default: the same diagnostics are printed to the run log every run,
# so writing a second file earns nothing. Set a path if you want one.
INTERNAL_REPORT_PATH = os.getenv("INTERNAL_REPORT_PATH", "")

# With email off the report file IS the delivery, so the run marks its news seen
# once the file is written. With email on nothing changes: news is marked only
# after Brevo accepts it, because otherwise a failed send silently burns a day.
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "1") == "1"

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
# Caps on what a single digest lists. 0 means no cap - send everything the run
# captured, which is the current intent while the output is being judged.
MAX_ITEMS_PER_EMAIL = int(os.getenv("MAX_ITEMS_PER_EMAIL", "0"))
DEDUP_WINDOW_DAYS = int(os.getenv("DEDUP_WINDOW_DAYS", "7"))
FEED_TIMEOUT = int(os.getenv("FEED_TIMEOUT", "20"))

# How much of a summary counts as the article declaring its subject. A keyword
# past this point is a passing mention, not what the piece is about.
LEDE_CHARS = int(os.getenv("LEDE_CHARS", "400"))

# How much of a scraped article may decide its state. A piece names where the
# work is in its dateline and opening paragraphs; what sits at the end is the
# author's biography. "Rooftop Solar after PM Surya Ghar" was filed under Andhra
# Pradesh because character 6648 of 7896 read "...student at Madanapalle
# Institute of Technology, Andhra Pradesh".
BODY_HEAD_CHARS = int(os.getenv("BODY_HEAD_CHARS", "1500"))

# Every story in the digest carries a summary. Two tiers, because a summary must
# never cost news: the free one is the article's own lede, taken from the feed
# description or the scraped body, and it is always filled in. The LLM pass then
# rewrites those into one clean sentence each, in batches, and anything it fails
# to return simply keeps its lede. Set SUMMARY_ENABLED=0 for lede-only.
SUMMARY_ENABLED = os.getenv("SUMMARY_ENABLED", "1") == "1"
SUMMARY_CHARS = int(os.getenv("SUMMARY_CHARS", "260"))
# Stories per summarising call. Batching is what keeps this affordable: one call
# per story would double the run's LLM usage and compete with state extraction,
# which is the call that decides whether an article is news at all.
SUMMARY_BATCH = int(os.getenv("SUMMARY_BATCH", "8"))
# A hard ceiling of its own, on top of the shared budget, so a big news day
# cannot spend the whole LLM allowance on polish.
MAX_SUMMARY_CALLS_PER_RUN = int(os.getenv("MAX_SUMMARY_CALLS_PER_RUN", "6"))

# Phase 2 - government tender portals. Always on: tenders are half of what this
# digest is for. Collection stays wrapped whole, so a portal failing still costs
# the tender section only and never the news.
MAX_PORTAL_FETCHES = int(os.getenv("MAX_PORTAL_FETCHES", "60"))
PORTAL_TIMEOUT = int(os.getenv("PORTAL_TIMEOUT", "25"))
MAX_TENDERS_PER_EMAIL = int(os.getenv("MAX_TENDERS_PER_EMAIL", "0"))
PORTAL_WORKERS = int(os.getenv("PORTAL_WORKERS", str(tenders.DEFAULT_WORKERS)))
# Which portal kinds to read. The GePNIC e-tender portals are captcha-gated, so
# the readable tenders come from the agency and DISCOM sites; set this to
# "tender_page,agency,discom" to stop spending requests on the walled ones.
PORTAL_KINDS = tuple(k.strip() for k in os.getenv(
    "PORTAL_KINDS", ",".join(tenders.DEFAULT_PORTAL_KINDS)).split(",") if k.strip())

# --- testing switches (all default off, safe to leave unset in production) ---
# DRY_RUN=1      print the email instead of sending it
# SKIP_SEEDING=1 process news even when history is empty, instead of seeding
# ONLY_SITES=n   use just the first n feeds, for a fast rehearsal
DRY_RUN = os.getenv("DRY_RUN") == "1"
SKIP_SEEDING = os.getenv("SKIP_SEEDING") == "1"
ONLY_SITES = int(os.getenv("ONLY_SITES", "0"))

if not GROQ_API_KEY:
    raise Exception("Missing GROQ_API_KEY")
if EMAIL_ENABLED and not BREVO_API_KEY:
    raise Exception("Missing BREVO_API_KEY (set EMAIL_ENABLED=0 for report-only)")
if EMAIL_ENABLED and not EMAIL_TO:
    raise Exception("EMAIL_TO not set (set EMAIL_ENABLED=0 for report-only)")

TO_EMAILS = [e.strip() for e in (EMAIL_TO or "").split(",") if e.strip()]
if EMAIL_ENABLED and not TO_EMAILS:
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
    SUMMARY_MODEL,
    "openai/gpt-oss-20b", "llama-3.1-8b-instant", "qwen/qwen3.6-27b",
    "gemma2-9b-it", "llama3-8b-8192", "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile", "mixtral-8x7b-32768",
]
STRONG_CANDIDATES = [
    CLUSTER_MODEL,
    "openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3.6-27b",
    "llama3-70b-8192", "openai/gpt-oss-20b", "llama-3.1-8b-instant",
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
    # "compound" models are agentic pipelines with built-in tool use, not plain
    # chat completions - slower and not dependable for strict JSON output, so they
    # are a last resort rather than a default pick.
    NON_CHAT = ("whisper", "tts", "guard", "embed", "moderation", "rerank",
                "orpheus")
    LAST_RESORT = ("compound",)

    def any_chat(prefer_large):
        usable = sorted(m for m in available
                        if not any(x in m.lower() for x in NON_CHAT))
        chat = [m for m in usable if not any(x in m.lower() for x in LAST_RESORT)]
        chat = chat or usable          # only fall back to agentic if nothing else
        if not chat:
            return None
        large = [m for m in chat
                 if any(s in m.lower() for s in ("70b", "120b", "-large", "32b", "27b"))]
        if prefer_large and large:
            return large[0]
        small = [m for m in chat
                 if any(s in m.lower() for s in ("8b", "9b", "20b", "instant", "mini"))]
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


def capped(items, limit):
    """items trimmed to `limit`, where 0 (or less) means no trimming at all."""
    return items if not limit or limit <= 0 else items[:limit]
feed_errors = []

# A bare "Mozilla/5.0" is 403'd by Cloudflare and similar front ends, which is
# what several of these publishers sit behind.
BROWSER_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("application/rss+xml, application/atom+xml, application/xml;q=0.9, "
               "text/xml;q=0.9, */*;q=0.8"),
    "Accept-Language": "en-IN,en;q=0.9",
}


def fetch_article_html(url):
    """The article page, fetched the same way the feeds are.

    newspaper3k downloads with its own plain client, which is why the four
    Cloudflare publishers whose *feeds* we recovered still 403 on their
    *article* pages. Measured: 28 of 29 scrape failures were HTTP 403, all from
    solarenergy.org, solarbuildermag.com, pv-tech.org and energy-storage.news -
    the same wall, hit one layer down. Reusing the browser headers, and the TLS
    impersonation on a 403, gets past it.
    """
    r = requests.get(url, headers=BROWSER_UA, timeout=FEED_TIMEOUT)
    if r.status_code == 403 and impersonator is not None:
        try:
            r2 = impersonator.get(url, impersonate="chrome", timeout=FEED_TIMEOUT)
            if r2.status_code < 400:
                r = r2
        except Exception:
            pass
    if r.status_code >= 400:
        raise RuntimeError("HTTP %s" % r.status_code)
    return r.text


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


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text):
    """Plain text out of an RSS summary.

    Feed summaries carry markup, and flatten() happily turns a tag into words:
    <a href="https://www.saurenergy.com/solar-energy-news/..."> flattens to
    "a href https www saurenergy com solar energy news", so the keyword "solar"
    matched every article from that publisher no matter what it was about.
    Measured live, this is what let pure wind and data-centre stories through
    the gate.
    """
    plain = html_lib.unescape(TAG_RE.sub(" ", text or ""))
    # collapse the runs a stripped tag leaves behind, and the non-breaking
    # spaces feeds are full of, so downstream text is clean for the LLM too
    return re.sub(r"\s+", " ", plain).strip()


def topical_text(title, desc):
    """The part of an article a topic decision may be made from.

    The headline and the opening of the summary are where a piece declares what
    it is about. Further down you find passing mentions - a tariff order that
    happens to say "solar", a bidder list, a "related articles" tail - and
    matching those is how a wind tender and a data-centre round-up ended up in a
    solar digest.
    """
    return "%s %s" % (title, (desc or "")[:LEDE_CHARS])


# Furniture that leads a feed description or a scraped body and says nothing
# about the story: a byline, a share prompt, a date stamp, a "read more" tail.
#
# Every alternative matches only itself. An earlier version ended each one with
# `[^.]*\.?` to "run to the end of the sentence", which on "Share this
# Advertisement A 250 MW plant opened." swallowed the entire summary - the
# greedy tail ate the story along with the furniture. Bounded patterns plus the
# never-empty guard in lede_summary are what keep that from happening again.
SUMMARY_NOISE_RE = re.compile(
    r"^\s*(?:"
    r"share (?:this|it|on(?: \w+){0,2})"
    r"|read more"
    r"|in short"                          # EQ Mag opens every description with it
    r"|advertisements?"
    r"|sponsored(?: content)?"
    r"|subscribe(?: now)?"
    r"|by [^\W\d_][\w.'’-]*(?: [^\W\d_][\w.'’-]*){0,3}(?=\s*[|–—-]\s)"
    r"|(?:published|updated|posted)(?: on)?\s*:?\s*[^|–—.]{0,40}(?=[|–—])"
    r")\s*[:|–—-]?\s*",
    re.I)

SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")

# Feeds mark their own truncation, and each publisher does it differently:
# Renewable Watch ends every description "[...]", others use "..." or a bare
# ellipsis, some append "Read more". They all mean the same thing, so they are
# normalised to one ellipsis rather than reaching the client four ways.
FEED_TRUNCATION_RE = re.compile(
    r"[\s.]*(?:\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…)\s*(?:read more|continue reading)?"
    r"[\s.]*$", re.I)


def lede_summary(text, limit=None):
    """The opening of an article, trimmed at a sentence boundary.

    This is the summary every story is guaranteed to have. It costs nothing -
    the text is already in hand, either as the feed's own description or as the
    body of an article the run scraped anyway to find its state - so a story
    still reads as a story when the LLM is rate-limited, keyless or switched off.
    """
    limit = SUMMARY_CHARS if limit is None else limit
    clean = strip_html(text or "")
    # A feed can lead with several pieces of furniture ("By A Correspondent |
    # Published: ... | Share this"), so strip until nothing more comes off.
    # Stripping never empties the text: a summary that reads as pure boilerplate
    # is still better than a blank line under a headline, and a pattern that
    # overreaches must not be able to delete a story's only description.
    while True:
        trimmed = SUMMARY_NOISE_RE.sub("", clean, count=1)
        if trimmed == clean or not trimmed.strip():
            break
        clean = trimmed
    # The publisher's own "there is more" marker, in whichever dialect, so the
    # length test below measures the story rather than the furniture.
    truncated_by_feed = bool(FEED_TRUNCATION_RE.search(clean))
    if truncated_by_feed:
        stripped = FEED_TRUNCATION_RE.sub("", clean).strip()
        clean = stripped or clean

    if not clean or limit <= 0:
        return clean[:max(limit, 0)] if limit > 0 else ""
    if len(clean) <= limit:
        return clean + ("…" if truncated_by_feed else "")

    window = clean[:limit + 1]
    # Prefer a whole sentence, but only if it is a real summary rather than a
    # three-word fragment; otherwise cut on a word and mark the truncation.
    ends = [m.start() for m in SENTENCE_END_RE.finditer(window)]
    if ends and ends[-1] >= limit // 3:
        return window[:ends[-1]].strip()
    cut = clean[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:-–") + "…"


def best_summary_source(title, desc, content):
    """The richest text available for summarising one article.

    A scraped body beats a feed description, but not every feed description is
    poorer than every body: some feeds carry the full first paragraph while a
    scrape that hit a paywall returns a line of navigation. So take whichever is
    longer, and fall back to the headline so the field is never empty.
    """
    candidates = [strip_html(t or "") for t in (content, desc)]
    best = max(candidates, key=len) if any(candidates) else ""
    return best or (title or "")


# Words that mark the next few tokens as a place rather than a name. "MB Power
# Madhya Pradesh Ltd" is a company; "project in Agar Malwa, Madhya Pradesh" is a
# location, and only the second says where the work is.
LOCATION_CUES = {
    "in", "at", "for", "from", "across", "near", "throughout", "within",
    "to", "of", "state", "states", "districts", "district", "region",
}
CUE_WINDOW = 3


def _has_location_cue(flat, start):
    """True if a location cue sits within CUE_WINDOW words before `start`."""
    before = flat[:start].split()
    return any(w in LOCATION_CUES for w in before[-CUE_WINDOW:])


def states_in(text, require_cue=False):
    """Every tracked state named in the text, in the order they appear.

    With require_cue, an occurrence only counts when a location cue precedes it,
    which is what separates a place from a company name.
    """
    flat = flatten(text)
    found = []
    for state, aliases in STATE_ALIASES.items():
        positions = []
        for alias in aliases:
            start = flat.find(alias)
            while start >= 0:
                if not require_cue or _has_location_cue(flat, start):
                    positions.append(start)
                    break
                start = flat.find(alias, start + 1)
        if positions:
            found.append((min(positions), state))
    return [state for _, state in sorted(found)]


def find_state(text, title=None):
    """The one tracked state an article is about, or None when that is unclear.

    Two rules, both learned from a live run:

    * The headline wins. It names the subject; the body names whatever it
      mentions in passing.
    * Naming several states means none of them. A round-up listing "Karnataka,
      Maharashtra, Gujarat, Rajasthan and Andhra Pradesh" is not Andhra Pradesh
      news, and neither is a national BESS story that lists five states it
      operates in. First-match-wins filed exactly those under whichever state
      the config happened to list first.

    None is not a drop. It falls through to the existing scrape-then-ask-the-LLM
    path, which is precisely the machinery for "a human would have to read it".
    """
    if title:
        in_title = states_in(title)
        if len(in_title) == 1:
            return in_title[0]
        if len(in_title) > 1:
            return None
    # Order matters: ambiguity is judged on every mention, because the cue filter
    # would hide the later members of a list ("across A, B and C") and make a
    # round-up look like single-state news. Only once one state is left does the
    # cue decide whether it is a place or part of a company name.
    found = states_in(text)
    if len(found) != 1:
        return None
    return found[0] if found[0] in states_in(text, require_cue=True) else None


# "The model said this is not one of our states" and "the call never happened"
# are different answers, and collapsing them loses news. Groq's free tier allows
# 8,000 tokens a minute on gpt-oss-20b - about eleven of these prompts - so a
# run of any size WILL be rate-limited, and every 429 used to mark an article
# seen and discard it forever.
LLM_FAILED = object()


def llm_extract_state(title, content):
    """Which tracked state an article concerns.

    Returns a state name, None when the model genuinely says none applies, or
    LLM_FAILED when the call could not be made - a rate limit, a timeout, a dead
    key. The caller must defer LLM_FAILED rather than treat it as a verdict.
    """
    if not FAST_MODEL or not budget.can_call_llm():
        return LLM_FAILED

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
        # Not a verdict - the article has not been judged at all.
        print("[LLM ERROR]", e)
        return LLM_FAILED


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


def story_key(fingerprint, state):
    """The identity used to suppress a repeat of the same story.

    Scoped by state as well as by figures. Round capacities repeat constantly -
    a live run turned up three unrelated 100 MW stories sharing the fingerprint
    {p100} - and figures alone would let the second one silently suppress the
    first. Two reports of one event share a state, so scoping costs real dedup
    nothing and removes a class of false suppression.
    """
    if not fingerprint:
        return ""
    return "%s|%s" % ((state or "?").lower(), "|".join(sorted(fingerprint)))


def seen_recently(fingerprint, state=None):
    """True only on an exact key match inside the window.

    A story with no extractable figures is never suppressed - when unsure, send.
    """
    key = story_key(fingerprint, state)
    if not key:
        return False

    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    c.execute("SELECT fingerprint FROM stories WHERE first_seen >= ?", (cutoff,))
    return any(row[0] == key for row in c.fetchall())


def record_story(item):
    key = story_key(item["fingerprint"], item.get("state"))
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


# ---------------- SUMMARIES ----------------
# How much of an article the summariser is shown. Enough for the lede and the
# figures in it; past that you are paying tokens for a bidder list.
SUMMARY_SOURCE_CHARS = 900


def summarize_clusters(clusters):
    """Give every story in the digest a one-sentence summary, in place.

    Fails open at the level of the individual story, not the batch: each cluster
    already carries its lede summary before this runs, so a rate limit, bad JSON,
    a dropped index or a dead key leaves that lede standing and the digest is
    still complete. Nothing here can add, drop or reorder a story.

    Runs after clustering deliberately - only the stories that actually reach the
    digest are summarised, which is the smallest number of calls that can do the
    job, and state extraction has already taken the budget it needs by then.
    """
    if not clusters:
        return clusters

    pending = [c for c in clusters if not c.get("summary_source_is_llm")]
    if not SUMMARY_ENABLED or not STRONG_MODEL:
        # The run log is the only place this is visible, so say which of the two
        # it was: one is a setting, the other is a broken key or a retired model.
        why = "SUMMARY_ENABLED=0" if not SUMMARY_ENABLED else "no usable model"
        print("[SUMMARY] %d story(ies) keep their lede (%s)" % (len(pending), why))
        return clusters

    calls = improved = 0
    for start in range(0, len(pending), max(SUMMARY_BATCH, 1)):
        batch = pending[start:start + max(SUMMARY_BATCH, 1)]
        if calls >= MAX_SUMMARY_CALLS_PER_RUN or not budget.can_call_llm():
            print("[SUMMARY] budget reached — %d story(ies) keep their lede"
                  % len(pending[start:]))
            break

        budget.llm_calls += 1
        calls += 1
        listing = "\n\n".join(
            "%d. %s\n%s" % (i, item["title"],
                            (item.get("summary") or "")[:SUMMARY_SOURCE_CHARS])
            for i, item in enumerate(batch))

        prompt = f"""
Summarise each news item below in ONE sentence of at most 35 words.

Write for a solar and battery-storage developer in India: keep capacities,
tariffs, money, company and state names, and say what actually happened. Do not
add anything that is not in the text, and do not editorialise.

Return ONLY JSON in this exact form, with a key for every index 0 to {len(batch) - 1}:
{{"summaries": {{"0": "...", "1": "..."}}}}

{listing}
"""
        try:
            response = client.chat.completions.create(
                model=STRONG_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            answers = json.loads(
                response.choices[0].message.content.strip()).get("summaries", {})
            for i, item in enumerate(batch):
                text = answers.get(str(i)) or answers.get(i)
                if isinstance(text, str) and text.strip():
                    item["summary"] = strip_html(text.strip())
                    item["summary_source_is_llm"] = True
                    improved += 1
        except Exception as e:
            # The lede is already in place, so this costs polish and nothing else.
            print("[SUMMARY ERROR]", e, "-> keeping the lede for this batch")

    print("[SUMMARY] %d/%d story(ies) summarised by the model in %d call(s)"
          % (improved, len(clusters), calls))
    return clusters


# ---------------- FEEDS ----------------
def process_site(site, seeding):
    """Collect matching articles from one feed.

    In seeding mode every entry is marked seen and nothing is returned, so the
    first run does not email weeks of back content.
    """
    name = site["name"]
    matches = []

    try:
        r = requests.get(site["rss"], headers=BROWSER_UA,
                         timeout=FEED_TIMEOUT)
        if r.status_code == 403 and impersonator is not None:
            # A bot challenge, not a dead feed. Retry once as a real browser.
            try:
                r = impersonator.get(site["rss"], impersonate="chrome",
                                     timeout=FEED_TIMEOUT)
                if r.status_code < 400:
                    print(f"[FEED] {name}: 403 cleared by TLS impersonation")
            except Exception:
                pass                      # keep the original 403 below
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
        desc = strip_html(entry.get("summary") or "")

        if not url or already_seen(url):
            continue

        fresh += 1

        if seeding:
            mark_seen(url)
            continue

        blurb = f"{title} {desc}"

        if not matches_topic(topical_text(title, desc)):
            mark_seen(url)
            continue

        state = find_state(blurb, title)
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
                article.set_html(fetch_article_html(url))
                article.parse()
                content = article.text or desc
            except Exception:
                budget.deferred += 1     # transient failure stays retryable
                continue

            state = find_state(content[:BODY_HEAD_CHARS], title)

            if not state:
                if not budget.can_call_llm():
                    budget.deferred += 1
                    continue
                state = llm_extract_state(title, content)

            if state is LLM_FAILED:
                # Rate limited, timed out, or no model. Leave it unmarked so the
                # next run judges it, exactly as a failed scrape is treated.
                budget.deferred += 1
                continue

            if not state:
                mark_seen(url)
                continue

        # NOT marked seen here: if the send fails, an article already recorded as
        # reviewed would never be looked at again and the news would be lost. The
        # caller marks these only after Brevo accepts the email.
        matches.append({
            "site": name,
            "title": title,
            "url": url,
            "state": state,
            "summary": lede_summary(best_summary_source(title, desc, content)),
            "fingerprint": numeric_fingerprint(f"{title} {content[:1500]}"),
        })

    # On a seeding run nothing is assessed - every entry is marked seen before
    # the keyword gate - so reporting "0 matched" reads as though the filter
    # rejected 475 articles when it never looked at one.
    if seeding:
        print(f"[FEED] {name}: {fresh} seeded (not assessed)")
    else:
        print(f"[FEED] {name}: {fresh} new, {len(matches)} matched")
    return matches


# ---------------- EMAIL ----------------
# ---------------- TENDER HISTORY (Phase 2) ----------------
def ensure_tender_table():
    c.execute("""CREATE TABLE IF NOT EXISTS tenders_seen (
        key TEXT PRIMARY KEY, title TEXT, url TEXT, state TEXT, first_seen TEXT)""")
    conn.commit()


def tender_is_new(key):
    c.execute("SELECT 1 FROM tenders_seen WHERE key=?", (key,))
    return c.fetchone() is None


def record_tender(item):
    c.execute("INSERT OR IGNORE INTO tenders_seen VALUES (?,?,?,?,?)",
              (item["key"], item["title"][:300], item["url"], item["state"],
               datetime.now(timezone.utc).isoformat()))
    conn.commit()


def gather_tenders():
    """Phase 2. Returns (new_items, errors) and never raises.

    Wrapped whole: a failure anywhere in tender collection must not cost you the
    news digest, which is the part that already works.
    """
    try:
        ensure_tender_table()
        sources = tenders.load_sources()
        if not sources:
            return [], ["sources.json empty or unreadable"]

        found, errors = tenders.collect(
            sources, TENDER_KEYWORDS, MAX_PORTAL_FETCHES, PORTAL_TIMEOUT,
            portal_kinds=PORTAL_KINDS, workers=PORTAL_WORKERS)

        unique, seen_keys = [], set()
        for item in found:
            item["key"] = tenders.tender_key(item)
            # Two portals in one state can list the same tender.
            if item["key"] in seen_keys or not tender_is_new(item["key"]):
                continue
            seen_keys.add(item["key"])
            unique.append(item)

        print(f"[TENDERS] {len(found)} matched, {len(unique)} new, "
              f"{len(errors)} portal error(s)")
        return unique, errors

    except Exception as e:
        print(f"[TENDERS ERROR] {type(e).__name__}: {e}")
        return [], [f"tender collection failed ({type(e).__name__})"]


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



def _group_errors(errors):
    """Collapse "State Portal (reason)" lines into one line per reason.

    Eighteen consecutive "captcha-gated" lines bury the one genuine failure
    underneath them, which is the opposite of what a health section is for.
    """
    from collections import OrderedDict
    grouped = OrderedDict()
    for err in errors:
        match = re.search(r"\(([^)]*)\)\s*$", err)
        reason = match.group(1) if match else "other"
        grouped.setdefault(reason, []).append(re.sub(r"\s*\([^)]*\)\s*$", "", err))
    return grouped


def build_report(clusters, reviewed, seeding, tender_items=None,
                 tender_errors=None, diagnostics=True):
    """The digest as a standalone Markdown document.

    Same data as the email, laid out to be read as a file: links are real links,
    the run's health is a section rather than a footer, and repeated portal
    failures are grouped instead of listed one per line.
    """
    tender_items = tender_items or []
    tender_errors = tender_errors or []
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))

    out = ["# Solar & BESS Daily", ""]
    out.append("*%s · %d story(ies) from %d feed(s)*"
               % (now.strftime("%d %b %Y, %H:%M IST"), len(clusters), len(SITES)))
    out.append("")

    if diagnostics:
        for warning in (storage_warning, model_warning):
            if warning:
                out += ["> **%s**" % warning, ""]

    if seeding and diagnostics:
        out += [
            "## Seeding run — no news listed", "",
            "History was empty when this run started, so everything the feeds are "
            "currently carrying has been recorded as already reviewed. "
            "From the next run you get only genuinely new items.", "",
            "%d article(s) across %d feed(s) were recorded. This happens on the "
            "first run, and again after a deployment replaces the container "
            "filesystem." % (seen_count(), len(SITES)), "",
        ]
    elif seeding:
        # Nothing about databases or deployments: from the client's side this is
        # simply a day with nothing to report.
        out += ["## News", "", "No new items today.", ""]
    elif not clusters:
        out += ["## News", "", "No new matching news today.", ""]
    else:
        out += ["## News", ""]
        by_state = {}
        for cluster in clusters:
            by_state.setdefault(cluster["state"], []).append(cluster)

        n = 0
        for state, entries in sorted(by_state.items()):
            out += ["### %s" % state.title(), ""]
            for cluster in entries:
                n += 1
                out.append("%d. **[%s](%s)**" % (n, cluster["title"], cluster["url"]))
                if cluster.get("summary"):
                    out.append("")
                    out.append("   %s" % cluster["summary"])
                out.append("")
                sources = sorted({s for s in cluster["sources"]})
                if len(sources) > 1:
                    out.append("   *%s — also covered by %s*"
                               % (sources[0], ", ".join(sources[1:])))
                else:
                    out.append("   *%s*" % sources[0])
                out.append("")

    if not seeding:
        out += ["## Tenders & government notices", ""]
        if not tender_items:
            out += ["No new tenders matched today.", ""]
        else:
            by_state = {}
            for item in capped(tender_items, MAX_TENDERS_PER_EMAIL):
                by_state.setdefault(item["state"], []).append(item)
            t = 0
            for state, entries in sorted(by_state.items()):
                out += ["### %s" % state.title(), ""]
                for item in entries:
                    t += 1
                    out.append("%d. **[%s](%s)**" % (t, item["title"], item["url"]))
                    out.append("   %s — %s" % (item["portal"], item["source"]))
                    out.append("")
            if MAX_TENDERS_PER_EMAIL and len(tender_items) > MAX_TENDERS_PER_EMAIL:
                out += ["*...and %d more not shown.*"
                        % (len(tender_items) - MAX_TENDERS_PER_EMAIL), ""]

    if not diagnostics:
        return "\n".join(out) + "\n"

    # ---- health ----
    # A quiet failure has to look different from a slow news day, so this stays
    # in the internal report even when everything worked.
    out += ["## Run health", ""]
    out += ["| | |", "|---|---|"]
    out.append("| Articles reviewed | %d |" % reviewed)
    out.append("| Stories after dedup | %d |" % len(clusters))
    out.append("| Feeds | %d ok, %d failed |"
               % (len(SITES) - len(feed_errors), len(feed_errors)))
    out.append("| LLM calls | %d / %d |" % (budget.llm_calls, MAX_LLM_CALLS_PER_RUN))
    out.append("| Scrapes | %d / %d |" % (budget.scrapes, MAX_SCRAPES_PER_RUN))
    if budget.deferred:
        out.append("| Deferred to next run | %d |" % budget.deferred)
    out.append("| Tenders | %d new, %d portal error(s) |"
               % (len(tender_items), len(tender_errors)))
    out.append("| Storage | `%s`, %d article(s) on record |" % (DB_PATH, seen_count()))
    out.append("")

    if feed_errors:
        out += ["### Feeds that failed", ""]
        for err in feed_errors:
            out.append("- %s" % err)
        out.append("")

    if tender_errors:
        out += ["### Portals that returned nothing", ""]
        for reason, names in _group_errors(tender_errors).items():
            out.append("- **%s** (%d): %s" % (reason, len(names), ", ".join(names)))
        out.append("")

    return "\n".join(out) + "\n"


def write_report(body, path=None):
    """Write a Markdown report. Returns True only if the file is really there."""
    path = REPORT_PATH if path is None else path
    if not path:
        return False
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print("[REPORT] %s (%d bytes)" % (path, len(body.encode("utf-8"))))
        return True
    except Exception as e:
        print("[REPORT ERROR] %s: %s" % (type(e).__name__, e))
        return False

# The email is plain text, so nothing wraps it for us. A summary on one long
# line renders as a single unreadable run in most mail clients.
EMAIL_WRAP = int(os.getenv("EMAIL_WRAP", "76"))


def _wrap_summary(text, indent="     ", width=None):
    """A summary as indented plain-text lines, wrapped on word boundaries."""
    width = EMAIL_WRAP if width is None else width
    return textwrap.wrap(text, width=max(width, 20), initial_indent=indent,
                         subsequent_indent=indent) or []


def build_email(clusters, reviewed, seeding, tender_items=None,
                tender_errors=None, diagnostics=True):
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y")
    lines = [f"SOLAR & BESS DAILY — {today}", ""]

    if diagnostics:
        for warning in (storage_warning, model_warning):
            if warning:
                lines += ["!" * 60, warning, "!" * 60, ""]

    if seeding and not diagnostics:
        lines += ["No new items today.", ""]
    elif seeding:
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
                if cluster.get("summary"):
                    lines += _wrap_summary(cluster["summary"])
                lines.append(f"     {cluster['url']}")

                sources = sorted({s for s in cluster["sources"]})
                if len(sources) > 1:
                    lines.append(f"     {sources[0]} · also: {', '.join(sources[1:])}")
                else:
                    lines.append(f"     {sources[0]}")
                lines.append("")
            lines.append("")

    # ---- TENDERS section ----
    tender_items = tender_items or []
    tender_errors = tender_errors or []

    if not seeding:
        lines += ["=" * 60, "TENDERS & GOVT NOTICES", "=" * 60, ""]

        if not tender_items:
            lines += ["No new tenders matched today.", ""]
        else:
            by_state = {}
            for item in capped(tender_items, MAX_TENDERS_PER_EMAIL):
                by_state.setdefault(item["state"], []).append(item)

            t = 0
            for state, entries in sorted(by_state.items()):
                lines.append(state.upper())
                for item in entries:
                    t += 1
                    lines.append(f" {t:>2}. {item['title']}")
                    lines.append(f"     {item['url']}")
                    lines.append(f"     {item['portal']} · {item['source']}")
                lines.append("")

            if MAX_TENDERS_PER_EMAIL and len(tender_items) > MAX_TENDERS_PER_EMAIL:
                lines += [f"...and {len(tender_items) - MAX_TENDERS_PER_EMAIL} "
                          f"more not shown", ""]

    if not diagnostics:
        return "\n".join(lines)

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
    lines.append(f"Tenders: {len(tender_items)} new, {len(tender_errors)} portal error(s)")
    for err in tender_errors[:10]:
        lines.append(f"  ! {err}")
    if len(tender_errors) > 10:
        lines.append(f"  ! ...and {len(tender_errors) - 10} more")

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
        if seen_recently(item["fingerprint"], item.get("state")):
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
            # The fullest lede any outlet in the cluster carried. One outlet
            # running a two-line stub is no reason to summarise the story from
            # it when another ran four paragraphs.
            "summary": max((m.get("summary") or "" for m in members), key=len),
            "fingerprint": primary["fingerprint"],
        })

    if MAX_ITEMS_PER_EMAIL and len(clusters) > MAX_ITEMS_PER_EMAIL:
        print(f"[LIMIT] {len(clusters)} stories, capping at {MAX_ITEMS_PER_EMAIL}")
        clusters = clusters[:MAX_ITEMS_PER_EMAIL]

    # After the cap, so nothing is summarised that will not be sent.
    summarize_clusters(clusters)

    tender_items, tender_errors = ([], [])
    if not seeding:
        tender_items, tender_errors = gather_tenders()

    # What the client receives: no diagnostics anywhere in it.
    body = build_email(clusters, reviewed, seeding, tender_items, tender_errors,
                       diagnostics=False)

    # What the owner reads. Printed rather than sent, so the run log keeps the
    # "silence means the cron broke" signal even though the client never sees it.
    print("\n" + build_email(clusters, reviewed, seeding, tender_items,
                             tender_errors, diagnostics=True) + "\n")

    report_written = write_report(
        build_report(clusters, reviewed, seeding, tender_items, tender_errors,
                     diagnostics=False))
    write_report(
        build_report(clusters, reviewed, seeding, tender_items, tender_errors,
                     diagnostics=True), INTERNAL_REPORT_PATH)

    has_content = bool(clusters) or bool(tender_items)

    if not has_content and not SEND_WHEN_EMPTY:
        # Nothing to deliver, so nothing failed to deliver: the run completed and
        # anything it reviewed is recorded, or a repeat-heavy day would be
        # re-reviewed from scratch tomorrow.
        print("[SYSTEM] Nothing to report — no email sent")
        delivered = True
    elif EMAIL_ENABLED:
        delivered = send_email(EMAIL_SUBJECT, body)
    else:
        # Report-only: the file on disk is the delivery, so it decides.
        delivered = report_written
        print("[SYSTEM] EMAIL_ENABLED=0 — report is the delivery "
              + ("written" if report_written else "FAILED"))

    if delivered:
        # Recorded only once the digest has actually gone somewhere. A failed
        # delivery must leave the day's news untouched for the next run.
        for item in collected:
            mark_seen(item["url"])
        for cluster in clusters:
            record_story(cluster)
        for item in capped(tender_items, MAX_TENDERS_PER_EMAIL):
            record_tender(item)
    elif DRY_RUN:
        # A rehearsal deliberately consumes nothing, so the same articles are
        # available for the real run. That is not a failure.
        print(f"[SYSTEM] Dry run — nothing sent, {len(collected)} article(s) "
              f"left for the next real run")
    else:
        print(f"[SYSTEM] Delivery failed — {len(collected)} article(s) left "
              f"unmarked, will be retried next run")

    print("[SYSTEM] Done")


if __name__ == "__main__":
    main()
