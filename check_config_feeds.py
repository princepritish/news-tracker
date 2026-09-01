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

Failing that bar is not the same as being broken, and the report keeps the three
apart, because only one of them is a repair job:

  * **dead**      - no feed at the URL and none found on the domain. Fix the URL.
  * **empty**     - a well-formed feed carrying no items. The link is correct;
                    a publisher between issues or an event site looks like this
                    and may carry items tomorrow. Nothing to fix.
  * **rate-limited** - HTTP 429. Not a verdict on the feed at all; re-run later.

Only *dead* feeds get their URL rewritten or their `verified` flag cleared.
"""

import argparse
import json

import feedparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import verify_feeds as vf


def site_root(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def rate_limited(url):
    """True if the host is currently refusing us for volume rather than for cause.

    A 429 says "you are asking too often", which is the one failure this script
    causes itself: probing a dead feed costs up to twenty more requests to the
    same domain, and running the whole config at eight workers is enough to trip
    JMK and Renewable Mirror. Both were read perfectly by the tracker minutes
    earlier. Reporting that as a dead feed sends someone hunting for a
    replacement URL for a feed that works.
    """
    try:
        return vf.fetch(url).status_code == 429
    except Exception:
        return False


def empty_feed(url):
    """Details if `url` serves a well-formed feed that currently has no items.

    An empty feed is not a broken one. The URL is right, the server answers, the
    document parses - there is simply nothing in it today, which is ordinary for
    an event site or a publisher between issues, and it may carry items tomorrow.
    Counting that as dead sends someone hunting for a replacement URL for a feed
    that is working exactly as designed. `vf.validate` holds the stricter
    at-least-one-item bar on purpose, because it answers a different question:
    whether a publication is worth ADDING in the first place.
    """
    try:
        r = vf.fetch(url)
        if r.status_code >= 400:
            return None
        parsed = feedparser.parse(r.content)
        if parsed.entries or parsed.bozo or not parsed.version:
            return None
        return {"title": (parsed.feed.get("title") or "").strip()[:60],
                "last_build": (parsed.feed.get("updated")
                               or parsed.feed.get("published") or "").strip()[:40]}
    except Exception:
        return None


def check(site):
    """Return the site dict annotated with what we found."""
    out = dict(site)
    out["_found"] = None
    out["_how"] = None

    current = vf.validate(site["rss"])
    if current:
        out["_found"], out["_how"] = current, "configured URL"
    elif rate_limited(site["rss"]):
        # Not proven dead, and hunting for a replacement would only dig deeper
        # into the same rate limit. Leave the configured URL alone.
        out["_status"] = "rate-limited (HTTP 429) - not proven dead, re-run later"
        out["_tried"] = 0
        out["_rate_limited"] = True
        print(f"WAIT {site['name'][:38]:<38} {out['_status']}", flush=True)
        return out
    elif (blank := empty_feed(site["rss"])):
        # A working feed with nothing in it today. The link is right, so there
        # is nothing here to repair.
        when = blank["last_build"] or "no build date"
        out["_status"] = f"valid feed, 0 items ({when})"
        out["_tried"] = 0
        out["_empty"] = True
        print(f"IDLE {site['name'][:38]:<38} {out['_status']}", flush=True)
        return out
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
    # A rate limit is not a verdict. These stay out of the dead count so a
    # transient 429 never reads as a feed to go and replace.
    waiting = [r for r in results if not r["_found"] and r.get("_rate_limited")]
    # A valid feed with no items is neither working nor broken: the link is
    # correct and there is nothing to repair, so it is counted on its own rather
    # than inflating the dead list with feeds nobody can fix because nothing is
    # wrong with them.
    idle = [r for r in results if not r["_found"] and r.get("_empty")]
    dead = [r for r in results if not r["_found"]
            and not r.get("_rate_limited") and not r.get("_empty")]

    print(f"\n{len(good)} already correct · {len(fixed)} repaired · "
          f"{len(dead)} dead · {len(idle)} empty · {len(waiting)} rate-limited")

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

    if idle:
        lines += ["", "## Valid feed, no items - not broken", "",
                  "These serve a well-formed feed at the configured URL and it "
                  "currently holds no items. The link is correct and there is "
                  "nothing to repair; a publisher between issues, or an event "
                  "site, looks exactly like this and may carry items tomorrow.", "",
                  "| Feed | Configured URL | Status |", "|---|---|---|"]
        for r in idle:
            lines.append(f"| {r['name']} | {r['rss']} | {r.get('_status', '')} |")

    if waiting:
        lines += ["", "## Rate-limited - unproven, not dead", "",
                  "These answered HTTP 429 while this script was running. That is a "
                  "verdict on our request rate, not on the feed: probing one dead "
                  "feed costs up to twenty requests to the same host. Re-run for "
                  "these alone before touching their URLs.", "",
                  "| Feed | Configured URL |", "|---|---|"]
        for r in waiting:
            lines.append(f"| {r['name']} | {r['rss']} |")

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
        elif r.get("_rate_limited") or r.get("_empty"):
            # Never rewrite a flag, and never drop a feed, on a 429 or on an
            # empty feed. In the first case we were throttled rather than
            # answered; in the second the URL is correct and only the contents
            # are empty. Neither disproves what the config already says.
            pass
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
