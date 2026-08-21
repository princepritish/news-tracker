"""Offline tests for tenders.py (Phase 2).

No network: portal HTML is supplied inline. Covers keyword matching, link
harvesting, the identity key, and the failure paths that must never break a run.

    python test_tenders.py
"""

import sys
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

print("\n" + ("ALL PASS" if OK else "FAILURES ABOVE"))
sys.exit(0 if OK else 1)
