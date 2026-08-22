# news-tracker

Daily digest of Indian solar / BESS news and government tenders. Runs once a day
on Railway (cron), deployed from `main` — pushing to `main` deploys.

Every run writes `report.md`. Email is a second, optional channel.

## Run it

```bash
pip install -r requirements.txt
python test_script.py      # 44 checks, offline
python test_tenders.py     # 57 checks, offline
```

Both suites stub Groq, newspaper3k, Brevo and feedparser — no network, no keys,
no cost. **Run both before and after any change; they must stay green.**

Rehearse against live feeds without sending anything:

```bash
GROQ_API_KEY=... EMAIL_ENABLED=0 SKIP_SEEDING=1 TENDERS_ENABLED=1 python script.py
```

`SKIP_SEEDING=1` matters — without it an empty database just seeds and you see no
news. `EMAIL_ENABLED=0` needs no Brevo key at all.

## Report-only vs email

`EMAIL_ENABLED=0` is the working mode while Brevo is blocked (below). The report
file becomes the delivery: news is marked seen once `report.md` is written.

With `EMAIL_ENABLED=1` nothing about the old behaviour changes — news is marked
seen only after Brevo returns 2xx, because a failed send must not burn a day of
news. Do not "simplify" these into one path; they are different on purpose.

## Known problems (as of 22 Aug 2026)

1. **Brevo returns 401** — the account has an IP restriction and Railway's IP
   changes between deploys. Nothing in this repo can fix it; it needs the
   restriction removed at app.brevo.com/security/auth. Run with
   `EMAIL_ENABLED=0` until then.
2. **13 of 43 feeds fail** (30 work), verified live on 22 Aug 2026 by
   `check_config_feeds.py` (writes `config_feed_report.md`):
   - 2 are Cloudflare **403** that nothing shifts — Renewable Energy World and
     Battery Industry refuse every impersonation profile (chrome/safari/firefox).
   - 2 are **404** (Energy Next, Power Engineering) — the path is simply wrong.
   - 8 return **HTML, not a feed**, at every conventional path.
   - 1 times out (Construction World).

   **The 403s were never about Railway's IP.** The blocked publishers answer
   with `cf-mitigated: challenge`: Cloudflare rejects Python's TLS handshake
   before it reads a single header, so no `User-Agent` could ever have helped.
   Mercom works because it sits on Vercel with no bot protection. `curl_cffi`
   replays Chrome's real TLS fingerprint and recovers **PV Tech, Energy Storage
   News and Solar Builder** — three of the best sources in the list. It is a
   fallback, tried only after a genuine 403, and it is optional: if the import
   fails those feeds simply stay 403 as before.
3. **Some government portals need TLS verification turned off.** Andhra Pradesh
   REDA, Madhya Pradesh DISCOM, Sikkim DISCOM and the Bihar e-tender portal
   present expired or incomplete certificate chains and fail the handshake
   outright. `fetch_portal` retries those once with `verify=False`. This is
   deliberate and narrow: verified HTTPS is always attempted first, these are
   public tender listings, and no credential is ever sent to them. Do not widen
   it past portal fetches.

4. **The GePNIC e-tender portals cannot be scraped at all.** Probed for real on
   22 Aug 2026: every public listing page — Active Tenders, Tenders by Closing
   Date, by Organisation, by Location, by Classification, the MIS reports site,
   and the national CPPP aggregator — puts the tender list behind a captcha.
   There is no non-captcha route. They are still walked so the wall stays
   visible in the report, and they now report `captcha-gated` instead of
   masquerading as an empty portal. **Do not spend time writing a GePNIC
   parser; the wall is the problem, not the markup.**

   The readable tenders come from the **agency and DISCOM** sites, which is why
   those kinds are now read by default.

`verify_feeds.py` (discovers feeds for `publications.json`) and
`probe_tenders.py` (categorises portals, saves their HTML) are both best run
from a normal connection: government portals and several publishers block
datacentre IP ranges, so a hosted run reports failures a laptop does not.

## Architecture

One run, in order:

1. **Per feed** — skip seen URLs; strip markup off the summary; a keyword gate
   over the headline and lede; then state matching that requires exactly one
   state, located. Only what survives gets the article scraped; only what
   survives *that* costs an LLM call to extract the state. A recurring page with
   a stable URL can therefore only ever appear once.
2. **Cross-run dedup** — a numeric fingerprint (`1,200 MW` and `1.2 GW` both
   become `p1200`), scoped by state, suppresses stories whose figures were
   already sent this week.
3. **Clustering** — one LLM call groups the whole run by underlying event, so the
   same tender from ten outlets becomes one line.
4. **Tenders** (`TENDERS_ENABLED=1`) — harvest links from each portal in
   parallel, keep those whose anchor text matches a tender keyword.
5. **One report** (`report.md`), always written, grouped by state, with a health
   section. Optionally also one email.

## Invariants — do not break these

- **Never mark a matched article seen before it has actually been delivered.**
  With email on that means Brevo returning 2xx; with `EMAIL_ENABLED=0` it means
  `report.md` being written. Either way it happens in `main()`, never in
  `process_site()`. Getting this wrong silently destroys a day of news: the
  articles look reviewed, the next run finds nothing, and the log claims it will
  retry. This has already happened once in production.
- **Clustering fails open.** Bad JSON, a missing or repeated index, a timeout, a
  rate limit — all yield one cluster per article. A duplicate line is a nuisance;
  a dropped story is the actual failure.
- **Suppression needs an exact fingerprint match, scoped by state.** A story with
  no extractable figures is never suppressed. The state scoping is not
  decoration: a live run turned up three unrelated 100 MW stories with the
  identical fingerprint `{p100}`, and on figures alone the second would have
  silently suppressed the first.
- **Tender identity is portal + normalised title, never the URL.** GePNIC detail
  links carry session parameters that change between visits, so URL-based dedup
  would re-report every tender daily.
- **Tender collection is wrapped whole.** Any failure there costs the tender
  section only, never the news digest.
- **The report is always written**, even with no news, so silence means the cron
  broke.
- **Scrape failures and budget exhaustion are deferred, not dropped** — counted
  in `budget.deferred` and left unmarked so the next run retries them.

## Gotchas

- **Only a new deployment wipes `seen.db`** — that is, a commit change. Every
  cron run on the same commit shares the same filesystem and keeps its history,
  so this costs one seeding run per deploy, not per day. Pointing `DB_PATH`
  inside a mounted volume carries history across deploys too.
- **Deferral is the real capacity limit.** A first run on an empty history defers
  ~160 articles against `MAX_SCRAPES_PER_RUN`/`MAX_LLM_CALLS_PER_RUN` (both 60).
  In steady state a day's genuinely-new articles sit far below that, but if
  `Deferred to next run` stays high in the report every day, raise the caps or
  the backlog never clears.
- **Model IDs are resolved at runtime, not hardcoded.** This account has no Llama
  models at all — it has `openai/gpt-oss-20b` and `openai/gpt-oss-120b`. Don't
  reintroduce a hardcoded model ID. `groq/compound*` models are agentic pipelines,
  unreliable for strict JSON, and are deliberately last-resort.
- **Always pass `encoding="utf-8"` when opening a file.** The corpus is full of
  `₹`, en-dashes and Indic script; on Windows the default is cp1252 and both
  reads and writes blow up on them. `verify_feeds.py` used to crash *after* every
  network probe for exactly this reason. `script.py` also forces stdout/stderr to
  UTF-8 at import for the same reason.
- **Short tender keywords are word-bounded** so `pv` doesn't match "PVC pipes"
  and `epc` doesn't match inside a longer word. Keep that when editing
  `build_matcher`.
- **Keyword matching flattens punctuation**, so `pre-GI` matches
  `pre gi structure`. Two keywords still don't fire on real text:
  `lithium battery` misses "lithium-ion battery", and `hot dip structure` misses
  "hot-dip galvanised structures". Changing them to `lithium` and `hot dip` would
  fix it.

  **Leave the news keyword list alone.** This is a settled decision, not an
  oversight: on 22 Aug 2026 the owner was shown the measured hit counts below
  and chose to keep the list unchanged, including the two broken entries. The
  news half is meant to be a solar/BESS project-and-financing digest. Don't
  re-propose edits to `filters.keywords` without being asked.
- **The keyword gate is what makes 43 feeds affordable.** Without it every
  off-topic article costs a scrape and an LLM call. Measured live: it keeps
  ~50% of entries (187 of 375).
- **Three rules keep passing mentions out.** All three came from reading a live
  report and finding junk in it; none is decoration, and removing any one puts
  the junk back:
  1. *Summaries are stripped of markup before matching* (`strip_html`). Feed
     summaries contain HTML, and `flatten()` turns a tag into words - an
     `<a href=".../solar-energy-news/...">` made every article from that
     publisher match `solar`. This is why a wind tender and a data-centre
     round-up were in a solar digest.
  2. *Only the headline and lede decide the topic* (`topical_text`, first
     `LEDE_CHARS`=400 characters). Past that you are matching bidder lists and
     "related articles" tails.
  3. *A state must be unambiguous and locational* (`find_state`). The headline
     wins; naming several states means none of them (a five-state round-up is
     not news about whichever the config lists first); and a lone body mention
     needs a location cue nearby, so "MB Power **Madhya Pradesh** Ltd" in a
     bidder list no longer files a national tender under Madhya Pradesh.

  Measured on the same live corpus, these took the report from ~65% to ~94%
  on-target. The cost is recall: "APERC Clears Path for Rooftop Solar" loses its
  state, because "Andhra Pradesh Electricity Regulatory Commission" is an
  organisation rather than a place. That is not a drop - it falls through to the
  scrape-and-ask-the-LLM path, which resolves it when `GROQ_API_KEY` works.

- **Six of the twelve news keywords never fire.** Measured over 375 live
  entries: `solar` 170, `bess` 31, `battery energy storage system` 19,
  `solar inverter` 2, `lithium battery` 1, and zero for `solar mounting
  structure`, `hybrid inverter`, `solar battery`, `hot dip structure`,
  `pre gi structure`, `zem structure`, `galvalume structure`. Trade press writes
  about projects, financing and policy, not about galvalume. The component
  keywords belong to the tender side (`tender_keywords` already has
  `mounting structure` and `galvalume`) - which is the half GePNIC has walled
  off. Don't "fix" the news gate to chase them; the channel cannot answer that
  question.

## Config

`config.json` — feeds (with `verified` flags), news keywords, tender keywords,
18 states.
`sources.json` — 18 regions × 56 tender portals, generated from the owner's
spreadsheet.
`publications.json` — the 42 candidate publications with focus and site URL.

Required env: `GROQ_API_KEY`. Also `BREVO_API_KEY` and `EMAIL_TO` unless
`EMAIL_ENABLED=0`. Everything else has a working default; see README.md.

## Not built

Phase 3 (LinkedIn / Instagram) is deliberately not started. Neither platform
offers a compliant way to read other people's posts — Instagram's Graph API
covers only your own business account plus limited hashtag search, and LinkedIn
needs approved Partner access. Scraping breaks behind login walls and violates
their terms, which is the opposite of what this project needs. It needs a
decision (own accounts via official API, a paid provider, or skip) before any
code.
