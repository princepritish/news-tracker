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

os.chdir("/home/user/news-tracker")
DB = tempfile.mktemp(suffix=".db")
os.environ.update(GROQ_API_KEY="x", BREVO_API_KEY="x",
                  EMAIL_TO="a@b.com,c@d.com", DB_PATH=DB)

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
check("explains seeding", "already reviewed" in body, True)

def fresh_batch(tag):
    """New URLs so they are genuinely unseen after the seeding run."""
    return [dict(e, link=e["link"] + tag) for e in ENTRIES]

print("\n=== run 2: real news, dedup ===")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("b"))
s = load(); SENT.clear(); s.main()
body = SENT[0]["textContent"]
check("one email", len(SENT), 1)
check("3 matched -> 2 stories", "2 story(ies) after dedup" in body, True)
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
check("no story lost (3 stories)", "3 story(ies) after dedup" in body, True)
FAIL_CLUSTER = False

print("\n=== storage failure surfaces in email ===")
os.environ["DB_PATH"] = "/proc/nope/seen.db"     # unwritable
s = load(); SENT.clear(); s.main()
check("warning in email", "STORAGE PROBLEM" in SENT[0]["textContent"], True)
check("names the fix", "history was written to" in SENT[0]["textContent"].lower() or "STORAGE PROBLEM" in SENT[0]["textContent"], True)

print("\n=== empty db seeds and explains why ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
feedparser.parse = lambda *a, **k: types.SimpleNamespace(entries=fresh_batch("d"))
s = load(); SENT.clear(); s.main()
_b = SENT[0]["textContent"]
check("seeds on empty db", "recorded as already reviewed" in _b, True)
check("explains deployment cause", "deployment" in _b, True)
check("no news listed", "BIHAR" in _b, False)

print("\n=== feed failure counted ===")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
def bad_get(u, **k): raise ConnectionError("dns")
rq.get = bad_get
s = load(); SENT.clear(); s.main()
body = SENT[0]["textContent"]
check("feed error reported", "1 failed" in body, True)
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
check("model warning in email", "LLM UNAVAILABLE" in _b, True)
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
check("tender count in footer", "Tenders:" in _b, True)

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
check("portal errors reported", "portal error" in _b, True)

requests.get = _orig_get
os.environ["TENDERS_ENABLED"] = "0"
os.environ.pop("SKIP_SEEDING", None)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
