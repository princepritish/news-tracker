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

Example:

```bash
export EMAIL_TO="to1@example.com,to2@example.com"
export EMAIL_BCC="bcc@example.com"
```
