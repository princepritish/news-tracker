# news-tracker

Daily email digest of Indian solar / BESS news and government tenders. Runs once
a day on Railway (cron), deployed from `main` — pushing to `main` deploys.

## Run it

```bash
pip install -r requirements.txt
python test_script.py     # 36 checks, offline
python test_tenders.py    # 31 checks, offline
```

Both suites stub Groq, newspaper3k, Brevo and feedparser — no network, no keys,
no cost. **Run both before and after any change; they must stay green.**

Rehearse against live feeds without sending anything:

```bash
DRY_RUN=1 SKIP_SEEDING=1 ONLY_SITES=3 python script.py
```

`SKIP_SEEDING=1` matters — without it an empty database just seeds and you see no
news.

## Known problems (as of the last production run)

1. **Brevo returns 401** — the account has an IP restriction and Railway's IP
   changes between deploys. Nothing in this repo can fix it; it needs the
   restriction removed at app.brevo.com/security/auth. Until then every run
   builds a correct digest and discards it.
2. **24 of 43 feeds fail.** 403s are Cloudflare-style blocks (a full browser
   User-Agent was added and may have recovered some); 404s are wrong URLs;
   "no entries" means the URL serves HTML, not a feed. `verify_feeds.py` finds
   the real ones. 41 entries in `config.json` are still marked
   `"verified": false`.
3. **Tender portals are completely untested.** `sources.json` holds 56 portals
   and not one has been fetched. Run `probe_tenders.py` — it sorts them into
   working / reachable-but-empty / unreachable and saves the HTML.

Both scripts are best run from a normal connection: government portals and
several publishers block datacentre IP ranges, so a hosted run reports failures
that a laptop does not.

## Architecture

One run, in order:

1. **Per feed** — skip seen URLs, then a keyword gate, then state matching on the
   headline and RSS blurb. Only what survives gets the article scraped; only what
   survives *that* costs an LLM call to extract the state.
2. **Cross-run dedup** — a numeric fingerprint (`1,200 MW` and `1.2 GW` both
   become `p1200`) suppresses stories whose figures were already sent this week.
3. **Clustering** — one LLM call groups the whole run by underlying event, so the
   same tender from ten outlets becomes one line.
4. **Tenders** (`TENDERS_ENABLED=1`) — harvest links from each portal, keep those
   whose anchor text matches a tender keyword.
5. **One email**, always sent, grouped by state, with a health footer.

## Invariants — do not break these

- **Never mark a matched article seen before Brevo accepts the email.** They are
  marked in `main()` after a successful send, never in `process_site()`. Getting
  this wrong silently destroys a day of news: the articles look reviewed, the
  next run finds nothing, and the log claims it will retry. This has already
  happened once in production.
- **Clustering fails open.** Bad JSON, a missing or repeated index, a timeout, a
  rate limit — all yield one cluster per article. A duplicate line is a nuisance;
  a dropped story is the actual failure.
- **Suppression needs an exact fingerprint match.** A story with no extractable
  figures is never suppressed.
- **Tender identity is portal + normalised title, never the URL.** GePNIC detail
  links carry session parameters that change between visits, so URL-based dedup
  would re-report every tender daily.
- **Tender collection is wrapped whole.** Any failure there costs the tender
  section only, never the news digest.
- **The email always sends**, even with no news, so silence means the cron broke.
- **Scrape failures and budget exhaustion are deferred, not dropped** — counted
  in `budget.deferred` and left unmarked so the next run retries them.

## Gotchas

- **A new deployment wipes `seen.db`.** Cron runs share the deployment's
  filesystem, but a deploy replaces it, so the history restarts empty and that run
  seeds instead of sending news. Mount a Railway volume and set `DB_PATH` to a
  path inside it to survive deploys.
- **Model IDs are resolved at runtime, not hardcoded.** This account has no Llama
  models at all — it has `openai/gpt-oss-20b` and `openai/gpt-oss-120b`. Don't
  reintroduce a hardcoded model ID. `groq/compound*` models are agentic pipelines,
  unreliable for strict JSON, and are deliberately last-resort.
- **Short tender keywords are word-bounded** so `pv` doesn't match "PVC pipes"
  and `epc` doesn't match inside a longer word. Keep that when editing
  `build_matcher`.
- **Keyword matching flattens punctuation**, so `pre-GI` matches
  `pre gi structure`. Two keywords still don't fire on real text:
  `lithium battery` misses "lithium-ion battery", and `hot dip structure` misses
  "hot-dip galvanised structures". Changing them to `lithium` and `hot dip` would
  fix it — left alone because the list is the owner's.
- **The keyword gate is what makes 43 feeds affordable.** Without it every
  off-topic article costs a scrape and an LLM call.

## Config

`config.json` — feeds, news keywords, tender keywords, 18 states.
`sources.json` — 18 regions × tender portals, generated from the owner's
spreadsheet.
`publications.json` — the 42 candidate publications with focus and site URL.

Required env: `GROQ_API_KEY`, `BREVO_API_KEY`, `EMAIL_TO`, optional `EMAIL_BCC`.
Everything else has a working default; see README.md.

## Not built

Phase 3 (LinkedIn / Instagram) is deliberately not started. Neither platform
offers a compliant way to read other people's posts — Instagram's Graph API
covers only your own business account plus limited hashtag search, and LinkedIn
needs approved Partner access. Scraping breaks behind login walls and violates
their terms, which is the opposite of what this project needs. It needs a
decision (own accounts via official API, a paid provider, or skip) before any
code.
