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
                                session=FakeSession("ok"))
check("collect walks every portal", len(items), 6)
check("collect reports no errors on success", errors, [])

items, errors = tenders.collect(SOURCES, ["solar"], max_fetches=2,
                                session=FakeSession("ok"))
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
                                session=FakeSession("ok"))
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

print("\n" + ("ALL PASS" if OK else "FAILURES ABOVE"))
sys.exit(0 if OK else 1)
