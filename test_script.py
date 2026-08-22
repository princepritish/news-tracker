"""Daily-digest tests: seeding, dedup, budgets, storage failure, always-send."""
import json, os, sys, types, tempfile, importlib.util

CLUSTERS = {"groups": [[0, 1], [2]]}
STATE_ANS = "NONE"
FAIL_CLUSTER = False

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
        else:
            out = STATE_ANS
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=out))])
groq.Groq = _G
sys.modules["groq"] = groq

np = types.ModuleType("newspaper")
class Article:
    def __init__(s, u): s.url, s.text = u, ""
    def download(s): pass
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
body = SENT[0]["textContent"]
check("one email sent", len(SENT), 1)
check("no news listed", "SECI" in body, False)
check("explains seeding", "already reviewed" in internal(), True)

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

print("\n=== run 3: nothing new, still sends ===")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("b"))
s = load(); SENT.clear(); s.main()
check("email still sent", len(SENT), 1)
check("says no news", "No new matching news today" in SENT[0]["textContent"], True)

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
_b = SENT[0]["textContent"]
check("seeds on empty db", "recorded as already reviewed" in internal(), True)
check("explains deployment cause", "deployment" in internal(), True)
check("no news listed", "BIHAR" in _b, False)

print("\n=== feed failure counted ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
def bad_get(u, **k): raise ConnectionError("dns")
rq.get = bad_get
s = load(); SENT.clear(); s.main()
body = SENT[0]["textContent"]
check("feed error reported", "1 failed" in internal(), True)
check("still sends", len(SENT), 1)
rq.get = get

print("\n=== no usable Groq model ===")
import builtins
_saved = MODELS[:]
MODELS.clear(); MODELS.extend(["whisper-large-v3", "llama-guard-4"])  # no chat model
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("e"))
s = load(); SENT.clear(); s.main()
_b = SENT[0]["textContent"]
check("model warning reaches the owner", "LLM UNAVAILABLE" in internal(), True)
check("still sends", len(SENT), 1)
check("no llm calls attempted", s.budget.llm_calls, 0)
MODELS.clear(); MODELS.extend(_saved)

print("\n=== LLM budget cap ===")
os.environ.update(DB_PATH=tempfile.mktemp(suffix=".db"), MAX_LLM_CALLS_PER_RUN="0")
s = load(); SENT.clear(); s.main(); SENT.clear(); s2 = load(); s2.main()
check("llm calls stay at 0", s2.budget.llm_calls, 0)
check("still sends", len(SENT), 1)

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
os.environ["TENDERS_ENABLED"] = "1"
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
check("tender not repeated next run", "Rooftop Solar Plant" in SENT[0]["textContent"], False)

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
os.environ["TENDERS_ENABLED"] = "0"
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
os.environ["TENDERS_ENABLED"] = "1"
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

# a seeding run must not explain databases to a client either
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("z3"))
s_ = load(); SENT.clear(); s_.main()
seed_client = SENT[0]["textContent"]
check("seeding run says nothing technical to the client",
      any(w in seed_client for w in ("deployment", "database", "already reviewed")), False)
check("and still sends something", len(SENT), 1)
requests.get = _orig_get
os.environ["TENDERS_ENABLED"] = "0"

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
