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

Isolation rules, since this must never damage the news digest:
  * every portal is wrapped individually - one failure is recorded, not raised
  * a global cap bounds total fetches per run
  * no LLM calls at all; matching is plain string work, so this adds nothing to
    the Groq budget
"""

import json
import re
from urllib.parse import urljoin, urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; SolarTenderWatch/1.0)"}

# Tender listings are link text, which is short - so an anchor-text match is a
# much stronger signal than the same word appearing in article prose.
LINK_RE = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                     re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# Junk that appears as links on every government portal.
NAV_NOISE = {
    "home", "login", "contact us", "help", "sitemap", "search", "back",
    "downloads", "faq", "disclaimer", "privacy policy", "terms of use",
    "skip to main content", "screen reader access", "register", "next",
    "previous", "first", "last", "view all", "more", "click here", "english",
    "hindi", "accessibility", "feedback", "archive", "tenders", "all tenders",
}


def clean_text(html_fragment):
    return WS_RE.sub(" ", TAG_RE.sub(" ", html_fragment)).strip()


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
            patterns.append(re.compile(rf"\b{re.escape(kw)}\b"))
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
    root = f"{parsed.scheme}://{parsed.netloc}"
    app = "/nicgep/app" if "nicgep" in url or parsed.netloc.endswith("tenders.gov.in") else None
    if not app:
        return []
    return [
        f"{root}{app}?page=FrontEndLatestActiveTenders&service=page",
        f"{root}{app}?page=FrontEndAdvancedSearch&service=page",
    ]


def fetch_portal(portal, state, matches, timeout, session=None):
    """Read one portal. Returns (items, error_string_or_None) - never raises."""
    get = (session or requests).get
    urls = gepnic_candidates(portal["url"]) + [portal["url"]]
    last_error = None

    for url in urls:
        try:
            r = get(url, headers=UA, timeout=timeout)
            if r.status_code >= 400:
                last_error = f"HTTP {r.status_code}"
                continue

            html = r.text
            hits = [
                {
                    "title": text,
                    "url": link,
                    "state": state,
                    "portal": portal["label"],
                    "source": urlparse(url).netloc,
                }
                for text, link in harvest_links(html, url)
                if matches(text)
            ]
            if hits:
                return hits, None
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
    return f"{item['source']}|{title}"


def collect(sources, keywords, max_fetches, timeout=25, session=None,
            portal_kinds=("etender", "tender_page")):
    """Walk every configured portal within the fetch budget.

    Returns (items, errors). Only e-tender and dedicated tender pages are read by
    default - agency and DISCOM home pages are mostly navigation and rarely worth
    a request.
    """
    matches = build_matcher(keywords)
    items, errors, fetches = [], [], 0

    for source in sources:
        for portal in source["portals"]:
            if portal["kind"] not in portal_kinds:
                continue
            if fetches >= max_fetches:
                errors.append(f"{source['state']}: fetch budget reached")
                return items, errors

            fetches += 1
            found, error = fetch_portal(portal, source["state"], matches,
                                        timeout, session)
            if error and not found:
                errors.append(f"{source['state']} {portal['label']} ({error})")
            items.extend(found)

    return items, errors


def load_sources(path="sources.json"):
    try:
        with open(path) as f:
            return json.load(f)["sources"]
    except Exception as e:
        print(f"[TENDERS] Could not load {path}: {e}")
        return []
