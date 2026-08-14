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
