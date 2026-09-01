"""Discover and verify RSS/Atom feeds for the publications in publications.json.

The tracker in script.py only works with sites that expose a real feed, so before
adding a publication to config.json you need to know (a) whether it has a feed at
all and (b) what the URL is. This does both.

For each site it tries, in order:
  1. feed autodiscovery - <link rel="alternate" type="application/rss+xml"> in the
     homepage HTML, which is what the site itself advertises;
  2. a list of conventional feed paths (/feed/, /rss.xml, ...).

A candidate only counts if it parses as a feed AND contains at least one entry, so
a site that serves an empty stub or an HTML error page at /feed/ is reported as a
miss rather than a false positive.

Usage:
    python verify_feeds.py                 # probe everything, write feed_report.md + feeds.json
    python verify_feeds.py --only 1,5,33   # probe just those publication ids
    python verify_feeds.py --merge-config  # append verified feeds to config.json

Needs outbound network access to the publication domains.
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin

import feedparser
import requests

# script.py retries a 403 with Chrome's real TLS fingerprint, which is the only
# thing that gets past Cloudflare's bot management. Without the same fallback
# here, this script reports feeds as dead that the tracker reads perfectly:
# measured on 2 Sep 2026, a plain fetch called 25 of 42 dead while the tracker's
# own fetch path read 29 - PV Tech, Energy Storage News and Solar Builder among
# them. A checker that disagrees with production is worse than no checker.
try:
    from curl_cffi import requests as impersonator
except Exception:
    impersonator = None

UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8",
}

# Ordered by how common they are, so the usual case costs one request.
CANDIDATE_PATHS = [
    "feed/",
    "rss/",
    "feed.xml",
    "rss.xml",
    "atom.xml",
    "index.xml",
    "?feed=rss2",
    "news/feed/",
    "blog/feed/",
    "rss/all.xml",
    "feeds/posts/default?alt=rss",
    "news/rss",
    "rss/news",
    "en/rss",
    "feed/rss",
    "rss/feed",
]

TIMEOUT = 25


def fetch(url):
    """Fetch a URL exactly the way script.py does, impersonation fallback included."""
    r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    if r.status_code == 403 and impersonator is not None:
        try:
            r2 = impersonator.get(url, impersonate="chrome", timeout=TIMEOUT)
            if r2.status_code < 400:
                return r2
        except Exception:
            pass                              # keep the original 403
    return r


def validate(url):
    """Return feed details if `url` serves a parseable feed with >=1 entry, else None."""
    try:
        r = fetch(url)
    except Exception:
        return None
    if r.status_code >= 400:
        return None

    body = r.content
    head = body[:600].lstrip().lower()
    looks_xml = (
        head.startswith(b"<?xml")
        or b"<rss" in head
        or b"<feed" in head
        or b"<rdf" in head
    )
    is_json_feed = "json" in r.headers.get("content-type", "").lower() and b'"items"' in body[:2000]
    if not (looks_xml or is_json_feed):
        return None

    parsed = feedparser.parse(body)
    if not parsed.entries:
        return None

    first = parsed.entries[0]
    return {
        "url": r.url,
        "format": parsed.version or "unknown",
        "entries": len(parsed.entries),
        "feed_title": (parsed.feed.get("title") or "").strip()[:100],
        "latest_item": (first.get("title") or "").strip()[:100],
        "latest_date": (first.get("published") or first.get("updated") or "").strip(),
    }


def autodiscover(base):
    """Feed URLs the homepage advertises via <link rel="alternate">."""
    try:
        r = fetch(base)
    except Exception as exc:
        return [], f"homepage unreachable ({type(exc).__name__})"
    if r.status_code >= 400:
        return [], f"homepage HTTP {r.status_code}"

    found = []
    for tag in re.findall(r"<link\b[^>]*>", r.text, re.I):
        if "alternate" not in tag.lower():
            continue
        if not re.search(r"application/(rss|atom)\+xml|application/feed\+json", tag, re.I):
            continue
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if href:
            found.append(urljoin(r.url, href.group(1)))

    ordered, seen = [], set()
    for u in found:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    # Comment feeds are advertised alongside the real one but are never what we want.
    ordered.sort(key=lambda u: ("comment" in u.lower(), len(u)))
    return ordered, f"homepage HTTP {r.status_code}"


def probe(pub):
    result = {
        "id": pub["id"],
        "name": pub["name"],
        "focus": pub.get("focus", ""),
        "site": pub["site"],
        "feed": None,
        "method": None,
        "status": "",
    }
    if pub.get("note"):
        result["note"] = pub["note"]

    tried = set()
    discovered, status = autodiscover(pub["site"])
    result["status"] = status

    for url in discovered[:6]:
        tried.add(url)
        found = validate(url)
        if found:
            result["feed"], result["method"] = found, "autodiscovery"
            break

    if not result["feed"]:
        for path in CANDIDATE_PATHS:
            url = urljoin(pub["site"], path)
            if url in tried:
                continue
            tried.add(url)
            found = validate(url)
            if found:
                result["feed"], result["method"] = found, "conventional path"
                break

    result["candidates_tried"] = len(tried)
    mark = "OK  " if result["feed"] else "MISS"
    detail = result["feed"]["url"] if result["feed"] else result["status"]
    print(f"[{result['id']:>2}] {mark} {result['name'][:36]:<36} {detail}", flush=True)
    return result


def write_report(results, path):
    ok = [r for r in results if r["feed"]]
    missing = [r for r in results if not r["feed"]]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Feed availability report",
        "",
        f"Generated {generated} by `verify_feeds.py`.",
        "",
        f"**{len(ok)} of {len(results)} publications expose a usable feed.**",
        "",
        "A publication counts as having a feed only if the URL parses as RSS/Atom *and*",
        "carries at least one item.",
        "",
        "## Feeds found",
        "",
        "| # | Publication | Feed URL | Format | Items | Found via |",
        "|---|---|---|---|---|---|",
    ]
    for r in ok:
        f = r["feed"]
        lines.append(
            f"| {r['id']} | {r['name']} | {f['url']} | {f['format']} | "
            f"{f['entries']} | {r['method']} |"
        )

    lines += ["", "## No feed found", "", "| # | Publication | Site | Candidates tried | Status |", "|---|---|---|---|---|"]
    for r in missing:
        lines.append(
            f"| {r['id']} | {r['name']} | {r['site']} | {r['candidates_tried']} | {r['status']} |"
        )

    notes = [r for r in results if r.get("note")]
    if notes:
        lines += ["", "## Source-list issues", ""]
        for r in notes:
            lines.append(f"- **{r['name']}** (#{r['id']}): {r['note']}")

    lines += [
        "",
        "## Re-running",
        "",
        "```bash",
        "pip install -r requirements.txt",
        "python verify_feeds.py",
        "python verify_feeds.py --merge-config   # add the verified feeds to config.json",
        "```",
        "",
        "A miss is not always permanent: some sites block datacenter IPs or rate-limit,",
        "so a publication that fails here may still work from another network.",
        "",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def merge_config(results, path="config.json"):
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)

    existing = {s["rss"].rstrip("/") for s in config["sites"]}
    added = []
    for r in results:
        if not r["feed"]:
            continue
        url = r["feed"]["url"]
        if url.rstrip("/") in existing:
            continue
        config["sites"].append({"name": r["name"], "rss": url})
        existing.add(url.rstrip("/"))
        added.append(r["name"])

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\n[CONFIG] Added {len(added)} feed(s) to {path}")
    for name in added:
        print(f"         + {name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publications", default="publications.json")
    ap.add_argument("--only", help="comma-separated publication ids to probe")
    ap.add_argument("--url", help="probe a single site URL not in publications.json")
    ap.add_argument("--name", help="name to use with --url (defaults to the domain)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--report", default="feed_report.md")
    ap.add_argument("--json-out", default="feeds.json")
    ap.add_argument("--merge-config", action="store_true", help="append verified feeds to config.json")
    args = ap.parse_args()

    if args.url:
        # Ad-hoc check for a site someone wants to add, without editing
        # publications.json first.
        from urllib.parse import urlparse as _up
        site = args.url if args.url.startswith("http") else "https://" + args.url
        pubs = [{
            "id": 0,
            "name": args.name or _up(site).netloc.replace("www.", ""),
            "focus": "ad-hoc",
            "site": site if site.endswith("/") else site + "/",
        }]
    else:
        with open(args.publications, encoding="utf-8") as fh:
            pubs = json.load(fh)["publications"]

    if args.only and not args.url:
        wanted = {int(x) for x in args.only.split(",")}
        pubs = [p for p in pubs if p["id"] in wanted]

    print(f"[PROBE] {len(pubs)} publications, {args.workers} workers\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = sorted(pool.map(probe, pubs), key=lambda r: r["id"])

    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    write_report(results, args.report)

    ok = sum(1 for r in results if r["feed"])
    print(f"\n=== {ok}/{len(results)} have a usable feed ===")
    print(f"[WROTE] {args.report}, {args.json_out}")

    if args.merge_config:
        merge_config(results)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
