"""Check the feeds actually configured in config.json, and repair the broken ones.

verify_feeds.py answers a different question: which of the 42 *candidate*
publications expose a feed at all. This one takes the 43 feeds the tracker really
reads and, for each, either confirms it or goes looking for the working URL on the
same domain - autodiscovery first, then the conventional paths - so a feed that
merely moved is fixed instead of dropped.

    python check_config_feeds.py                # report only
    python check_config_feeds.py --write        # apply repairs + verified flags
    python check_config_feeds.py --drop-dead    # also remove feeds beyond repair

A feed counts as working only if it parses AND carries at least one entry, the
same bar verify_feeds.py uses, so an HTML error page served at /feed/ is a miss.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import verify_feeds as vf


def site_root(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def check(site):
    """Return the site dict annotated with what we found."""
    out = dict(site)
    out["_found"] = None
    out["_how"] = None

    current = vf.validate(site["rss"])
    if current:
        out["_found"], out["_how"] = current, "configured URL"
    else:
        # The configured URL is dead. Look for a working one on the same domain.
        root = site_root(site["rss"])
        tried = set()
        discovered, status = vf.autodiscover(root)
        out["_status"] = status
        for url in discovered[:6]:
            tried.add(url)
            found = vf.validate(url)
            if found:
                out["_found"], out["_how"] = found, "autodiscovery"
                break
        if not out["_found"]:
            for path in vf.CANDIDATE_PATHS:
                url = urljoin(root, path)
                if url in tried or url == site["rss"]:
                    continue
                tried.add(url)
                found = vf.validate(url)
                if found:
                    out["_found"], out["_how"] = found, "conventional path"
                    break
        out["_tried"] = len(tried)

    if out["_found"]:
        mark = "OK  " if out["_how"] == "configured URL" else "FIX "
        detail = out["_found"]["url"]
    else:
        mark = "DEAD"
        detail = out.get("_status", "no feed found")
    print(f"{mark} {site['name'][:38]:<38} {detail}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--report", default="config_feed_report.md")
    ap.add_argument("--write", action="store_true",
                    help="write repaired URLs and verified flags back to config.json")
    ap.add_argument("--drop-dead", action="store_true",
                    help="with --write, remove feeds that could not be repaired")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    sites = config["sites"]
    print(f"Checking {len(sites)} configured feeds\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(check, sites))

    good = [r for r in results if r["_how"] == "configured URL"]
    fixed = [r for r in results if r["_how"] and r["_how"] != "configured URL"]
    dead = [r for r in results if not r["_found"]]

    print(f"\n{len(good)} already correct · {len(fixed)} repaired · {len(dead)} dead")

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Configured feed report", "",
        f"Generated {generated} by `check_config_feeds.py`.", "",
        f"**{len(good) + len(fixed)} of {len(results)} configured feeds are usable** "
        f"({len(good)} as configured, {len(fixed)} after repair).", "",
        "## Working as configured", "",
        "| Feed | URL | Items | Latest item |", "|---|---|---|---|",
    ]
    for r in good:
        f_ = r["_found"]
        lines.append(f"| {r['name']} | {f_['url']} | {f_['entries']} | {f_['latest_item']} |")

    lines += ["", "## Repaired (URL changed)", "",
              "| Feed | Old URL | Working URL | Found via | Items |", "|---|---|---|---|---|"]
    for r in fixed:
        f_ = r["_found"]
        lines.append(f"| {r['name']} | {r['rss']} | {f_['url']} | {r['_how']} | {f_['entries']} |")

    lines += ["", "## No working feed", "",
              "| Feed | Configured URL | Candidates tried | Status |", "|---|---|---|---|"]
    for r in dead:
        lines.append(f"| {r['name']} | {r['rss']} | {r.get('_tried', 0)} | "
                     f"{r.get('_status', 'n/a')} |")

    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {args.report}")

    if not args.write:
        print("(report only - pass --write to update config.json)")
        return

    new_sites = []
    for r in results:
        entry = {k: v for k, v in r.items() if not k.startswith("_")}
        if r["_found"]:
            entry["rss"] = r["_found"]["url"]
            entry["verified"] = True
        else:
            if args.drop_dead:
                continue
            entry["verified"] = False
        new_sites.append(entry)

    config["sites"] = new_sites
    with open(args.config, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Updated {args.config}: {len(new_sites)} feeds "
          f"({sum(1 for s in new_sites if s.get('verified'))} verified)")


if __name__ == "__main__":
    main()
