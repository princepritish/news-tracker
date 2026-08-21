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

## Environment variables

- `GROQ_API_KEY`: Groq API key
- `BREVO_API_KEY`: Brevo API key
- `EMAIL_TO`: Comma-separated recipient list (must include at least two email IDs)
- `EMAIL_BCC`: Optional single BCC email ID

## Phase 2 — government tenders

`sources.json` holds 18 regions from the tender database: e-tender portal, REDA
agency and DISCOM for each. Off by default; enable with `TENDERS_ENABLED=1`.

```bash
python probe_tenders.py        # which portals respond, and what they return
python test_tenders.py         # offline tests, no network
```

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

| Variable | Default | Purpose |
|---|---|---|
| `TENDERS_ENABLED` | `0` | `1` switches Phase 2 on |
| `MAX_PORTAL_FETCHES` | `40` | Portal requests per run |
| `PORTAL_TIMEOUT` | `25` | Seconds per portal |
| `MAX_TENDERS_PER_EMAIL` | `40` | Cap on listed tenders |

## Testing

Four levels, cheapest first. Nothing below level 3 can reach an inbox.

**1. Offline suite — no network, no API keys, no cost**

```bash
python test_script.py
```

26 checks covering the normal path plus the failure modes: clustering failure,
storage failure, dead feeds, no usable model, budget caps. Every case asserts an
email is still produced.

**2. Dry run against live feeds — real data, nothing sent**

```bash
export GROQ_API_KEY=... BREVO_API_KEY=... EMAIL_TO=you@example.com
DRY_RUN=1 SKIP_SEEDING=1 ONLY_SITES=3 python script.py
```

Prints the exact email to stdout instead of sending it. `SKIP_SEEDING=1` is what
makes this useful — without it an empty database just seeds and shows no news.
`ONLY_SITES=3` keeps the rehearsal quick; drop it for the full 43.

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

Feed URLs are checked separately:

```bash
python verify_feeds.py          # which of the 43 actually serve a feed
```

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
