# yc-hiring-agent — contact enrichment add-on

Takes the `matches.csv` from the matcher and finds **verified** email contacts in
the roles you care about (CEO / CTO / Founder / Eng-lead / HR / Talent), so you
can reach a real decision-maker at each startup.

It is built around one hard rule you set: **verified only.** That word does a lot
of work, so read the contract below before trusting the output.

---

## What "verified only" actually means here

Email verification has a ceiling that no tool can exceed, and pretending
otherwise is how you end up with a list that *looks* clean and bounces anyway.
The honest picture:

| Status | What it means | Default action |
|---|---|---|
| **valid** | Mailbox confirmed deliverable (SMTP check passed, not catch-all) | ✅ kept |
| **accept_all** | Domain accepts mail to *any* address — the individual mailbox **cannot be verified** | ❌ excluded (opt-in) |
| **invalid** | Failed checks; mailbox doesn't exist | ❌ dropped |
| **webmail** | gmail/outlook/etc.; B2B verifiers don't probe these | ❌ dropped |
| **disposable** | burner address | ❌ dropped |
| **unknown** | greylisted / blocked / timed out | ❌ dropped |

The one that bites people is **accept-all (catch-all)**. Many YC startups run
Google Workspace with a catch-all, which "always returns OK for any mailbox" —
so a verifier literally cannot tell you whether `jane@startup.com` is real. This
tool does **not** silently pass those off as verified. By default they're
excluded; with `--include-accept-all` they're emitted but clearly marked
`status=accept_all` so you always know the difference. At a 3-person startup the
`{first}@domain` pattern is usually right even on a catch-all, so the option is
there — labelled, never disguised.

Every emitted row carries its evidence: `status`, `confidence`, `smtp_check`,
`accept_all`, `source`, and `verified_on`. That's the audit trail that makes
"verified" a claim you can actually stand behind.

---

## How it works

```
matches.csv ──► unique companies (dedupe on company_id)
                     │  derive email domain from company_website
                     ▼
            Hunter domain-search ──► people + position + per-email verification
                     │  keep only titles that map to your target roles
                     ▼
            verified-only policy ──► drop catch-all / invalid / unknown / low-confidence
                     │  (optional) cross-check "valid" with your own SMTP probe
                     ▼
            if nobody survives ──► role-inbox fallback (careers@, jobs@, founders@…)
                     │              verified the same way
                     ▼
            subtract suppression list ──► contacts.csv + contacts.jsonl
```

**Why a third-party provider instead of scraping LinkedIn?** Because LinkedIn /
social scraping is the fastest route to exactly the banned-account, bad-data
outcome you're trying to avoid. A purpose-built provider (Hunter) returns the
person, their role, a confidence score, *and* a verification status from warmed
infrastructure — and it's the same vendor category every legitimate sales/
recruiting team uses. We talk to its documented REST API through the same
cached, rate-limited session as the matcher.

### Files (drop-in)
```
yc_agent/
  http.py              # shared PoliteSession (same as v1; keep yours if present)
  enrich/
    config.py          # reads the 'enrichment:' block; key comes from env
    models.py          # Contact, CompanyContacts, VerificationStatus, RoleBucket
    seniority.py       # position text -> role bucket (CEO/CTO/Founder/Eng/HR/Talent)
    verify.py          # self-hosted verifier: syntax + MX + optional SMTP probe
    providers.py       # Hunter client (domain-search / email-finder / verifier)
    pipeline.py        # orchestration + the verified-only policy + outputs
    cli.py             # entrypoint
tests/test_enrich.py   # 12 offline tests on real Hunter response shapes
config.enrichment.yaml # the block to append to your config.yaml
```

---

## Install & run

```bash
pip install requests requests-cache pydantic PyYAML dnspython email-validator

# 1) Copy the enrich/ folder into your existing yc_agent/ package.
# 2) Append config.enrichment.yaml to your config.yaml.
# 3) Get a free key at https://hunter.io and export it (NEVER put it in a file):
export HUNTER_API_KEY=sk_xxxxxxxxxxxxxxxx

# 4) Run against the matcher's output:
python -m yc_agent.enrich.cli --in out/matches.csv --config config.yaml

# Only founders + CTOs, and also surface catch-all addresses (clearly marked):
python -m yc_agent.enrich.cli --in out/matches.csv --roles cto,founder --include-accept-all

# Paranoid mode: re-verify every provider "valid" with your own SMTP probe:
python -m yc_agent.enrich.cli --in out/matches.csv --smtp-verify
```

Outputs in `out/`:
- **contacts.csv** — one row per verified contact, with full evidence columns.
- **contacts.jsonl** — one object per company (includes companies with zero
  verified contacts, plus `notes` explaining why).

Run the tests:
```bash
python -m pytest -q        # or:  python tests/test_enrich.py
```

---

## The API key, and the public-data question

- **Key lives in the environment, never in a file.** The config has no key field;
  the client reads `HUNTER_API_KEY`. This keeps a secret out of anything you might
  commit. Hunter's free tier is enough to try this; domain-search and verifier
  are the endpoints used.
- **Rate limits.** Hunter allows 15 req/s and 500 req/min on domain-search; the
  default pace (~5 req/s) sits well under, and responses are cached for 6 hours
  so re-runs are cheap.

### Self-hosted SMTP verification — read before enabling
`--smtp-verify` makes *your* machine connect to strangers' mail servers and probe
whether a mailbox exists. It's off by default for good reasons:
- Repeated probing from one IP can get that IP greylisted or blacklisted, hurting
  your own deliverability.
- Catch-all domains answer "yes" to everything, so a pass there is **not** proof.
- Many providers block the technique outright (→ `unknown`).

If you enable it, set `smtp.mail_from` to a domain you control. For most users the
provider's verifier (warmed IPs, done at scale) is both safer and more accurate.

---

## Compliance — this is for targeted outreach, not bulk mail

Finding a hiring manager's address to send a thoughtful application or a relevant
note is legitimate. Blasting a scraped list is not, and the law distinguishes
them. Keep yourself on the right side:

- **Suppression list is absolute.** Put any address in `out/suppression.txt`
  (one per line) and it is never emitted or contacted again. A "no" is permanent.
- **CAN-SPAM (US):** identify yourself honestly, no deceptive subject/headers,
  include a real reply path and a way to opt out, honor it promptly.
- **GDPR / similar (EU/UK):** business contact data can be processed under
  legitimate interest, but the person can object — honor it, and minimise (this
  tool only pulls role-relevant people, not everyone). 
- **No social scraping.** We deliberately don't touch LinkedIn or scrape personal
  profiles; contacts come from a provider that sources and verifies B2B data.
- **Volume + relevance.** A handful of well-targeted, relevant emails to startups
  you'd actually work at is the intended use. Treat it that way.

*Not legal advice — when in doubt for your jurisdiction or scale, check with a
professional.*

---

## Tuning

| Want | Change |
|---|---|
| Different roles | `target_roles` or `--roles ceo,cto,...` |
| Allow catch-all (marked) | `include_accept_all: true` / `--include-accept-all` |
| Stricter floor | raise `min_confidence` |
| Skip role-inbox fallback | `role_inbox_fallback: false` |
| Cross-check valids yourself | `--smtp-verify` (see caveats) |
| Add a provider | implement the 3 methods in `providers.py` and map its status in `normalise_status` |

## Known limitations (stated honestly)
- **Coverage is bounded by the provider's database.** A brand-new founder with no
  web footprint may simply not be findable; you'll get a role inbox instead.
- **Catch-all is a real wall**, not a bug — see the contract above.
- **Names can seed a lookup** (`email-finder`) but a name without a verifiable
  mailbox still won't pass the verified-only gate.
