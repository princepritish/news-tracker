"""Check which tender portals are reachable, and capture their HTML.

The tender reader deliberately uses generic link harvesting because no one has
seen these 50+ government pages yet. This script closes that gap: it reports what
each portal returns and saves the HTML, so parsers can be written against real
markup instead of assumptions.

    python probe_tenders.py                 # probe everything, save HTML
    python probe_tenders.py --state Bihar   # just one state
    python probe_tenders.py --no-save       # report only

Writes portal_report.md and, unless --no-save, one .html per portal in
portal_html/. Run it from a normal network - government portals frequently block
datacentre IP ranges, so a hosted run can report failures that a laptop does not.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

import tenders

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

DEFAULT_KEYWORDS = ["solar", "spv", "rooftop solar", "pv", "solar pump", "epc",
                    "photovoltaic", "renewable", "battery", "bess"]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def probe(job):
    state, portal, save_dir, timeout = job
    match = tenders.build_matcher(DEFAULT_KEYWORDS)
    urls = tenders.gepnic_candidates(portal["url"]) + [portal["url"]]

    result = {"state": state, "label": portal["label"], "url": portal["url"],
              "status": None, "links": 0, "matched": 0, "final_url": None,
              "error": None, "saved": None, "samples": []}

    for url in urls:
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
        except Exception as e:
            result["error"] = f"{type(e).__name__}"
            continue

        result["status"] = r.status_code
        result["final_url"] = r.url
        if r.status_code >= 400:
            result["error"] = f"HTTP {r.status_code}"
            continue

        links = tenders.harvest_links(r.text, r.url)
        hits = [t for t, _ in links if match(t)]
        result.update(links=len(links), matched=len(hits), error=None)
        result["samples"] = hits[:3]

        if save_dir:
            name = f"{slug(state)}--{slug(portal['label'])}.html"
            path = os.path.join(save_dir, name)
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(r.text)
            result["saved"] = name

        if hits:
            break      # this URL works; no need to try the fallbacks

    mark = "OK  " if result["matched"] else ("--  " if not result["error"] else "ERR ")
    print(f"[{mark}] {state[:14]:<14} {portal['label'][:16]:<16} "
          f"links={result['links']:<4} matched={result['matched']:<3} "
          f"{result['error'] or urlparse(result['final_url'] or '').netloc}",
          flush=True)
    return result


def write_report(results, path):
    works = [r for r in results if r["matched"]]
    reachable = [r for r in results if not r["error"] and not r["matched"]]
    broken = [r for r in results if r["error"]]

    out = [
        "# Tender portal probe",
        "",
        f"- **{len(works)}** portals returned matching tenders",
        f"- **{len(reachable)}** reachable but nothing matched "
        "(may need a search POST, or genuinely has no solar work listed)",
        f"- **{len(broken)}** unreachable",
        "",
        "## Working",
        "",
        "| State | Portal | Links | Matched | Example |",
        "|---|---|---|---|---|",
    ]
    for r in works:
        example = (r["samples"][0][:70] + "…") if r["samples"] else ""
        out.append(f"| {r['state']} | {r['label']} | {r['links']} | "
                   f"{r['matched']} | {example} |")

    out += ["", "## Reachable, nothing matched", "",
            "| State | Portal | Links found | Final URL |", "|---|---|---|---|"]
    for r in reachable:
        out.append(f"| {r['state']} | {r['label']} | {r['links']} | {r['final_url']} |")

    out += ["", "## Unreachable", "", "| State | Portal | Error | URL |",
            "|---|---|---|---|"]
    for r in broken:
        out.append(f"| {r['state']} | {r['label']} | {r['error']} | {r['url']} |")

    out += ["", "## Next step", "",
            "For portals in the middle table, open the saved HTML in `portal_html/`.",
            "A high link count with zero matches usually means the landing page is a",
            "menu and the listing sits behind a search form, which needs a portal",
            "specific adapter.", ""]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources.json")
    ap.add_argument("--state")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--save-dir", default="portal_html")
    ap.add_argument("--report", default="portal_report.md")
    args = ap.parse_args()

    sources = tenders.load_sources(args.sources)
    if args.state:
        sources = [s for s in sources if s["state"].lower() == args.state.lower()]
    if not sources:
        print("No matching sources")
        return 1

    save_dir = None if args.no_save else args.save_dir
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)

    jobs = [(s["state"], p, save_dir, args.timeout)
            for s in sources for p in s["portals"]
            if p["kind"] in ("etender", "tender_page")]

    print(f"[PROBE] {len(jobs)} portals across {len(sources)} states\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(probe, jobs))

    write_report(results, args.report)
    working = sum(1 for r in results if r["matched"])
    print(f"\n=== {working}/{len(results)} portals returned matching tenders ===")
    print(f"[WROTE] {args.report}" + (f", {save_dir}/" if save_dir else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
