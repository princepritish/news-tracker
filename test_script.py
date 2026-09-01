"""Daily-digest tests: seeding, dedup, budgets, storage failure, always-send."""
import json, os, sys, types, tempfile, importlib.util

CLUSTERS = {"groups": [[0, 1], [2]]}
STATE_ANS = "NONE"
FAIL_CLUSTER = False
FAIL_SUMMARY = False
# What the stubbed model returns for a summarising call. A dict keyed by the
# index within the batch, exactly as the real prompt asks for.
SUMMARIES = {str(i): "Model sentence %d." % i for i in range(12)}

groq = types.ModuleType("groq")
MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
class _ML:
    @property
    def data(s): return [types.SimpleNamespace(id=m) for m in MODELS]
class _G:
    def __init__(s, **k):
        s.chat = types.SimpleNamespace(completions=s)
        s.models = types.SimpleNamespace(list=lambda: _ML())
    def create(s, model, messages, **kw):
        t = messages[0]["content"]
        if "Group these energy-news headlines" in t:
            if FAIL_CLUSTER: raise RuntimeError("groq 429 rate limit")
            out = json.dumps(CLUSTERS)
        elif "Summarise each news item" in t:
            if FAIL_SUMMARY: raise RuntimeError("groq 429 rate limit")
            out = json.dumps({"summaries": SUMMARIES})
        else:
            out = STATE_ANS
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=out))])
groq.Groq = _G
sys.modules["groq"] = groq

np = types.ModuleType("newspaper")
class Article:
    def __init__(s, u): s.url, s.text, s.html = u, "", ""
    def download(s): pass
    # the pipeline fetches the page itself now and hands the html over, so the
    # stub has to accept it or every scrape silently "fails" and defers
    def set_html(s, html): s.html = html
    def parse(s): s.text = "body text"
np.Article = Article
sys.modules["newspaper"] = np

os.chdir(os.path.dirname(os.path.abspath(__file__)))
DB = tempfile.mktemp(suffix=".db")
INTERNAL = tempfile.mktemp(suffix=".md")
os.environ.update(GROQ_API_KEY="x", BREVO_API_KEY="x",
                  EMAIL_TO="a@b.com,c@d.com", DB_PATH=DB,
                  REPORT_PATH="", INTERNAL_REPORT_PATH=INTERNAL)


def internal():
    """The owner's copy of the last run.

    The email now goes to a client, so every diagnostic - feed errors,
    budgets, storage warnings, why the history was empty - lives here and
    in the run log instead. The "a quiet failure must stay visible"
    invariant is unchanged; only the audience for it is.
    """
    with open(INTERNAL, encoding="utf-8") as fh:
        return fh.read()

SENT = []
import requests as rq, feedparser
class R:
    status_code, text, content = 200, "ok", b"<rss/>"
def post(u, **k): SENT.append(k.get("json")); return R()
def get(u, **k): return R()
rq.post, rq.get = post, get

ENTRIES = [
    {"title": "SECI Issues Tender For 1,200 MW Solar In Bihar", "link": "http://a/1",
     "summary": "solar bids invited in Bihar"},
    {"title": "SECI floats 1.2 GW Bihar solar tender", "link": "http://b/2",
     "summary": "solar tender Bihar announced"},
    {"title": "NTPC commissions 300 MW solar in Sikkim", "link": "http://c/3",
     "summary": "solar project Sikkim"},
    {"title": "India wins cricket match", "link": "http://d/4", "summary": "sports"},
]
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=ENTRIES)

def load():
    spec = importlib.util.spec_from_file_location("s", "script.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.SITES = [{"name": "TestFeed", "rss": "http://x/feed"}]
    return m

ok = True
def check(label, got, want):
    global ok
    good = got == want; ok = ok and good
    print(f"{'PASS' if good else 'FAIL'}  {label}: {got!r}" + ("" if good else f" (want {want!r})"))

print("=== run 1: seeding ===")
s = load(); SENT.clear(); s.main()
# Nothing was assessed, so there is nothing to send a client.
check("seeding sends no email", len(SENT), 0)
check("no news listed", "SECI" in internal(), False)
check("explains seeding to the owner", "already reviewed" in internal(), True)

def fresh_batch(tag):
    """New URLs so they are genuinely unseen after the seeding run."""
    return [dict(e, link=e["link"] + tag) for e in ENTRIES]

print("\n=== run 2: real news, dedup ===")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("b"))
s = load(); SENT.clear(); s.main()
body = SENT[0]["textContent"]
check("one email", len(SENT), 1)
check("3 matched -> 2 stories", "| Stories after dedup | 2 |" in internal(), True)
check("cricket filtered out", "cricket" in body.lower(), False)
check("BIHAR header", "BIHAR" in body, True)
check("SIKKIM header (new state)", "SIKKIM" in body, True)
# multi-outlet formatting needs distinct source names, which one mock feed
# cannot produce - exercise the builder directly
_b = s.build_email([{ "title": "SECI tenders 1200 MW", "url": "http://a",
    "state": "bihar", "sources": ["EQ Mag", "SolarQuarter", "Saur"],
    "fingerprint": set()}], 3, False)
check("merged source note", "EQ Mag \u00b7 also: Saur, SolarQuarter" in _b, True)
check("single source has no 'also'", "also:" in s.build_email([{ "title": "x",
    "url": "u", "state": "bihar", "sources": ["EQ Mag"], "fingerprint": set()}],
    1, False), False)

print("\n=== run 3: nothing new, stays quiet ===")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("b"))
s = load(); SENT.clear(); s.main()
check("no email on a day with nothing new", len(SENT), 0)
check("the owner's copy still records the run",
      "No new matching news today" in internal(), True)

print("\n=== clustering failure (rate limit) fails open ===")
FAIL_CLUSTER = True
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=ENTRIES)
s = load(); SENT.clear(); s.main()          # seeding
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("c"))
s = load(); SENT.clear(); s.main()          # real
body = SENT[0]["textContent"]
check("still sends on LLM failure", len(SENT), 1)
check("no story lost (3 stories)", "| Stories after dedup | 3 |" in internal(), True)
FAIL_CLUSTER = False

print("\n=== storage failure surfaces in email ===")
# A plain file standing where a directory should be. Both makedirs and
# sqlite3.connect fail on it on every platform, unlike /proc/... which only
# exists on Linux - on Windows that path is created without complaint and the
# storage failure this case exists to test never happens.
_blocker = tempfile.mktemp(suffix=".notadir")
open(_blocker, "w").close()
os.environ["DB_PATH"] = os.path.join(_blocker, "seen.db")
s = load(); SENT.clear(); s.main()
check("storage warning reaches the owner", "STORAGE PROBLEM" in internal(), True)
check("names the fix", "history was written to" in internal().lower(), True)
# open_db falls back to the basename in the cwd, which is the repo directory
s.conn.close()
for _f in (_blocker, "seen.db"):
    try: os.remove(_f)
    except OSError: pass

print("\n=== empty db seeds and explains why ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("d"))
s = load(); SENT.clear(); s.main()
check("seeds on empty db", "recorded as already reviewed" in internal(), True)
check("explains deployment cause", "deployment" in internal(), True)
check("a seeding run reaches no client", len(SENT), 0)
check("no news listed", "BIHAR" in internal(), False)

print("\n=== feed failure counted ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
def bad_get(u, **k): raise ConnectionError("dns")
rq.get = bad_get
s = load(); SENT.clear(); s.main()
check("feed error reported", "1 failed" in internal(), True)
check("a run with every feed down has nothing to send", len(SENT), 0)
rq.get = get

print("\n=== no usable Groq model ===")
import builtins
_saved = MODELS[:]
MODELS.clear(); MODELS.extend(["whisper-large-v3", "llama-guard-4"])  # no chat model
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("e"))
s = load(); SENT.clear(); s.main()                       # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("e2"))
s = load(); SENT.clear(); s.main()
check("model warning reaches the owner", "LLM UNAVAILABLE" in internal(), True)
check("news still sends with no usable model", len(SENT), 1)
check("no llm calls attempted", s.budget.llm_calls, 0)
MODELS.clear(); MODELS.extend(_saved)

print("\n=== LLM budget cap ===")
os.environ.update(DB_PATH=tempfile.mktemp(suffix=".db"), MAX_LLM_CALLS_PER_RUN="0")
s = load(); SENT.clear(); s.main()                       # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("g"))
SENT.clear(); s2 = load(); s2.main()
check("llm calls stay at 0", s2.budget.llm_calls, 0)
check("news still sends with the budget at zero", len(SENT), 1)

import requests
print("\n=== send failure must not consume the news ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["SKIP_SEEDING"] = "1"
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("f"))

_ok_post = requests.post
requests.post = lambda url, **k: types.SimpleNamespace(
    status_code=401, text='{"message":"unrecognised IP"}')
s_ = load(); s_.main()
# Off-topic articles are marked immediately - they have been judged and are
# irrelevant regardless of whether the email goes out. Only *matched* articles
# wait for a successful send, so the batch's one cricket item is expected here.
check("matched articles not marked after failed send", s_.seen_count(), 1)

requests.post = _ok_post
s_ = load(); SENT.clear(); s_.main()
check("news reappears on next run", "BIHAR" in SENT[0]["textContent"], True)
check("marked seen after successful send", s_.seen_count() > 0, True)
os.environ.pop("SKIP_SEEDING", None)

print("\n=== tenders section (Phase 2) ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")

TENDER_HTML = ('<html><body>'
  '<a href="/d?id=1">Supply and Installation of 500 kWp Rooftop Solar Plant</a>'
  '<a href="/d?id=2">Construction of boundary wall at block office</a>'
  '</body></html>')

_orig_get = requests.get
def _get(url, **kw):
    if "gov.in" in url or "gov.bt" in url:
        return types.SimpleNamespace(status_code=200, text=TENDER_HTML, content=b"")
    return _orig_get(url, **kw)
requests.get = _get

feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("t"))
s_ = load(); SENT.clear(); s_.main()          # run 1 seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("t2"))
s_ = load(); SENT.clear(); s_.main()
_b = SENT[0]["textContent"]
check("tenders section present", "TENDERS & GOVT NOTICES" in _b, True)
check("solar tender listed", "Rooftop Solar Plant" in _b, True)
check("non-solar tender excluded", "boundary wall" in _b, False)
check("tender count in footer", "| Tenders |" in internal(), True)

# same tenders next run -> already recorded, so not repeated
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("t3"))
s_ = load(); SENT.clear(); s_.main()
# The report is written whether or not anything is sent, so assert on it: this
# run has no new tenders and its figures were already sent, so nothing goes out.
check("tender not repeated next run", "Rooftop Solar Plant" in internal(), False)
check("and with nothing new there is no email", len(SENT), 0)

# a portal blowing up must not cost us the news digest
def _boom(url, **kw):
    if "gov.in" in url or "gov.bt" in url:
        raise ConnectionError("portal down")
    return _orig_get(url, **kw)
requests.get = _boom
# Fresh db + no seeding: earlier batches in this file reuse the same figures, so
# a shared db would (correctly) suppress them and hide what we're testing.
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["SKIP_SEEDING"] = "1"
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("t4"))
s_ = load(); SENT.clear(); s_.main()
_b = SENT[0]["textContent"]
check("news survives total tender failure", "BIHAR" in _b, True)
check("portal errors reported", "portal error" in internal(), True)

requests.get = _orig_get
os.environ.pop("SKIP_SEEDING", None)

print("\n=== suppression key ===")
# Round capacities repeat constantly: a live run turned up three unrelated
# 100 MW stories whose figures alone were identical. Keyed on figures only, the
# second one would silently suppress the first - the exact failure this project
# cares most about. The state scopes the key so that cannot happen.
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
s = load()
check("same figures, different states -> different keys",
      s.story_key({"p100"}, "bihar") != s.story_key({"p100"}, "odisha"), True)
check("same figures, same state -> same key",
      s.story_key({"p100"}, "bihar"), s.story_key({"p100"}, "bihar"))
check("figure order does not matter",
      s.story_key({"p100", "e200"}, "bihar"), s.story_key({"e200", "p100"}, "bihar"))
check("no figures -> no key, never suppressed", s.story_key(set(), "bihar"), "")
check("no figures is never 'seen recently'", s.seen_recently(set(), "bihar"), False)

s.record_story({"fingerprint": {"p100"}, "state": "bihar",
                "title": "Bihar 100 MW solar", "url": "http://a"})
check("a repeat in the same state is suppressed",
      s.seen_recently({"p100"}, "bihar"), True)
check("the same figure elsewhere still gets through",
      s.seen_recently({"p100"}, "odisha"), False)
check("a different figure in the same state gets through",
      s.seen_recently({"p250"}, "bihar"), False)

print("\n=== markup must not be matched as text ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
s = load()

# Live bug: every Saur article carried <a href=".../solar-energy-news/...">, and
# flatten() turns that into the word "solar" - so wind and data-centre stories
# sailed through a gate that exists to keep them out.
_wind = ("IWTMA Flags State-Level Bottlenecks to Wind Growth Target "
         '<a href="https://www.saurenergy.com/solar-energy-news/ayana">more</a>')
check("raw markup would have matched", s.matches_topic(_wind), True)
check("stripped markup does not", s.matches_topic(s.strip_html(_wind)), False)
check("strip_html drops tags",
      s.strip_html("<p>Wind news.</p><a href='x/solar-energy-news/y'>more</a>").strip(),
      "Wind news. more")
check("strip_html decodes entities",
      s.strip_html("Tata&nbsp;Power &amp; BESS").strip(), "Tata Power & BESS")
# a genuine mention in prose must still count
check("real prose still matches",
      s.matches_topic(s.strip_html("<p>NTPC commissions a solar plant.</p>")), True)

print("\n=== one state, or none ===")
check("headline wins over the body",
      s.find_state("Bihar tender. Body mentions Odisha and nothing else.",
                   "Solar tender in Bihar"), "bihar")
# A round-up naming five states is not news about whichever comes first.
check("several states in the body -> unclear, not the first one",
      s.find_state("Rollout across Karnataka, Maharashtra, Bihar and Odisha today."),
      None)
check("several states in the headline -> unclear",
      s.find_state("plant moves", "Plant Shifts from Andhra Pradesh to West Bengal"),
      None)
check("exactly one state in the body is used",
      s.find_state("The project sits in Jharkhand near the border."), "jharkhand")
check("no state at all -> None", s.find_state("A solar plant was commissioned."), None)
check("states_in reports them in the order they appear",
      s.states_in("First Odisha, then Bihar, then Assam."),
      ["odisha", "bihar", "assam"])
check("untracked states are ignored",
      s.find_state("A project in Gujarat and Rajasthan."), None)

print("\n=== a keyword deep in the body is a passing mention ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
s = load()

_lede = "A solar plant was commissioned in Bihar."
_deep = "A tariff order was issued. " + ("filler text. " * 60) + " mentions solar."
check("keyword in the headline counts",
      s.matches_topic(s.topical_text("NTPC commissions solar plant", "")), True)
check("keyword in the lede counts",
      s.matches_topic(s.topical_text("Tariff order issued", _lede)), True)
check("keyword past the lede does not",
      s.matches_topic(s.topical_text("Tariff order issued", _deep)), False)
check("the whole body would have matched it",
      s.matches_topic("Tariff order issued " + _deep), True)

print("\n=== a state in a company name is not a location ===")
# Live: "MB Power Madhya Pradesh Ltd" in a bidder list filed a national SECI
# tender under Madhya Pradesh.
check("bidder name does not set the state",
      s.find_state("Winners included Rama Reflection Pvt Ltd and "
                   "MB Power Madhya Pradesh Ltd."), None)
check("a real location still does",
      s.find_state("The project sits in Agar Malwa, Madhya Pradesh."), "madhya pradesh")
check("cue may sit a few words back",
      s.find_state("A BESS project in Choutuppal, Telangana was announced."), "telangana")
check("headline still wins without needing a cue",
      s.find_state("body text", "Madhya Pradesh Ltd wins order"), "madhya pradesh")
check("states_in without a cue sees every mention",
      s.states_in("MB Power Madhya Pradesh Ltd"), ["madhya pradesh"])
check("states_in with a cue rejects the company name",
      s.states_in("MB Power Madhya Pradesh Ltd", require_cue=True), [])
# the ambiguity guard must not be weakened by the cue filter
check("a list of states is still ambiguous",
      s.find_state("Rollout across Bihar, Odisha and Assam."), None)

print("\n=== the client copy carries no diagnostics ===")
# The digest is forwarded to a client, so it must never expose infrastructure,
# costs, file paths, or the fact that a third of the sources are broken.
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
requests.get = _get
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("z1"))
s_ = load(); SENT.clear(); s_.main()                      # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("z2"))
s_ = load(); SENT.clear(); s_.main()
client = SENT[0]["textContent"]

check("client still gets the news", "BIHAR" in client, True)
check("client still gets the tenders", "TENDERS & GOVT NOTICES" in client, True)
for leak in ("Feeds:", "LLM calls", "scrapes:", "Storage:", "portal error",
             "Deferred", "story(ies) after dedup", "STORAGE PROBLEM",
             "LLM UNAVAILABLE", "captcha"):
    check("client copy hides %r" % leak, leak in client, False)
check("no database path leaks", ".db" in client, False)

# ...while the owner's copy keeps every one of them
own = internal()
check("owner copy keeps the feed tally", "| Feeds |" in own, True)
check("owner copy keeps the budget", "| LLM calls |" in own, True)
check("owner copy keeps the storage line", "Storage" in own, True)

# a seeding run reaches no client at all now, which also settles the question of
# whether it might explain databases to one
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("z3"))
s_ = load(); SENT.clear(); s_.main()
check("a seeding run reaches no client", len(SENT), 0)
check("the owner still gets the explanation", "already reviewed" in internal(), True)
requests.get = _orig_get

print("\n=== a seeding run must not look like a filter failure ===")
# A first deploy logged "475 new, 0 matched" across every feed, which reads as
# the keyword gate having rejected everything. It had assessed nothing: seeding
# marks each entry seen before the gate ever runs.
import io as _io
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sd"))
_buf, _real = _io.StringIO(), sys.stdout
sys.stdout = _buf
try:
    s_ = load(); SENT.clear(); s_.main()
finally:
    sys.stdout = _real
_log = _buf.getvalue()
check("seeding says seeded, not matched", "seeded (not assessed)" in _log, True)
check("seeding never claims 0 matched", "0 matched" in _log, False)

# a real run still reports matches the usual way
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sd2"))
_buf, _real = _io.StringIO(), sys.stdout
sys.stdout = _buf
try:
    s_ = load(); SENT.clear(); s_.main()
finally:
    sys.stdout = _real
check("a real run still reports matched", "matched" in _buf.getvalue(), True)

print("\n=== tenders always run, and nothing captured is trimmed ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("MAX_ITEMS_PER_EMAIL", None)
os.environ.pop("MAX_TENDERS_PER_EMAIL", None)
requests.get = _get
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("nc1"))
s_ = load(); SENT.clear(); s_.main()                      # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("nc2"))
s_ = load(); SENT.clear(); s_.main()
_b = SENT[0]["textContent"]

check("tenders run with no flag set", "TENDERS & GOVT NOTICES" in _b, True)
check("and the tender is listed", "Rooftop Solar Plant" in _b, True)
check("no flag is consulted any more", hasattr(s_, "TENDERS_ENABLED"), False)

# 0 means "send everything", which is what the caps now default to
check("cap of 0 keeps every item", s_.capped(list(range(9)), 0), list(range(9)))
check("negative cap also keeps everything", s_.capped(list(range(9)), -1), list(range(9)))
check("a real cap still trims", s_.capped(list(range(9)), 4), [0, 1, 2, 3])
check("news cap defaults to unlimited", s_.MAX_ITEMS_PER_EMAIL, 0)
check("tender cap defaults to unlimited", s_.MAX_TENDERS_PER_EMAIL, 0)
check("nothing was truncated", "not shown" in _b, False)

# an explicit cap must still be honoured
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["MAX_ITEMS_PER_EMAIL"] = "1"
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("nc3"))
s_ = load(); SENT.clear(); s_.main()
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("nc4"))
s_ = load(); SENT.clear(); s_.main()
check("an explicit cap is still obeyed",
      "| Stories after dedup | 1 |" in internal(), True)
os.environ.pop("MAX_ITEMS_PER_EMAIL", None)
requests.get = _orig_get

print("\n=== the subject line a client sees ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("EMAIL_SUBJECT", None)
requests.get = _get
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sj1"))
s_ = load(); SENT.clear(); s_.main()                      # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sj2"))
s_ = load(); SENT.clear(); s_.main()

check("subject is the plain client line", SENT[0]["subject"], "Today's Solar Alerts")
check("no story count in the subject", "story" in SENT[0]["subject"], False)
check("no internal product name", "BESS" in SENT[0]["subject"], False)

# a quiet day sends nothing at all, so no subject line reaches anyone
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sj3"))
s_ = load(); SENT.clear(); s_.main()                      # seeding = no news
check("a day with nothing to report sends no subject at all", len(SENT), 0)

# still overridable
os.environ["EMAIL_SUBJECT"] = "Khetan Solar Digest"
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sj4"))
s_ = load(); SENT.clear(); s_.main()                      # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sj5"))
s_ = load(); SENT.clear(); s_.main()
check("EMAIL_SUBJECT overrides it", SENT[0]["subject"], "Khetan Solar Digest")
os.environ.pop("EMAIL_SUBJECT", None)
requests.get = _orig_get

print("\n=== an empty run must not reach the client ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("SEND_WHEN_EMPTY", None)
requests.get = _orig_get                      # portals unreachable -> no tenders
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("q1"))
s_ = load(); SENT.clear(); s_.main()          # seeding: nothing was assessed
check("a seeding run sends nothing", len(SENT), 0)
# ...but what it reviewed is recorded, so tomorrow does not redo it
check("seeding still recorded what it reviewed", s_.seen_count() > 0, True)

# real news goes out as usual
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("q2"))
s_ = load(); SENT.clear(); s_.main()
check("a day with news still sends", len(SENT), 1)
check("and it carries the news", "BIHAR" in SENT[0]["textContent"], True)

# the very same feed again: every URL is already seen, so there is nothing to say
s_ = load(); SENT.clear(); s_.main()
check("a day with no new items sends nothing", len(SENT), 0)

# the old always-send behaviour is still available
os.environ["SEND_WHEN_EMPTY"] = "1"
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("q3"))
s_ = load(); SENT.clear(); s_.main()          # seeding, which normally stays quiet
check("SEND_WHEN_EMPTY restores the heartbeat", len(SENT), 1)
os.environ.pop("SEND_WHEN_EMPTY", None)

print("\n=== article pages are fetched the way feeds are ===")
# 28 of 29 scrape failures were HTTP 403 from four Cloudflare publishers whose
# feeds we had already recovered - newspaper3k was fetching the article page
# with its own plain client and hitting the same wall one layer down.
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
s = load()

_calls = []
class _Resp:
    def __init__(self, code, text): self.status_code, self.text = code, text

def _walled(url, **kw):
    _calls.append(("plain", url))
    return _Resp(403, "cloudflare challenge")

class _Imp:
    @staticmethod
    def get(url, **kw):
        _calls.append(("impersonated", url, kw.get("impersonate")))
        return _Resp(200, "<html><body><p>the article body</p></body></html>")

_saved_get, _saved_imp = s.requests.get, s.impersonator
s.requests.get, s.impersonator = _walled, _Imp
try:
    html = s.fetch_article_html("https://www.pv-tech.org/story/")
finally:
    s.requests.get, s.impersonator = _saved_get, _saved_imp

check("a 403 is retried as a browser", [c[0] for c in _calls],
      ["plain", "impersonated"])
check("and it impersonates chrome", _calls[1][2], "chrome")
check("the article html comes back", "the article body" in html, True)

# with no impersonator available it must fail cleanly, not hang or lie
s.requests.get, s.impersonator = _walled, None
try:
    _err = None
    try:
        s.fetch_article_html("https://www.pv-tech.org/story/")
    except Exception as e:
        _err = str(e)
finally:
    s.requests.get, s.impersonator = _saved_get, _saved_imp
check("without impersonation it raises the status", _err, "HTTP 403")

# a normal 200 must not spend an impersonation request
_calls.clear()
def _fine(url, **kw):
    _calls.append(("plain", url))
    return _Resp(200, "<html><body><p>ordinary page</p></body></html>")
s.requests.get = _fine
try:
    html = s.fetch_article_html("https://example.com/a")
finally:
    s.requests.get = _saved_get
check("a 200 is left alone", [c[0] for c in _calls], ["plain"])

print("\n=== the author's bio is not the story's location ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
s = load()
# Live: "Rooftop Solar after PM Surya Ghar" was filed under Andhra Pradesh
# because character 6648 of 7896 read "...student at Madanapalle Institute of
# Technology, Andhra Pradesh". A byline is not a dateline.
_lede = "A rooftop solar scheme review. " * 8
_bio = " The author is a student at an institute of technology in Andhra Pradesh."
_article = _lede + ("More discussion of the scheme. " * 60) + _bio

check("the whole body finds the bio's state",
      s.find_state(_article, "Rooftop Solar after PM Surya Ghar"), "andhra pradesh")
check("the head of the article does not",
      s.find_state(_article[:s.BODY_HEAD_CHARS], "Rooftop Solar after PM Surya Ghar"),
      None)
# mirror the real call: the pipeline truncates before asking
check("a state named up front is still read",
      s.find_state(("A 300 MW plant was commissioned in Bihar. " + _article)[:s.BODY_HEAD_CHARS],
                   "Plant commissioned"), "bihar")
# and the untruncated body is genuinely ambiguous - two states, so neither wins
check("untruncated, two states means none",
      s.find_state("A 300 MW plant was commissioned in Bihar. " + _article,
                   "Plant commissioned"), None)
check("the window is configurable", s.BODY_HEAD_CHARS, 1500)

print("\n=== a rate limit is not a verdict ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["SKIP_SEEDING"] = "1"
os.environ.pop("MAX_LLM_CALLS_PER_RUN", None)   # an earlier case pinned it to 0
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("rl"))

# Groq free tier allows ~11 of these prompts a minute, so a real run WILL be
# rate limited. Every 429 used to mark the article seen and lose it for good.
STATE_ANS = "NONE"
s = load()
check("a genuine NONE is a verdict", s.llm_extract_state("t", "body"), None)
# an exhausted budget means the article was never judged either
s.budget.llm_calls = s.MAX_LLM_CALLS_PER_RUN
check("an exhausted budget is not a verdict",
      s.llm_extract_state("t", "body") is s.LLM_FAILED, True)
s.budget.llm_calls = 0

_saved_create = groq.Groq.create
def _rate_limited(self, model, messages, **kw):
    raise RuntimeError("Error code: 429 - rate_limit_exceeded")
groq.Groq.create = _rate_limited
s = load()
check("a 429 is not a verdict", s.llm_extract_state("t", "body") is s.LLM_FAILED, True)

# and the article survives it. These headlines name no tracked state, so they
# are exactly the ones that reach the LLM.
NO_STATE = [
    {"title": "NTPC commissions 300 MW solar at Rihand", "link": "http://n/1",
     "summary": "solar project commissioned"},
    {"title": "SECI floats 500 MW solar tender at Kurnool", "link": "http://n/2",
     "summary": "solar tender floated"},
]
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=NO_STATE)
s = load(); SENT.clear(); s.main()
check("these headlines really do reach the LLM", s.budget.scrapes > 0, True)
check("rate-limited articles are deferred, not dropped", s.budget.deferred, 2)
check("and none of them was marked seen", s.seen_count(), 0)

groq.Groq.create = _saved_create

# next run, with the LLM answering again, the same articles are still judged
STATE_ANS = "bihar"
s2 = load(); SENT.clear(); s2.main()
check("the next run judges them instead of losing them", s2.budget.deferred, 0)
check("and they are recorded once judged", s2.seen_count() > 0, True)
STATE_ANS = "NONE"

# no model at all is also not a verdict
_saved = MODELS[:]
MODELS.clear(); MODELS.extend(["whisper-large-v3"])
s3 = load()
check("no usable model is not a verdict either",
      s3.llm_extract_state("t", "body") is s3.LLM_FAILED, True)
MODELS.clear(); MODELS.extend(_saved)
os.environ.pop("SKIP_SEEDING", None)

print("\n=== every story carries a summary ===")
# The digest lists a headline and a link. Without a sentence saying what
# happened, a reader has to open every link to find the one that matters.
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("SKIP_SEEDING", None)
s = load()

_long = ("The state utility signed a power purchase agreement for a 250 MW solar "
         "project in Bihar at a tariff of Rs 2.48 per unit. Construction starts "
         "in November and the plant is due to commission by March 2028. The "
         "developer will also build a 50 MWh battery.")
check("a short lede is kept whole",
      s.lede_summary("A 250 MW plant was commissioned."),
      "A 250 MW plant was commissioned.")
check("a long lede is cut at a sentence, not mid-word",
      s.lede_summary(_long, 120).endswith("."), True)
check("and stays within the limit", len(s.lede_summary(_long, 120)) <= 121, True)
check("markup never reaches the summary",
      s.lede_summary("<p>A <b>250 MW</b> plant.</p>"), "A 250 MW plant.")
check("a byline is not a summary",
      s.lede_summary("By Our Correspondent | A 250 MW plant opened."),
      "A 250 MW plant opened.")
check("a share prompt is not a summary",
      s.lede_summary("Share this Advertisement A 250 MW plant opened."),
      "A 250 MW plant opened.")
# no sentence break inside the window: cut on a word and mark it
_nostop = "Bihar signs a power purchase agreement for a very large solar project"
check("a wordy lede with no full stop is truncated visibly",
      s.lede_summary(_nostop, 40).endswith("…"), True)
check("truncation never splits a word",
      " " not in s.lede_summary(_nostop, 40)[-2:], True)
check("empty text yields an empty summary", s.lede_summary(""), "")
check("None is survivable", s.lede_summary(None), "")

# Publishers mark their own truncation four different ways; the client sees one.
check("a feed's [...] becomes one ellipsis",
      s.lede_summary("Solaryaan launched a 10.24 kWh battery [...]"),
      "Solaryaan launched a 10.24 kWh battery…")
check("so does a trailing ...",
      s.lede_summary("Tata Power commissioned 100 MW ..."),
      "Tata Power commissioned 100 MW…")
check("so does 'Read more'",
      s.lede_summary("CIP acquired the project […] Read more"),
      "CIP acquired the project…")
check("a complete sentence is not given one",
      s.lede_summary("A 250 MW plant was commissioned."),
      "A 250 MW plant was commissioned.")
check("a description that is only a marker is not blanked",
      s.lede_summary("[...]") != "", True)

# EQ Mag opens every description with this, so it led half a live report.
check("'In Short :' is a label, not a summary",
      s.lede_summary("In Short : South Bihar University secured a patent."),
      "South Bihar University secured a patent.")
check("but the same words mid-sentence are left alone",
      s.lede_summary("The project is in short supply of panels."),
      "The project is in short supply of panels.")

# the richer of feed description and scraped body wins
check("the scraped body beats a stub description",
      s.best_summary_source("T", "Read on.", _long), _long)
check("a full description beats an empty body",
      s.best_summary_source("T", _long, ""), _long)
check("the headline is the last resort",
      s.best_summary_source("Headline only", "", ""), "Headline only")

print("\n=== the model rewrites the lede, and failure keeps it ===")
_clusters = [{"title": "T%d" % i, "url": "u%d" % i, "state": "bihar",
              "sources": ["EQ Mag"], "summary": "lede %d" % i,
              "fingerprint": set()} for i in range(3)]
s.summarize_clusters(_clusters)
check("the model's sentence replaces the lede",
      [c["summary"] for c in _clusters],
      ["Model sentence 0.", "Model sentence 1.", "Model sentence 2."])

FAIL_SUMMARY = True
_clusters = [{"title": "T", "url": "u", "state": "bihar", "sources": ["EQ Mag"],
              "summary": "the lede survives", "fingerprint": set()}]
s.summarize_clusters(_clusters)
check("a rate-limited summary keeps the lede",
      _clusters[0]["summary"], "the lede survives")
check("and the story is still in the digest", len(_clusters), 1)
FAIL_SUMMARY = False

# a model that answers with the wrong shape must not blank the summary
_saved_create2 = groq.Groq.create
def _garbage(self, model, messages, **kw):
    if "Summarise each news item" in messages[0]["content"]:
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content='{"summaries": {"0": ""}}'))])
    return _saved_create2(self, model, messages, **kw)
groq.Groq.create = _garbage
_clusters = [{"title": "T", "url": "u", "state": "bihar", "sources": ["EQ Mag"],
              "summary": "the lede survives", "fingerprint": set()}]
s.summarize_clusters(_clusters)
check("an empty model answer keeps the lede too",
      _clusters[0]["summary"], "the lede survives")
groq.Groq.create = _saved_create2

# summaries must never outrank the calls that decide whether news IS news
s.budget.llm_calls = s.MAX_LLM_CALLS_PER_RUN
_clusters = [{"title": "T", "url": "u", "state": "bihar", "sources": ["EQ Mag"],
              "summary": "the lede survives", "fingerprint": set()}]
s.summarize_clusters(_clusters)
check("an exhausted budget spends nothing on polish",
      s.budget.llm_calls, s.MAX_LLM_CALLS_PER_RUN)
check("and the lede still stands", _clusters[0]["summary"], "the lede survives")
s.budget.llm_calls = 0

print("\n=== the summary reaches both outputs ===")
_one = [{"title": "SECI tenders 1200 MW", "url": "http://a", "state": "bihar",
         "sources": ["EQ Mag"], "summary": "SECI invited bids for 1,200 MW.",
         "fingerprint": set()}]
_mail = s.build_email(_one, 1, False, diagnostics=False)
_md = s.build_report(_one, 1, False, diagnostics=False)
check("the email carries the summary", "SECI invited bids for 1,200 MW." in _mail, True)
check("the report carries the summary", "SECI invited bids for 1,200 MW." in _md, True)
check("the email still carries the link", "http://a" in _mail, True)
check("the report still links the headline",
      "[SECI tenders 1200 MW](http://a)" in _md, True)
# a story the model never got to must still print
_bare = [{"title": "No summary here", "url": "http://b", "state": "bihar",
          "sources": ["EQ Mag"], "summary": "", "fingerprint": set()}]
check("a summary-less story still lists",
      "No summary here" in s.build_email(_bare, 1, False, diagnostics=False), True)
check("and in the report too",
      "No summary here" in s.build_report(_bare, 1, False, diagnostics=False), True)

print("\n=== a full run puts a summary in front of the client ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sm1"))
s = load(); SENT.clear(); s.main()                       # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sm2"))
s = load(); SENT.clear(); s.main()
_client = SENT[0]["textContent"]
check("the client digest has a summary line", "Model sentence 0." in _client, True)
check("summaries cost one batched call, not one per story",
      s.budget.llm_calls <= 2 + s.MAX_SUMMARY_CALLS_PER_RUN, True)

print("\n=== SUMMARY_ENABLED=0 falls back to the lede everywhere ===")
os.environ.update(DB_PATH=tempfile.mktemp(suffix=".db"), SUMMARY_ENABLED="0")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sm3"))
s = load(); SENT.clear(); s.main()                       # seeds
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("sm4"))
s = load(); SENT.clear(); s.main()
_client = SENT[0]["textContent"]
check("no model sentence when summaries are off", "Model sentence" in _client, False)
# The Bihar cluster keeps the fullest lede any of its outlets carried, which
# here is the second headline's, not the first's.
check("but the feed's own lede is still there",
      "solar tender Bihar announced" in _client, True)
check("and the Sikkim story keeps its own",
      "solar project Sikkim" in _client, True)
os.environ.pop("SUMMARY_ENABLED", None)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
