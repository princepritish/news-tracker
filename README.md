# news-tracker

## Adding publications

`publications.json` holds the 42 candidate energy/solar/storage trade publications.
`verify_feeds.py` checks which of them actually expose an RSS/Atom feed and writes
`feed_report.md` plus machine-readable `feeds.json`:

```bash
pip install -r requirements.txt
python verify_feeds.py                   # probe all, write the report
python verify_feeds.py --only 1,5,33     # probe specific publications
python verify_feeds.py --merge-config    # append verified feeds to config.json
```

It tries the feed the homepage advertises first, then conventional paths
(`/feed/`, `/rss.xml`, ...), and only accepts a URL that parses as a feed *and*
has at least one item — so sites serving an HTML error page at `/feed/` are
reported as misses rather than silently added to `config.json`.

Run it from an unrestricted network: sites that block datacenter IP ranges can
show up as misses even when they do have a working feed.

## Output

The digest is written for a **client**, so it carries no diagnostics: no feed or
portal errors, no LLM or scrape counts, no database path, no explanation of why
the history was empty. Just news grouped by state, then tenders grouped by state.

Everything an operator needs is printed to the run log instead, and can also be
written to a file with `INTERNAL_REPORT_PATH`. That split is deliberate: the
"silence means the cron broke" signal still exists, it just isn't the client's
problem.

Email is a second, optional channel. `EMAIL_ENABLED=0` runs report-only and
needs no Brevo key — which is the working mode while Brevo's IP restriction is
unresolved. In report-only mode the report file is the delivery, so news is
marked seen once the file is written; with email on, news is marked seen only
after Brevo accepts it.

## Environment variables

- `GROQ_API_KEY`: Groq API key. Required.
- `EMAIL_ENABLED`: `1` (default) to send email, `0` for report-only
- `REPORT_PATH`: where to write the client report (default `report.md`)
- `INTERNAL_REPORT_PATH`: optional second report keeping the diagnostics.
  Off by default - the same detail is printed to the run log every run.
- `BREVO_API_KEY`: Brevo API key. Required unless `EMAIL_ENABLED=0`
- `EMAIL_TO`: Comma-separated recipient list. Required unless `EMAIL_ENABLED=0`
- `EMAIL_BCC`: Optional single BCC email ID
- `EMAIL_SUBJECT`: subject line the client sees (default `Today's Solar Alerts`).
  Fixed on purpose - a story count in a subject is an internal detail, and
  "0 story(ies)" is a poor thing to land in a client inbox on a quiet day.
- `LEDE_CHARS`: how much of a summary can decide the topic (default `400`).
  A keyword past this point is treated as a passing mention, which is what
  keeps bidder lists and "related articles" tails out of the digest.

### Article summaries

Every story in the digest carries one sentence saying what happened, so the
digest can be read without opening each link.

It is built in two tiers, and the first one is free and always runs: the
article's own opening, taken from the feed description or from the body the run
already scraped to find the state, stripped of bylines, share prompts and each
publisher's own truncation marker. The model then rewrites those into one clean
sentence per story — in batches, after clustering, so only stories that will
actually be sent are paid for.

**The model tier can fail without costing you anything.** A rate limit, bad
JSON, a dead key or `SUMMARY_ENABLED=0` all leave the extracted lede in place;
no story ever loses its summary, and none is ever dropped or reordered by
summarising. Summaries also yield to the LLM budget, because state extraction
decides whether an article is news at all and must not be starved by polish.

| Variable | Default | Purpose |
|---|---|---|
| `SUMMARY_ENABLED` | `1` | `0` uses the extracted lede only, no LLM calls |
| `SUMMARY_CHARS` | `260` | Longest extracted lede before it is trimmed |
| `SUMMARY_BATCH` | `8` | Stories summarised per LLM call |
| `MAX_SUMMARY_CALLS_PER_RUN` | `6` | Ceiling on summarising calls, on top of the shared budget |
| `EMAIL_WRAP` | `76` | Where summary text wraps in the plain-text email |

## Phase 2 — government tenders

`sources.json` holds 18 regions from the tender database: e-tender portal, REDA
agency and DISCOM for each. Always on - tenders are half of what the digest is for.

```bash
python probe_tenders.py        # which portals respond, and what they return
python test_tenders.py         # offline tests, no network
```

**The state GePNIC e-tender portals cannot be read.** Probed on 22 Aug 2026:
every public listing page, the MIS reports site and the national CPPP aggregator
all put the tender list behind a captcha. There is no non-captcha route, so they
report `captcha-gated` rather than looking like empty portals. The tenders that
can be read come from the agency and DISCOM sites, which is why those are read
by default.

Portals are read by harvesting every link and keeping those whose text matches a
tender keyword, rather than by parsing each site precisely. Tender titles live in
link text, so this survives restyling and works on platforms nobody has adapted
yet — at the cost of precision. Run `probe_tenders.py` to capture real HTML:
portals returning many links but no matches are the ones needing a dedicated
adapter, usually because the listing sits behind a search form.

Tender identity is portal + normalised title, deliberately **not** the URL —
GePNIC detail links carry session parameters that change between visits, which
would re-report the same tender every day.

Tender collection makes **no LLM calls**, and is wrapped so any failure — a dead
portal, a malformed page, all 36 portals down at once — costs you the tender
section only, never the news digest.

**Portal health, 2 Sep 2026: 16 of 56 portals return tenders**, up from 13 after
seven wrong URLs in `sources.json` were repaired. Those seven had been failing
DNS outright — `wbredda.org` was simply a typo for `wbreda.org` — and three of
the replacements carry real tenders: Bihar REDA (`breda.co.in`, 6), West Bengal
REDA (`wbreda.org`, 3) and Bhutan's agency (`www.moenr.gov.bt`, 1). Swapping
Bhutan's e-tender portal to the official `www.egp.gov.bt` also takes ~40 seconds
off every run.

Of the 40 that still return nothing: 13 are captcha-gated GePNIC portals
(unfixable from here), 3 return HTTP errors, 23 are read fine and simply had
nothing matching that day, and one — `power.nagaland.gov.in` — still has no
working URL and needs the real address from the tender spreadsheet.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_PORTAL_FETCHES` | `60` | Portal requests per run (sources.json holds 56) |
| `PORTAL_TIMEOUT` | `25` | Seconds per portal |
| `MAX_TENDERS_PER_EMAIL` | `0` | Cap on listed tenders; `0` means no cap |
| `PORTAL_WORKERS` | `8` | Portals fetched at once |
| `PORTAL_KINDS` | `etender,tender_page,agency,discom` | Which portals to read |

## Testing

Four levels, cheapest first. Nothing below level 3 can reach an inbox.

**1. Offline suite — no network, no API keys, no cost**

```bash
python test_script.py     # 167 checks
python test_tenders.py    # 115 checks
```

Covers the normal path plus the failure modes: clustering failure, storage
failure, dead feeds, no usable model, budget caps, meta-refresh stubs,
captcha-gated portals, concurrent portal collection, the state-scoped
suppression key, and every way summarising can fail — a rate limit, an empty
model answer, an exhausted budget, `SUMMARY_ENABLED=0`. Every case asserts a
digest is still produced, and every summary case asserts the story keeps a
readable sentence.

**2. Dry run against live feeds — real data, nothing sent**

```bash
export GROQ_API_KEY=...
EMAIL_ENABLED=0 SKIP_SEEDING=1 ONLY_SITES=3 python script.py
```

`EMAIL_ENABLED=0` writes `report.md` and needs no Brevo key. `DRY_RUN=1` also
prints the email body to stdout instead of sending it.

Prints the exact email to stdout instead of sending it. `SKIP_SEEDING=1` is what
makes this useful — without it an empty database just seeds and shows no news.
`ONLY_SITES=3` keeps the rehearsal quick; drop it for the full list.

Watch for: `[MODELS] N available:` (which Groq models your key really has),
`[FEED ERROR]` lines (broken URLs), and whether clustering merged what it should.

**3. Real send, to yourself only**

```bash
EMAIL_TO=you@example.com SKIP_SEEDING=1 ONLY_SITES=5 python script.py
```

Confirms Brevo delivery and how the digest actually renders in a mail client.

**4. On Railway**

Set `DRY_RUN=1` in the service variables and trigger a run manually. The email
appears in the deploy logs. Remove the variable when you're satisfied.

Feed URLs are checked separately. The two scripts answer different questions:

```bash
python check_config_feeds.py         # are the configured feeds alive?
python check_config_feeds.py --write # repair moved URLs, set verified flags
python verify_feeds.py               # do the 42 candidate publications have feeds?
```

`check_config_feeds.py` re-probes every feed in `config.json`, and when one is
dead goes looking for a working URL on the same domain before giving up.

Both scripts fetch the way `script.py` does, retrying a 403 with `curl_cffi`.
That matters: without it the checker called 25 of 42 feeds dead while the
tracker was reading 29 of them. A 429 is reported separately as "rate-limited,
unproven" rather than dead, because probing one dead feed costs up to twenty
requests to the same host and can trip a limit on a feed that works.

The report separates three states, because only one of them is a repair job:

- **dead** — no feed at the URL and none on the domain. Fix the URL.
- **empty** — a well-formed feed carrying no items right now. The link is
  correct and there is nothing to fix; an event site, or a publisher between
  issues, looks exactly like this and may carry items tomorrow.
- **rate-limited** — HTTP 429, no verdict on the feed at all. Re-run later.

Only *dead* feeds get their URL rewritten or their `verified` flag cleared.

As of 2 Sep 2026, measured through that path: **31 feeds configured — 29 carry
items and 2 are empty** (India Energy Storage Week and Construction World, both
serving valid RSS 2.0 with no items). The 11 that were broken have been dropped
from `config.json`: 2 Cloudflare 403s (Renewable Energy World, Battery
Industry), 3 404s (Energy Next, Power Engineering, Sustainability Magazine) and
6 that served HTML rather than a feed. See `config_feed_report.md`, and
`link_health.xlsx` for the full record of every non-working URL.

None of the 11 was repairable by changing the URL, and that was checked
exhaustively before dropping them — ~30 conventional paths each, re-swept with
Chrome TLS impersonation, plus homepage autodiscovery. They are broken at the
publisher, and nothing in the code can bring them back.

### Deduplication

- `DEDUP_MODE`: `shadow` (default), `on`, or `off`
  - `shadow` — clusters and logs what it *would* merge, but still sends one email
    per article. Read the `[CLUSTER]` lines in the logs to judge whether the
    grouping is sound before switching to `on`.

    Note: shadow mode preserves the one-email-per-article *format*, not the exact
    set of emails. State extraction (below) is active in every mode, so articles
    that name a state only implicitly now qualify and will arrive from the first
    run onward.
  - `on` — sends a single digest per run, one section per story, with
    "Also covered by: …" listing the other outlets.
  - `off` — no clustering at all, one email per article.
- `DEDUP_WINDOW_DAYS`: how far back to look for a repeat of the same story (default `7`)
- `CLUSTER_MODEL`: model for the once-per-run grouping call (default `llama-3.3-70b-versatile`)
- `SUMMARY_MODEL`: model for per-article summaries and state extraction (default `llama-3.1-8b-instant`)
- `MAX_ENTRIES_PER_SITE`: entries read per feed per run (default `30`)

Clustering **fails open**: on malformed JSON, a dropped index, a timeout or a rate
limit, every article becomes its own cluster and everything sends. A duplicate
email is a nuisance; a story silently dropped because a parse failed is not.

Example:

```bash
export EMAIL_TO="to1@example.com,to2@example.com"
export EMAIL_BCC="bcc@example.com"
```
