# yc-hiring-agent

Finds companies that are **(1) YC-funded in the last N years**, **(2) genuinely
still startups (not grown-up MNCs)**, and **(3) currently hiring for software
roles** — with strict, auditable filtering and without doing anything that risks
an account ban.

It is not a "scraping agent." It is a thin, well-behaved client over YC's own
**first-party structured data**. That single design decision is what buys you
accuracy, reliability, and safety at the same time.

---

## Why this design (read this first)

The naive version of this task — drive a headless browser over Google /
LinkedIn / Indeed, fuzzy-match company names, parse rendered HTML — is exactly
what produces banned accounts, ToS violations, and *bad* results (stale
reposts, wrong-company matches, missing roles). We do none of that.

YC's own website is powered by **two public Algolia search indexes**, the same
ones your browser queries when you use the YC directory and Work at a Startup:

| Index (logical) | Source we use | Gives us |
|---|---|---|
| Company directory (`YCCompany_production`) | the [`yc-oss`](https://github.com/yc-oss/api) daily JSON mirror — no key needed | batch, status, headcount → "is YC + which batch + still a startup" |
| Job board (`WaaSPublicCompanyJob_…`) | queried live with YC's public search-only key | every **currently open** role, with a `role` facet → "currently hiring + software" |

Both are **public, read-only, and meant to be queried by clients.** We use a
*static daily mirror* for the facts that change slowly (a company's batch and
status don't move intraday) and a *live query* for the fact that must be fresh
(open roles). Joining the two on `company_id` is the entire trick.

This is also why each filter is exact rather than heuristic:

- **"Funded in the last N years"** → a company is in-window iff its YC batch's
  start date is on or after `today − N years`. Deterministic, boundary-correct.
- **"Currently hiring"** → the role exists as a live row in the job index right
  now. We do *not* trust a company-level "isHiring" boolean, which can lag.
- **"Software role"** → YC's own `role:eng` facet (authoritative), with a
  precision-guarded title fallback that still rejects mechanical / electrical /
  hardware / sales engineering, because "software related" should mean software.
- **"Truly a startup, not an MNC"** → headcount cap + `status == Active`. (For
  example, Gusto is in the live hiring feed with ~2,400 staff and a W12 batch;
  the batch window and the headcount cap both remove it.)

### Why it won't get you banned

- **No authentication anywhere.** Every endpoint is public. There is no logged-in
  account that can be suspended. (This is the single biggest ban-avoidance
  decision — do not point a personal WaaS login at automation.)
- **No PII collection.** We deliberately skip the `companies/fetch` endpoint
  that returns founder / hiring-manager details, so there's nothing sensitive in
  the output and no CSRF/session dance to reverse.
- **Tiny, polite footprint.** The job index paginates 1,000 hits per request, so
  a full sweep is a handful of calls, not thousands of page loads. One global
  rate limiter (~3 req/s), exponential backoff that honours `Retry-After`,
  on-disk caching, an honest contactable `User-Agent`, and a `robots.txt` check
  before any HTML fetch.

*Not legal advice:* querying public data has supportive precedent (hiQ v.
LinkedIn), but site terms still apply. Skim YC's and WaaS's ToS + `robots.txt`,
keep volume reasonable, and put a real contact in the `User-Agent`.

---

## Architecture

```
                 ┌──────────────────────────┐
  Stage 1        │  yc-oss daily JSON mirror │   (no credentials)
  company        │  batches/<recent>.json    │
  universe ──────┤  → in-window batch        │
                 │  → status == Active       │
                 │  → team_size ≤ cap        │
                 └─────────────┬─────────────┘
                               │  dict[company_id → Company]
                               ▼
                 ┌──────────────────────────┐
  Stage 2        │  WaaS jobs Algolia index  │   (public search key)
  live open ─────┤  filters = role:eng       │
  software roles │  exhaustive pagination,   │
                 │  facet-partition if > 1k  │
                 └─────────────┬─────────────┘
                               │  list[Job]
                               ▼
  Stage 3   inner join on company_id  +  software-title precision filter
  Stage 4   emit matches.jsonl + matches.csv + run_manifest.json (with timestamps)
```

The source objects are the only things that touch the network. Everything else —
window math, the startup guards, role classification, the join — is a pure
function over parsed models, which is why it is fully unit-tested offline.

### Layout
```
yc_agent/
  config.py     # Config model, YAML loader, batch-window predicate
  models.py     # pydantic models (validation at the boundary catches schema drift)
  http.py       # PoliteSession (cache/retry/rate-limit/robots) + AlgoliaClient
  sources.py    # YcOssSource, WaasJobsSource, public-key bootstrap
  classify.py   # software-role decision (role facet + guarded title fallback)
  pipeline.py   # orchestration + JSONL/CSV/manifest writers
  cli.py        # entrypoint
tests/test_agent.py   # offline tests on real fixtures (14 cases)
config.yaml
```

---

## Usage

```bash
pip install -r requirements.txt

# See exactly which batches your window resolves to (no job fetch):
python -m yc_agent.cli --config config.yaml --dry-run

# Full run:
python -m yc_agent.cli --config config.yaml

# Stricter: last 2 years, ≤ 200 people, drop YC "top company" badge holders:
python -m yc_agent.cli --config config.yaml --years-back 2 --max-team-size 200 --exclude-top-company
```

Outputs land in `out/`:
- `matches.csv` — one row per open software role, with company context and an
  `observed_at` timestamp on every record (provenance = auditability).
- `matches.jsonl` — one JSON object per company with its nested matched jobs.
- `run_manifest.json` — counts at each funnel stage + the exact filters used.

Run the tests:
```bash
python -m pytest -q          # or:  python tests/test_agent.py
```

### The Algolia public key
`api_key` is YC's **search-only** key, the same one their site serves to every
browser. Keys can rotate, so by default the agent reads the current one off the
WaaS page at runtime. If extraction ever fails, pin it manually:

> DevTools → Network → the `*-dsn.algolia.net` request → copy the
> `X-Algolia-API-Key` header → set `algolia.api_key` in `config.yaml`.

---

## Tuning the strict filters

| Want | Change |
|---|---|
| A different funding window | `years_back` (or `--years-back`) |
| Reproducible "as of" a past date | `as_of: 2025-12-31` |
| Tighter MNC exclusion | lower `max_team_size`; `exclude_top_company: true` |
| Broader "software" (e.g. include data/ML facets) | add to `software_roles` |
| Maximum precision (role facet only) | `title_fallback.enabled: false` |
| Maximum recall (accept more titles) | extend `title_fallback.include_patterns` |

---

## Known limitations (stated honestly)

- **Upstream truth.** Accuracy is bounded by YC's own data. If a company hasn't
  posted a role to WaaS, it won't appear — but WaaS *is* the canonical YC hiring
  source, so this is the right ceiling to accept.
- **Season→date is approximate.** Batches are dated by season start month
  (configurable); a batch sitting exactly on the N-year boundary is included.
- **The public key may rotate.** Handled by runtime discovery + a clear manual
  override; there's no scenario where it fails silently.
- **Truncation is detected, never hidden.** If a result slice would exceed
  Algolia's 1,000-result ceiling, the agent partitions by facet; if it still
  can't, it logs a loud warning rather than quietly dropping rows.
```
```
