"""Phase 2 — government tender and announcement monitoring.

Watches the state e-tender portals, renewable-energy agencies and DISCOMs listed
in sources.json for newly published solar work.

Why this does not parse each portal precisely
---------------------------------------------
These are 50+ government sites on several different platforms, and the ones on
NIC GePNIC still differ in skin and markup. A parser written per site is a
maintenance burden that breaks silently the first time a page is restyled.

Instead every portal is read the same way: harvest every link with its anchor
text, then keep the links whose text matches a tender keyword. Tender titles on
these portals live in link text, so this survives layout changes, works on
platforms nobody has adapted yet, and degrades to "found nothing" rather than to
a crash. Precision can be added per portal later using probe_tenders.py output.

What the portals actually turned out to be
------------------------------------------
Probed for real on 22 Aug 2026; see portal_report.md. The GePNIC e-tender
portals — every state one in sources.json — put their whole public tender list
behind a captcha, on every listing page alike: Active Tenders, Tenders by
Closing Date, by Organisation, by Location, by Classification, and the national
CPPP aggregator too. No amount of link harvesting reaches them, so they are
reported as "captcha-gated" rather than as an empty portal, and the remaining
candidate URLs for that host are abandoned instead of costing more requests.

The agency and DISCOM sites are where the readable tenders actually are, which
is why they are read by default. Their pages also carry standing links to
consumer schemes ("Apply for Rooftop Solar"); those match the keywords once,
are reported once, and are then suppressed forever by the portal+title key.

Isolation rules, since this must never damage the news digest:
  * every portal is wrapped individually - one failure is recorded, not raised
  * a global cap bounds total fetches per run
  * no LLM calls at all; matching is plain string work, so this adds nothing to
    the Groq budget
"""

import datetime
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

# The verify=False fallback below is deliberate, so urllib3 warning about it
# once per portal per run is pure noise in the log - and the log is where the
# only remaining health signal lives.
try:
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (compatible; SolarTenderWatch/1.0)"}

# Tender listings are link text, which is short - so an anchor-text match is a
# much stronger signal than the same word appearing in article prose.
LINK_RE = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                     re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
REFRESH_RE = re.compile(r"""http-equiv\s*=\s*["']?\s*refresh""", re.I)
CONTENT_RE = re.compile(r"""content\s*=\s*["']([^"']*)["']""", re.I)
CONTENT_URL_RE = re.compile(r"""url\s*=\s*["']?([^"'\s]+)""", re.I)

# GePNIC renders its captcha inline on the listing page itself, so its presence
# in the markup is what separates "walled off" from "nothing listed today".
CAPTCHA_RE = re.compile(r"captcha", re.I)

# Agency and DISCOM sites carry the tenders that are actually readable. The
# e-tender portals stay in the walk so their captcha wall keeps showing up in
# the health footer instead of being quietly forgotten.
DEFAULT_PORTAL_KINDS = ("etender", "tender_page", "agency", "discom")

# Portals are independent and nearly all the time is spent waiting on them, so
# they are fetched in parallel. Serially, 56 portals against a 25s timeout is a
# worst case of over twenty minutes - on a metered host that is the whole cost
# of the run.
DEFAULT_WORKERS = 8

# A keyword match is necessary but not sufficient. DISCOM and agency sites carry
# standing links to consumer schemes - "Net Metering (Solar)", "PM Suryaghar
# Solar Registration", "Name Change for Solar" - which match the tender keywords
# exactly as well as a tender does. On a live first run they outnumbered genuine
# tenders 39 to 1 and filled the whole report.
#
# What separates them is wording and length: procurement notices name the act of
# procuring, and their titles are long (median 250 characters against 27 for the
# scheme links). Either signal is enough, so a terse "PPA for 10MW solar" is kept
# while "Solar Related" is not.
# Procurement wording.
TENDER_VOCAB_RE = re.compile(
    r"\b(tenders?|bids?|bidding|rfq|rfp|eoi|nit|quotations?|empanel\w*|supply|"
    r"installation|erection|commissioning|construction|procurement|contract|"
    r"auction|corrigend\w*|ppa|invit\w+|expression of interest)\b", re.I)

# The section is "Tenders & govt notices", and the notices matter as much as the
# tenders: a tariff order, a net-metering circular or a policy amendment changes
# what is worth bidding on. These sit alongside the procurement words rather
# than replacing them, and the exclusions below still strip menu items, job ads
# and mastheads - "Net Metering (Solar)" carries none of these words.
NOTICE_VOCAB_RE = re.compile(
    r"\b(notice|notification|circular|office memorandum|"
    r"tariff order|order no|policy|guidelines?|regulations?|amendment|"
    r"addendum|advisory|public consultation|discussion paper)\b", re.I)

MIN_TENDER_TITLE = 60

# A dated procurement event that has passed is dead; a dated policy is not.
# "Telangana Solar Bid 2015" is an archive, while "MP Policy for Decentralised
# Renewable Energy System 2016" is the rule still in force. So a year in the
# title only disqualifies something that is also an *event* - a bid, tender or
# auction. Anything within the last couple of years is left alone, because a
# recent notice may still be open.
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
DATED_EVENT_RE = re.compile(r"\b(bids?|bidding|tenders?|auctions?|rfp|rfq|eoi)\b", re.I)
STALE_AFTER_YEARS = int(os.environ.get("STALE_AFTER_YEARS", "2"))


def is_stale_archive(text, today=None):
    """True for a procurement event whose title carries a long-past year."""
    if not DATED_EVENT_RE.search(text):
        return False
    years = [int(m.group(0)) for m in YEAR_RE.finditer(text)]
    if not years:
        return False
    this_year = (today or datetime.date.today()).year
    return max(years) <= this_year - STALE_AFTER_YEARS


# Static reference material that lives permanently in a portal's menu. It reads
# like procurement because it is *about* procurement - a workflow chart for
# bidding, the certificate you file after commissioning - but nothing is being
# invited, so it is not a notice.
REFERENCE_PAGE_RE = re.compile(
    r"\b(work ?flow|flow chart|certificate|calculator|brochure|"
    r"proforma|format|check ?list|user manual|help ?file|faqs?|"
    r"list of empanelled|vendor list|price list|rate list)\b", re.I)


def _signature(title, state):
    """Significant words of a title, ignoring the state's own name.

    Telangana's REDA and DISCOM both link the state solar policy, as "Solar
    Power Policy" and "Telangana State Solar Power Policy". Those are one
    document, and a reader seeing both in one digest reads it as noise.
    """
    flat = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    drop = set(re.sub(r"[^a-z0-9 ]+", " ", (state or "").lower()).split())
    drop.update(("the", "and", "for", "under", "with", "from", "state"))
    return frozenset(w for w in flat.split() if len(w) > 3 and w not in drop)


def collapse_near_duplicates(items, threshold=0.7):
    """One entry per document per state.

    A tender is keyed on portal+title on purpose - two portals can genuinely run
    separate tenders with the same wording. A *notice* is the opposite: the same
    policy or circular gets mirrored across a state's REDA and DISCOM sites, and
    keying on the portal turns one document into several rows. Comparing the
    significant words within a state catches that without touching tenders that
    merely share a phrase. First seen wins.
    """
    kept = []
    signatures = []
    for item in items:
        sig = _signature(item["title"], item.get("state"))
        if not sig:
            kept.append(item)
            continue
        dup = False
        for prev_state, prev in signatures:
            if prev_state != item.get("state"):
                continue
            overlap = len(sig & prev) / float(len(sig | prev))
            if overlap >= threshold:
                dup = True
                break
        if not dup:
            signatures.append((item.get("state"), sig))
            kept.append(item)
    return kept


# Length alone lets two kinds of long non-tender through, and both looked bad in
# a client-facing digest: recruitment notices ("Appointment to the post of
# Member (Renewable Energy)...") and the site's own masthead link ("Uttar Pradesh
# New & Renewable Energy Development Agency, Department of ... Government of
# Uttar Pradesh"). Neither is procurement.
NOT_A_TENDER_RE = re.compile(
    r"\b(appointment to the post|recruitment|vacanc\w+|walk[- ]in interview|"
    r"applications? are invited for the post|constituent organisation|"
    r"department of additional sources)\b", re.I)

# A masthead link names the body and its parent government, and nothing else.
ORG_NAME_RE = re.compile(
    r"(development agency|regulatory commission|nigam|corporation)\b[^.]{0,80}"
    r"\b(government of|govt\.? of|department of)\b", re.I)



def looks_like_tender(text):
    """True if the anchor text reads like a tender or a government notice.

    Both belong in the digest: the section is "Tenders & govt notices", and a
    tariff order or a net-metering circular changes what is worth bidding on
    just as much as a fresh tender does.
    """
    if (NOT_A_TENDER_RE.search(text) or ORG_NAME_RE.search(text)
            or REFERENCE_PAGE_RE.search(text) or is_stale_archive(text)):
        return False
    return (bool(TENDER_VOCAB_RE.search(text))
            or bool(NOTICE_VOCAB_RE.search(text))
            or len(text) >= MIN_TENDER_TITLE)


# Junk that appears as links on every government portal.
NAV_NOISE = {
    "home", "login", "contact us", "help", "sitemap", "search", "back",
    "downloads", "faq", "disclaimer", "privacy policy", "terms of use",
    "skip to main content", "screen reader access", "register", "next",
    "previous", "first", "last", "view all", "more", "click here", "english",
    "hindi", "accessibility", "feedback", "archive", "tenders", "all tenders",
}


def clean_text(html_fragment):
    """Anchor text with tags stripped and entities decoded.

    Portals emit raw entities inside link text, so without unescaping a title
    reaches the report as "&nbsp;&nbsp;Seeking consent for procurement".
    """
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", html_fragment))).strip()


def meta_refresh_target(html, base_url):
    """The URL a <meta http-equiv="refresh"> stub points at, or None.

    requests follows HTTP redirects but not this one, and several GePNIC
    portals serve nothing else - a two-line page that harvests as zero links
    and is indistinguishable from a portal with nothing on it.
    """
    for tag in META_TAG_RE.findall(html or ""):
        if not REFRESH_RE.search(tag):
            continue
        content = CONTENT_RE.search(tag)
        if not content:
            continue
        target = CONTENT_URL_RE.search(content.group(1))
        if target:
            return urljoin(base_url, target.group(1))
    return None


def build_matcher(keywords):
    """Return a predicate matching any keyword in text.

    Short keywords are matched on word boundaries: 'pv' must not fire on 'PVC
    pipe', and 'epc' must not fire inside a longer token. Longer phrases are
    matched as substrings so 'rooftop solar' still catches 'rooftop solar plant'.
    """
    patterns = []
    for raw in keywords:
        kw = raw.lower().strip()
        if not kw:
            continue
        if len(kw) <= 4 and " " not in kw:
            patterns.append(re.compile(r"\b" + re.escape(kw) + r"\b"))
        else:
            patterns.append(re.compile(re.escape(kw)))

    def matches(text):
        low = text.lower()
        return any(p.search(low) for p in patterns)

    return matches


def harvest_links(html, base_url):
    """Every (text, absolute_url) pair on the page, minus obvious navigation."""
    out = []
    for href, inner in LINK_RE.findall(html):
        text = clean_text(inner)
        if not text or len(text) < 12:          # tender titles are never this short
            continue
        if text.lower() in NAV_NOISE:
            continue
        if href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        out.append((text[:300], urljoin(base_url, href)))
    return out


def gepnic_candidates(url):
    """Deep links to the 'latest active tenders' list on NIC GePNIC portals.

    GePNIC's landing page is a menu; the tender list lives one page in. Trying
    these first means a portal that follows the convention yields real listings
    instead of navigation.
    """
    parsed = urlparse(url)
    root = "%s://%s" % (parsed.scheme, parsed.netloc)
    app = "/nicgep/app" if "nicgep" in url or parsed.netloc.endswith("tenders.gov.in") else None
    if not app:
        return []
    return [
        "%s%s?page=FrontEndLatestActiveTenders&service=page" % (root, app),
        "%s%s?page=FrontEndAdvancedSearch&service=page" % (root, app),
    ]


# Several state portals present an expired or incomplete certificate chain and
# fail the handshake outright. They serve public tender listings and we send no
# credentials to them, so rather than write them off they are retried once with
# verification disabled - which recovers Andhra Pradesh REDA, Madhya Pradesh
# DISCOM, Sikkim DISCOM and the Bihar e-tender portal. Verified HTTPS is always
# tried first, and this never applies to anything but these public pages.
TLS_FAILURES = ("SSLError", "ConnectionError")


def fetch_portal(portal, state, matches, timeout, session=None):
    """Read one portal. Returns (items, error_string_or_None) - never raises."""
    raw_get = (session or requests).get

    def get(url, **kw):
        try:
            return raw_get(url, **kw)
        except Exception as e:
            if type(e).__name__ not in TLS_FAILURES or kw.get("verify") is False:
                raise
            return raw_get(url, verify=False, **kw)
    urls = gepnic_candidates(portal["url"]) + [portal["url"]]
    last_error = None
    followed = set()

    while urls:
        url = urls.pop(0)
        try:
            r = get(url, headers=UA, timeout=timeout)
            if r.status_code >= 400:
                last_error = "HTTP %s" % r.status_code
                continue

            html = r.text or ""

            # A meta-refresh stub carries no links at all. Follow it once per
            # target so a redirecting portal gets read rather than written off.
            hop = meta_refresh_target(html, url)
            if hop and hop not in followed:
                followed.add(hop)
                urls.insert(0, hop)
                last_error = "meta-refresh not followed"
                continue

            on_topic = [(text, link) for text, link in harvest_links(html, url)
                        if matches(text)]
            # One tender is often linked twice on the same page - a clean
            # heading and a row of furniture ("gavel 16 Sep 2025 Closed ...
            # Read more arrow_forward"). The portal+title key treats those as
            # two tenders because the anchor text differs, so collapse them
            # here, on the URL, while we are still inside a single page and the
            # URL is therefore meaningful. Across runs the URL is not safe to
            # key on - GePNIC session parameters change - which is why this
            # stays local to one fetch. First wins: the heading comes first.
            hits, seen_here = [], set()
            for text, link in on_topic:
                if not looks_like_tender(text) or link in seen_here:
                    continue
                seen_here.add(link)
                hits.append({
                    "title": text,
                    "url": link,
                    "state": state,
                    "portal": portal["label"],
                    "source": urlparse(url).netloc,
                })
            if hits:
                return hits, None

            # Distinguish three different nothings, because they need different
            # responses. A page that yielded on-topic links but no procurement
            # wording was read fine and simply has no tenders today - saying
            # "captcha-gated" there sends you hunting for a wall that isn't
            # there. Several DISCOM sites carry a login captcha in the page
            # furniture while their content reads perfectly.
            if on_topic:
                last_error = "no tender-like listings"
            elif CAPTCHA_RE.search(html):
                # A real wall: nothing on-topic came back and a captcha guards
                # the list. No point spending the remaining candidate requests.
                return [], "captcha-gated, needs a browser"
            else:
                last_error = "no matching tenders"
        except Exception as e:
            last_error = type(e).__name__

    return [], last_error


def tender_key(item):
    """Stable identity for a tender.

    Deliberately not the URL: GePNIC detail links carry session parameters that
    change between visits, so URL-based dedup would re-report the same tender
    every single day. Portal plus normalised title is stable.
    """
    title = WS_RE.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", item["title"].lower())).strip()
    return "%s|%s" % (item["source"], title)


def collect(sources, keywords, max_fetches, timeout=25, session=None,
            portal_kinds=DEFAULT_PORTAL_KINDS, workers=DEFAULT_WORKERS,
            collapse=True):
    """Walk every configured portal within the fetch budget.

    Returns (items, errors). Portals are fetched concurrently but chosen and
    reported in configuration order, so the digest reads the same way whatever
    order the network happens to answer in.
    """
    matches = build_matcher(keywords)
    jobs, errors, budget_hit = [], [], None

    for source in sources:
        for portal in source["portals"]:
            if portal["kind"] not in portal_kinds:
                continue
            if len(jobs) >= max_fetches:
                if budget_hit is None:
                    budget_hit = source["state"]
                continue
            jobs.append((source["state"], portal))

    if budget_hit is not None:
        errors.append("%s: fetch budget reached" % budget_hit)

    if not jobs:
        return [], errors

    def run(job):
        state, portal = job
        try:
            return fetch_portal(portal, state, matches, timeout, session)
        except Exception as e:                      # belt and braces
            return [], type(e).__name__

    if workers and workers > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            results = list(pool.map(run, jobs))
    else:
        results = [run(job) for job in jobs]

    items = []
    for (state, portal), (found, error) in zip(jobs, results):
        if error and not found:
            errors.append("%s %s (%s)" % (state, portal["label"], error))
        items.extend(found)

    return (collapse_near_duplicates(items) if collapse else items), errors


def load_sources(path="sources.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["sources"]
    except Exception as e:
        print("[TENDERS] Could not load %s: %s" % (path, e))
        return []
