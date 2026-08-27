"""Offline tests for tenders.py (Phase 2).

No network: portal HTML is supplied inline. Covers keyword matching, link
harvesting, the identity key, and the failure paths that must never break a run.

    python test_tenders.py
"""

import re
import sys
import threading
import time
import types

import tenders

OK = True


def check(label, got, want):
    global OK
    good = got == want
    OK = OK and good
    print(f"{'PASS' if good else 'FAIL'}  {label}"
          + ("" if good else f"\n      got={got!r}\n      want={want!r}"))


# ---------------- keyword matching ----------------
print("=== keyword matching ===")
match = tenders.build_matcher(["solar", "pv", "epc", "rooftop solar", "spv"])

check("plain hit", match("Supply of Solar Panels for District Office"), True)
check("case insensitive", match("SUPPLY OF SOLAR MODULES"), True)
check("phrase hit", match("Rooftop Solar Plant 100 kWp"), True)
# Short keywords must be word-bounded or they fire on unrelated words.
check("'pv' does not match PVC", match("Supply of PVC pipes for drainage"), False)
check("'pv' matches standalone", match("Installation of PV modules at site"), True)
check("'epc' does not match inside word", match("Repcast machinery tender"), False)
check("off topic", match("Construction of boundary wall at school"), False)


# ---------------- link harvesting ----------------
print("\n=== noise exclusions ===")
# Each of these matched a tender keyword and reached the report before the
# exclusion list existed.
for text in ("Construction of solar plant boundary wall",
             "Hiring of vehicles for renewable energy department",
             "Supply of battery for UPS backup",
             "Housekeeping services at solar park",
             "Supply of furniture for renewable energy office"):
    check(f"excluded: {text[:44]}", match(text), False)

# Real procurement must survive the exclusions - especially solar street
# lighting, which is a genuine category here rather than municipal noise.
for text in ("Supply and installation of 500 kWp rooftop solar plant",
             "Supply of solar street lights in Bihar",
             "EPC tender for 100 MW solar park",
             "Annual maintenance of rooftop solar plant"):
    check(f"kept: {text[:48]}", match(text), True)

print("\n=== link harvesting ===")
HTML = """
<html><body>
<a href="/">Home</a>
<a href="#top">Skip to main content</a>
<a href="javascript:void(0)">Search</a>
<a href="/nicgep/app?page=FrontEndTenderDetails&id=99">
   Supply, Installation and Commissioning of 500 kWp Rooftop Solar Plant</a>
<a href="/nicgep/app?page=FrontEndTenderDetails&id=100">Construction of toilet block</a>
<a href="/nicgep/app?page=FrontEndTenderDetails&id=101">
   <span>Tender for</span> Solar Water Pumping Systems - 250 Nos</a>
<a href="/x">Next</a>
</body></html>
"""
links = tenders.harvest_links(HTML, "https://jharkhandtenders.gov.in/nicgep/app")
texts = [t for t, _ in links]

check("navigation dropped", any(t.lower() == "home" for t in texts), False)
check("javascript dropped", any("Search" == t for t in texts), False)
check("short link dropped", any(t == "Next" for t in texts), False)
check("real tenders harvested", len(links), 3)
check("nested tags flattened",
      any(t.startswith("Tender for Solar Water Pumping") for t in texts), True)
check("urls absolute",
      all(u.startswith("https://jharkhandtenders.gov.in") for _, u in links), True)

matched = [t for t, _ in links if match(t)]
check("only solar ones match keywords", len(matched), 2)


# ---------------- identity key ----------------
print("\n=== tender identity ===")
a = {"title": "Supply of 500 kWp Rooftop Solar Plant",
     "source": "jharkhandtenders.gov.in"}
b = {"title": "supply of 500 kwp rooftop solar plant!!",
     "source": "jharkhandtenders.gov.in"}
d = {"title": "Supply of 500 kWp Rooftop Solar Plant",
     "source": "assamtenders.gov.in"}

check("case and punctuation ignored", tenders.tender_key(a), tenders.tender_key(b))
check("different portal is different tender",
      tenders.tender_key(a) != tenders.tender_key(d), True)
# The URL is deliberately not part of the key: GePNIC detail links carry session
# parameters that change per visit, which would re-report the same tender daily.
check("key ignores url", "http" in tenders.tender_key(a), False)


# ---------------- gepnic deep links ----------------
print("\n=== gepnic candidates ===")
cands = tenders.gepnic_candidates("https://jharkhandtenders.gov.in/nicgep/app?page=Home")
check("builds latest-active-tenders url",
      any("FrontEndLatestActiveTenders" in u for u in cands), True)
check("non-gepnic gets none",
      tenders.gepnic_candidates("https://bsphcl.co.in/tenders.html"), [])


# ---------------- failure isolation ----------------
print("\n=== failures never raise ===")


class FakeSession:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        b = self.behaviour
        if b == "boom":
            raise ConnectionError("portal down")
        if b == "500":
            return types.SimpleNamespace(status_code=500, text="")
        if b == "empty":
            return types.SimpleNamespace(status_code=200, text="<html></html>")
        return types.SimpleNamespace(status_code=200, text=HTML)


portal = {"label": "e-tender portal", "url": "https://x.gov.in/page", "kind": "etender"}

for behaviour, expect_items in (("boom", 0), ("500", 0), ("empty", 0), ("ok", 2)):
    items, err = tenders.fetch_portal(portal, "Bihar", match, 5,
                                      session=FakeSession(behaviour))
    check(f"{behaviour}: returns {expect_items} item(s), no raise",
          len(items), expect_items)
    check(f"{behaviour}: error reported when empty", bool(err), expect_items == 0)

SOURCES = [{"state": "Bihar", "portals": [portal, dict(portal, label="tender page",
                                                       kind="tender_page")]},
           {"state": "Assam", "portals": [portal]}]

items, errors = tenders.collect(SOURCES, ["solar"], max_fetches=99,
                                session=FakeSession("ok"), collapse=False)
check("collect walks every portal", len(items), 6)
check("collect reports no errors on success", errors, [])

items, errors = tenders.collect(SOURCES, ["solar"], max_fetches=2,
                                session=FakeSession("ok"), collapse=False)
check("fetch budget is enforced", len(items), 4)
check("budget exhaustion is reported", any("budget" in e for e in errors), True)

items, errors = tenders.collect(SOURCES, ["solar"], max_fetches=99,
                                session=FakeSession("boom"))
check("all portals down yields no items", items, [])
check("all portals down yields errors", len(errors), 3)

check("bad sources shape does not raise",
      tenders.collect([{"state": "X", "portals": []}], ["solar"], 5), ([], []))


# ---------------- meta-refresh stubs ----------------
# Several GePNIC portals serve nothing but a redirect stub. requests follows HTTP
# redirects but not this one, so before it was followed those portals harvested
# as zero links and were written off as empty.
print("\n=== meta refresh ===")

STUB = ('<html><title>eProcurement</title><head>'
        '<META http-equiv="refresh" content="0;url=https://x.gov.in/nicgep/app">'
        '</head></html>')

check("finds the redirect target",
      tenders.meta_refresh_target(STUB, "https://x.gov.in/"),
      "https://x.gov.in/nicgep/app")
check("relative target is made absolute",
      tenders.meta_refresh_target(
          '<meta http-equiv="refresh" content="0; url=/tenders/list.html">',
          "https://y.gov.in/home/"),
      "https://y.gov.in/tenders/list.html")
check("ordinary page has no target",
      tenders.meta_refresh_target("<html><body>hi</body></html>", "https://z/"), None)
check("meta tag that is not a refresh is ignored",
      tenders.meta_refresh_target('<meta name="description" content="url=nope">',
                                  "https://z/"), None)


class RedirectSession:
    """Serves the stub once, then the real listing at the redirect target."""

    def __init__(self):
        self.seen = []

    def get(self, url, **kw):
        self.seen.append(url)
        if url.endswith("/nicgep/app"):
            return types.SimpleNamespace(status_code=200, text=HTML)
        return types.SimpleNamespace(status_code=200, text=STUB)


rs = RedirectSession()
items, err = tenders.fetch_portal(
    {"label": "e-tender portal", "url": "https://x.gov.in/", "kind": "etender"},
    "Bihar", match, 5, session=rs)
check("follows the stub to the real page", len(items), 2)
check("no error once followed", err, None)
check("the redirect target was fetched",
      any(u.endswith("/nicgep/app") for u in rs.seen), True)


class LoopSession:
    """A stub that points at itself - must not spin forever."""

    def __init__(self):
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return types.SimpleNamespace(
            status_code=200,
            text='<meta http-equiv="refresh" content="0;url=https://loop.gov.in/">')


ls = LoopSession()
items, err = tenders.fetch_portal(
    {"label": "tender page", "url": "https://loop.gov.in/", "kind": "tender_page"},
    "Assam", match, 5, session=ls)
check("self-referential redirect terminates", bool(err), True)
check("and does not hammer the portal", ls.calls <= 3, True)


# ---------------- captcha walls ----------------
# Every GePNIC listing page hides the tender list behind a captcha. Reporting
# that as "no matching tenders" sent the owner hunting for a bug that was not
# there, so it is now named, and the remaining candidates are not spent on it.
print("\n=== captcha detection ===")

CAPTCHA_PAGE = ('<html><body><table><tr><td>Tender Title</td>'
                '<td><input name="TenderTitle"/></td></tr>'
                '<tr><td>Enter Captcha</td><td><img id="captchaImage"/></td></tr>'
                '</table></body></html>')


class CaptchaSession:
    def __init__(self):
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return types.SimpleNamespace(status_code=200, text=CAPTCHA_PAGE)


cs = CaptchaSession()
items, err = tenders.fetch_portal(
    {"label": "e-tender portal", "url": "https://gep.tenders.gov.in/nicgep/app",
     "kind": "etender"},
    "Odisha", match, 5, session=cs)
check("captcha page yields nothing", items, [])
check("and says so plainly", "captcha" in (err or "").lower(), True)
# gepnic_candidates offers three URLs for this host; the wall is the same on all
# of them, so stopping at the first is a request saved on every portal, daily.
check("stops instead of trying every candidate", cs.calls, 1)

check("a page with no captcha still reports plainly",
      tenders.fetch_portal(
          {"label": "tender page", "url": "https://q.gov.in/", "kind": "tender_page"},
          "Bihar", match, 5,
          session=FakeSession("empty"))[1],
      "no matching tenders")


# ---------------- portal kinds ----------------
# The e-tender portals are captcha-gated, so the readable tenders live on the
# agency and DISCOM sites. Reading only the first two kinds found nothing.
print("\n=== portal kinds ===")

check("agency and discom are read by default",
      set(tenders.DEFAULT_PORTAL_KINDS),
      {"etender", "tender_page", "agency", "discom"})

MIXED = [{"state": "Bihar", "portals": [
    {"label": "e-tender portal", "url": "https://a.gov.in/", "kind": "etender"},
    {"label": "REDA agency", "url": "https://b.gov.in/", "kind": "agency"},
    {"label": "DISCOM", "url": "https://c.gov.in/", "kind": "discom"},
    {"label": "other", "url": "https://d.gov.in/", "kind": "something_else"},
]}]

items, errors = tenders.collect(MIXED, ["solar"], max_fetches=99,
                                session=FakeSession("ok"), collapse=False)
check("reads all four known kinds, skips the unknown", len(items), 6)

items, errors = tenders.collect(MIXED, ["solar"], max_fetches=99,
                                session=FakeSession("ok"),
                                portal_kinds=("discom",))
check("portal_kinds narrows the walk", len(items), 2)


# ---------------- concurrency ----------------
# Portals are fetched in parallel, but the digest must read identically however
# the network answers - so results stay in configuration order.
print("\n=== concurrent collection ===")

WIDE = [{"state": "S%d" % n, "portals": [
    {"label": "tender page", "url": "https://s%d.gov.in/" % n, "kind": "tender_page"}]}
    for n in range(12)]


class OrderScrambler:
    """Answers out of order, and records how many calls overlapped."""

    def __init__(self):
        self.live = 0
        self.peak = 0
        self.lock = threading.Lock()

    def get(self, url, **kw):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        # later hosts answer fastest, so completion order != configured order
        n = int(re.search(r"s(\d+)", url).group(1))
        time.sleep((12 - n) * 0.01)
        with self.lock:
            self.live -= 1
        return types.SimpleNamespace(status_code=200, text=HTML)


sc = OrderScrambler()
items, errors = tenders.collect(WIDE, ["solar"], max_fetches=99, session=sc,
                                workers=6)
check("every portal is read", len(items), 24)
check("results keep configuration order",
      [i["state"] for i in items[:4]], ["S0", "S0", "S1", "S1"])
check("fetches really did overlap", sc.peak > 1, True)

serial = OrderScrambler()
items_serial, _ = tenders.collect(WIDE, ["solar"], max_fetches=99,
                                  session=serial, workers=1)
check("workers=1 gives the same answer", items_serial, items)
check("and does not overlap", serial.peak, 1)

# A portal that blows up mid-pool must not take the others with it.
class OneBadApple:
    def get(self, url, **kw):
        if "s7." in url:
            raise ConnectionError("down")
        return types.SimpleNamespace(status_code=200, text=HTML)


items, errors = tenders.collect(WIDE, ["solar"], max_fetches=99,
                                session=OneBadApple(), workers=6)
check("one dead portal costs only itself", len(items), 22)
check("and is named in the errors", any("S7" in e for e in errors), True)

print("\n=== tender vs standing scheme link ===")
# The keyword gate alone cannot tell a tender from a DISCOM menu item: both say
# "solar". On a live first run the menu items outnumbered real tenders 39 to 1.
check("procurement wording is kept",
      tenders.looks_like_tender("Supply and Installation of 500 kWp Rooftop Solar"), True)
check("short scheme link is dropped",
      tenders.looks_like_tender("Net Metering (Solar)"), False)
check("another scheme link is dropped",
      tenders.looks_like_tender("PM Suryaghar Solar Registration"), False)
check("terse but real notice is kept (vocabulary)",
      tenders.looks_like_tender("PPA FOR 10MW SOLAR POWER PROJECT MIZORAM"), True)
check("long title is kept even without the vocabulary",
      tenders.looks_like_tender(
          "Provisional Empanelment of OEMs Solar Module Inverter Battery MMS and "
          "SLS for the state programme"), True)
check("'Solar Related' is not a tender",
      tenders.looks_like_tender("Solar Related"), False)

SCHEME_HTML = ("""<html><body>"""
   """<a href="/a">Net Metering (Solar)</a>"""
   """<a href="/b">PM Suryaghar Solar Registration</a>"""
   """<a href="/c">Supply and Installation of 500 kWp Rooftop Solar Plant at DC office</a>"""
   """</body></html>""")


class SchemeSession:
    def get(self, url, **kw):
        return types.SimpleNamespace(status_code=200, text=SCHEME_HTML)


items, err = tenders.fetch_portal(
    {"label": "DISCOM", "url": "https://d.gov.in/", "kind": "discom"},
    "Assam", match, 5, session=SchemeSession())
check("only the real tender survives the walk", len(items), 1)
check("and it is the right one",
      "Supply and Installation" in items[0]["title"], True)

# Portals emit raw entities inside anchor text; without unescaping, a title
# reaches the report as "&nbsp;&nbsp;Seeking consent for procurement of...".
check("html entities are decoded in link text",
      tenders.clean_text("&nbsp;&nbsp;Supply of 100 kWp Solar &amp; BESS&nbsp;"),
      "Supply of 100 kWp Solar & BESS")

print("\n=== captcha label must not fire on a readable page ===")
# Live bug: AP DISCOM harvests 358 links perfectly well, but carries a login
# captcha in the page furniture. Labelling it "captcha-gated" sent us hunting
# for a wall that was not there.
READABLE_WITH_LOGIN = ("""<html><body>"""
    """<form>Enter Captcha <img id="captchaImage"/></form>"""
    """<a href="/a">Solar Roof Top Registration</a>"""
    """<a href="/b">Solar/PM-Surya Ghar Muft Bijli Yojana</a>"""
    """</body></html>""")


class ReadableSession:
    def get(self, url, **kw):
        return types.SimpleNamespace(status_code=200, text=READABLE_WITH_LOGIN)


items, err = tenders.fetch_portal(
    {"label": "DISCOM", "url": "https://d.gov.in/", "kind": "discom"},
    "Andhra Pradesh", match, 5, session=ReadableSession())
check("a readable page yields nothing but is not called walled", items, [])
check("it is reported as having no tender-like listings",
      err, "no tender-like listings")

# and a genuine wall is still called a wall
items, err = tenders.fetch_portal(
    {"label": "e-tender portal", "url": "https://gep.tenders.gov.in/nicgep/app",
     "kind": "etender"},
    "Odisha", match, 5, session=CaptchaSession())
check("a real wall is still reported as captcha-gated",
      "captcha" in (err or "").lower(), True)

print("\n=== a client-facing digest must not carry these ===")
# Both of these reached a sample client email purely on length.
check("a recruitment notice is not a tender",
      tenders.looks_like_tender(
          "Appointment to the post of Member (Renewable Energy) & Member "
          "(Economic & Commercial) in the Central Electricity Authority, on "
          "deputation basis-inviting application for-regarding"), False)
check("the portal's own masthead link is not a tender",
      tenders.looks_like_tender(
          "Uttar Pradesh UP New & Renewable Energy Development Agency "
          "Department of AdditIonal Sources of Energy, Government of Uttar Pradesh"),
      False)
check("a real long tender is still kept",
      tenders.looks_like_tender(
          "Tender for solarization of grid connected AC Water Supply Pumps "
          "under Jal Jeevan Mission in Tripura on turn-key basis"), True)
check("a real empanelment is still kept",
      tenders.looks_like_tender(
          "Provisional Empanelment of OEMs (Solar Module, Inverter, Battery, "
          "MMS, SLS) under OREDA Ltd."), True)

print("\n=== government notices belong here too ===")
# The section is "Tenders & govt notices". A tariff order or a net-metering
# circular changes what is worth bidding on as much as a fresh tender does, so
# procurement wording is not the only way in.
for _t in ("Public Notice regarding rooftop solar subsidy",
           "Circular on net metering for solar consumers",
           "Tariff Order for Solar Projects FY27",
           "MP Policy for Decentralised Renewable Energy System 2016",
           "Guidelines for implementation of rooftop solar",
           "Regulation for timeline and TFR waiver of Rooftop Solar connection",
           "Amendment to the solar banking notification"):
    check("notice kept: %s" % _t[:38], tenders.looks_like_tender(_t), True)

# ...and the menu items still stay out, because none of them carry those words
for _t in ("Net Metering (Solar)", "PM Suryaghar Solar Registration",
           "Solar Related", "Solar Roof Top Registration",
           "Apply for Rooftop Solar"):
    check("menu item still dropped: %s" % _t[:30], tenders.looks_like_tender(_t), False)

# exclusions still beat the new vocabulary
check("a job advert is still not a notice",
      tenders.looks_like_tender(
          "Appointment to the post of Member (Renewable Energy) - "
          "applications are invited for the post, as per policy"), False)

print("\n=== one tender linked twice on a page is one tender ===")
# Live: MSPDCL lists each tender as a clean heading and again as a row of
# furniture. The portal+title key saw two tenders because the anchor text
# differed; within a single page the URL settles it.
TWICE = ("""<html><body>"""
  """<a href="/t/electrification-off-grid">Electrification of tribal household in """
  """the state of Manipur through off grid mode under New Solar Power Scheme</a>"""
  """<a href="/t/electrification-off-grid">gavel 16 Sep 2025 Closed Electrification """
  """of tribal household in the state of Manipur through off grid mode under New """
  """Solar Power Scheme Tender Reference No: 2/35 dated 12.09.2025 Read more """
  """arrow_forward</a>"""
  """<a href="/t/other">Supply and Installation of 500 kWp Rooftop Solar Plant</a>"""
  """</body></html>""")


class TwiceSession:
    def get(self, url, **kw):
        return types.SimpleNamespace(status_code=200, text=TWICE)


items, err = tenders.fetch_portal(
    {"label": "tender page", "url": "https://mspdcl.in/", "kind": "tender_page"},
    "Manipur", match, 5, session=TwiceSession())
check("the duplicate link collapses", len(items), 2)
check("and the clean heading is the one kept",
      items[0]["title"].startswith("Electrification of tribal household"), True)
check("the furniture version is dropped",
      any("arrow_forward" in i["title"] for i in items), False)
check("the genuinely different tender survives",
      any("500 kWp" in i["title"] for i in items), True)

# across runs the URL must NOT be the key - GePNIC session ids change
_a = {"title": "Supply of 500 kWp solar", "source": "x.gov.in",
      "url": "https://x.gov.in/d?id=1&jsessionid=AAA"}
_b = {"title": "Supply of 500 kWp solar", "source": "x.gov.in",
      "url": "https://x.gov.in/d?id=1&jsessionid=BBB"}
check("a changed session id is still the same tender",
      tenders.tender_key(_a), tenders.tender_key(_b))

print("\n=== Telangana's wall of look-alike notices ===")
import datetime as _dt
_today = _dt.date(2026, 8, 22)

# a dated *event* that has long passed is dead
check("a 2015 bid archive is stale",
      tenders.is_stale_archive("Telangana Solar Bid 2015", _today), True)
check("a 2014 bid archive is stale",
      tenders.is_stale_archive("Telangana Solar Bid 2014", _today), True)
# ...but a dated *policy* is the rule still in force
check("a 2016 policy is not an archive",
      tenders.is_stale_archive(
          "MP Policy For Decentralized Renewable Energy System 2016", _today), False)
check("a 2022 policy is not an archive",
      tenders.is_stale_archive("UP SOLAR ENERGY POLICY-2022 & GO'S", _today), False)
check("a recent tender is left alone",
      tenders.is_stale_archive("Solar Bid 2025", _today), False)
check("an undated tender is left alone",
      tenders.is_stale_archive("Tender for solarization of water pumps", _today), False)

# static reference material is not a notice
for _t in ("Work Flow Chart Under Solar Bidding",
           "Solar Plant Commissioning Certificate",
           "Rooftop Solar Calculator", "Solar FAQs"):
    check("reference page dropped: %s" % _t[:34], tenders.looks_like_tender(_t), False)

# one document mirrored on a state's REDA and DISCOM is one row
_items = [
    {"title": "Solar Power Policy", "state": "Telangana", "url": "a", "source": "reda"},
    {"title": "Telangana State Solar Power Policy", "state": "Telangana",
     "url": "b", "source": "discom"},
    {"title": "Seeking consent for procurement of 1500MWh BESS.",
     "state": "Telangana", "url": "c", "source": "discom"},
]
_kept = tenders.collapse_near_duplicates(_items)
check("the mirrored policy collapses to one", len(_kept), 2)
check("and the first wording is kept", _kept[0]["title"], "Solar Power Policy")
check("the unrelated tender survives",
      any("1500MWh" in i["title"] for i in _kept), True)

# the same wording in a different state is a different document
_two_states = [
    {"title": "Solar Power Policy", "state": "Telangana", "url": "a", "source": "x"},
    {"title": "Solar Power Policy", "state": "Bihar", "url": "b", "source": "y"},
]
check("states do not collapse into each other",
      len(tenders.collapse_near_duplicates(_two_states)), 2)

check("collapsing is on by default, so one state's mirrors merge",
      len(tenders.collect(MIXED, ["solar"], max_fetches=99,
                          session=FakeSession("ok"))[0]), 2)

print("\n" + ("ALL PASS" if OK else "FAILURES ABOVE"))
sys.exit(0 if OK else 1)
