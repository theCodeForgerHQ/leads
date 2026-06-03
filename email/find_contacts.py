"""Free contact finder for YC startup matches.

Reads matches.csv, scrapes founder names from WaaS company pages,
generates email patterns, verifies via MX + optional SMTP probe.

Usage:
    python email/find_contacts.py \
        --in yc_agent_project/yc_agent_project/out/matches.csv \
        --out yc_agent_project/yc_agent_project/out

No API keys needed. All verification is self-hosted:
  - Syntax check (instant, free)
  - MX record lookup (free, confirms domain accepts mail)
  - Optional SMTP probe (--smtp-verify; see caveats below)

Caveats on SMTP probe:
  Many YC startups use Google Workspace with catch-all enabled.
  A catch-all domain returns 250 OK for *any* address, so a pass
  is NOT proof the specific mailbox exists — we tag it accept_all.
  For 2-10 person startups, {first}@domain is almost always correct
  regardless, so accept_all contacts are included by default (clearly
  labelled). Use --no-accept-all to drop them.
"""

from __future__ import annotations

import argparse
import csv
import html as _html
import json
import logging
import re
import smtplib
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests
import requests_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import dns.resolver
    _HAVE_DNS = True
except ImportError:
    _HAVE_DNS = False

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tiny HTTP session (cache + rate limit)
# ---------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


def _make_session(cache_path: str, ttl: int = 86400) -> requests_cache.CachedSession:
    Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
    s = requests_cache.CachedSession(
        cache_name=cache_path.removesuffix(".sqlite"),
        backend="sqlite",
        expire_after=ttl,
        allowable_methods=("GET",),
        stale_if_error=True,
    )
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET"}),
                  respect_retry_after_header=True, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return s


_DATA_PAGE_RE = re.compile(r'data-page="([^"]+)"')


# ---------------------------------------------------------------------------
# WaaS founder scraper
# ---------------------------------------------------------------------------
@dataclass
class Founder:
    first: str
    last: str
    full: str


def _parse_name(raw: str) -> Optional[Founder]:
    parts = raw.strip().split()
    if not parts:
        return None
    # Handle "Patrick D. McGuckian" -> first=Patrick last=McGuckian
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ""
    return Founder(first=first, last=last, full=raw.strip())


def fetch_company_founders(slug: str, session: requests_cache.CachedSession,
                           limiter: _RateLimiter) -> list[Founder]:
    url = f"https://www.workatastartup.com/companies/{slug}"
    try:
        limiter.wait()
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return []
        m = _DATA_PAGE_RE.search(resp.text)
        if not m:
            return []
        props = json.loads(_html.unescape(m.group(1))).get("props", {})
        founders_raw = props.get("company", {}).get("founders", [])
        result = []
        for f in founders_raw:
            name = f.get("name") or ""
            parsed = _parse_name(name)
            if parsed:
                result.append(parsed)
        return result
    except Exception as exc:
        log.debug("Failed to fetch founders for %s: %s", slug, exc)
        return []


# ---------------------------------------------------------------------------
# Email pattern generator
# ---------------------------------------------------------------------------
def _normalise(s: str) -> str:
    """Lowercase, strip accents/dots/hyphens for email local parts."""
    import unicodedata
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def email_patterns(founders: list[Founder], domain: str) -> list[tuple[str, str]]:
    """Return (email, label) pairs for all candidate permutations."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(email: str, label: str) -> None:
        e = email.lower()
        if e not in seen:
            seen.add(e)
            candidates.append((e, label))

    for f in founders:
        first = _normalise(f.first)
        last = _normalise(f.last)
        if not first:
            continue
        add(f"{first}@{domain}", "first")
        if last:
            add(f"{first}.{last}@{domain}", "first.last")
            add(f"{first}{last}@{domain}", "firstlast")
            add(f"{first[0]}{last}@{domain}", "flast")
            add(f"{first[0]}.{last}@{domain}", "f.last")
            add(f"{last}@{domain}", "last")

    # Role inboxes as fallback — always included
    for local in ("founders", "cto", "ceo", "hello", "team", "careers", "jobs", "hi"):
        add(f"{local}@{domain}", f"role:{local}")

    return candidates


# ---------------------------------------------------------------------------
# Email verifier (MX + optional SMTP)
# ---------------------------------------------------------------------------
_WEBMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
            "yahoo.com", "icloud.com", "proton.me", "protonmail.com"}

_mx_cache: dict[str, list[str]] = {}
_mx_lock = threading.Lock()


def mx_hosts(domain: str) -> list[str]:
    with _mx_lock:
        if domain in _mx_cache:
            return _mx_cache[domain]
    try:
        if _HAVE_DNS:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5)
            hosts = sorted(str(r.exchange).rstrip(".") for r in answers)
        else:
            # Fallback: try connecting to domain:25 directly
            hosts = [domain]
    except Exception:
        hosts = []
    with _mx_lock:
        _mx_cache[domain] = hosts
    return hosts


def smtp_probe(email: str, mx: list[str], mail_from: str = "verify@example.com",
               timeout: float = 8.0) -> tuple[bool, bool]:
    """Return (smtp_ok, is_catch_all). smtp_ok=True means mailbox accepted.

    Catch-all detection: if random address is also accepted -> catch_all=True.
    """
    if not mx:
        return False, False

    import random, string
    rand_local = "".join(random.choices(string.ascii_lowercase, k=12))
    rand_email = f"{rand_local}@{email.split('@')[1]}"

    def _probe(target: str) -> Optional[bool]:
        for host in mx[:2]:
            try:
                with smtplib.SMTP(host, 25, timeout=timeout) as s:
                    s.ehlo_or_helo_if_needed()
                    s.mail(mail_from)
                    code, _ = s.rcpt(target)
                    return code == 250
            except Exception:
                continue
        return None

    real = _probe(email)
    if real is None:
        return False, False
    rand = _probe(rand_email)
    catch_all = (rand is True)
    return bool(real), catch_all


@dataclass
class VerifyResult:
    email: str
    status: str          # valid | accept_all | invalid | no_mx | webmail
    mx_ok: bool = False
    smtp_ok: Optional[bool] = None
    catch_all: Optional[bool] = None


def verify_email(email: str, smtp_enabled: bool = False,
                 mail_from: str = "verify@example.com") -> VerifyResult:
    domain = email.rsplit("@", 1)[-1].lower()

    if domain in _WEBMAIL:
        return VerifyResult(email=email, status="webmail")

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return VerifyResult(email=email, status="invalid")

    mx = mx_hosts(domain)
    if not mx:
        return VerifyResult(email=email, status="no_mx")

    if not smtp_enabled:
        return VerifyResult(email=email, status="accept_all", mx_ok=True)

    smtp_ok, catch_all = smtp_probe(email, mx, mail_from=mail_from)
    if not smtp_ok:
        return VerifyResult(email=email, status="invalid", mx_ok=True,
                            smtp_ok=False, catch_all=catch_all)
    status = "accept_all" if catch_all else "valid"
    return VerifyResult(email=email, status=status, mx_ok=True,
                        smtp_ok=smtp_ok, catch_all=catch_all)


# ---------------------------------------------------------------------------
# Input reader
# ---------------------------------------------------------------------------
def read_companies(path: str) -> list[dict]:
    """Deduplicated list of companies from matches.csv or matches.jsonl."""
    p = Path(path)
    rows: list[dict] = []
    if p.suffix == ".jsonl":
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            c = obj.get("company", {})
            rows.append({
                "company": c.get("name", ""),
                "company_id": c.get("id"),
                "slug": c.get("slug", ""),
                "website": c.get("website", ""),
                "team_size": c.get("team_size"),
                "batch": c.get("batch", ""),
            })
    else:
        seen_ids: set = set()
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cid = r.get("company_id")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                rows.append({
                    "company": r.get("company", ""),
                    "company_id": cid,
                    "slug": "",   # not in CSV; will be looked up via jsonl
                    "website": r.get("company_website", ""),
                    "team_size": r.get("team_size"),
                    "batch": r.get("batch", ""),
                })
    return rows


def enrich_with_slugs(companies: list[dict], jsonl_path: Optional[str]) -> list[dict]:
    """Fill in slug from matches.jsonl (CSV doesn't carry it)."""
    if not jsonl_path or not Path(jsonl_path).exists():
        return companies
    slug_map: dict[str, str] = {}
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        c = obj.get("company", {})
        cid = str(c.get("id", ""))
        slug_map[cid] = c.get("slug", "")
    for row in companies:
        if not row.get("slug"):
            row["slug"] = slug_map.get(str(row.get("company_id", "")), "")
    return companies


def domain_from_website(website: str) -> Optional[str]:
    if not website:
        return None
    raw = website.strip()
    if "//" not in raw:
        raw = "//" + raw
    host = (urlsplit(raw).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host) else None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
@dataclass
class ContactRow:
    company: str
    company_id: Optional[str]
    batch: str
    team_size: Optional[str]
    website: str
    domain: str
    founder_name: str
    email: str
    email_label: str
    status: str
    mx_ok: bool
    smtp_ok: Optional[bool]
    catch_all: Optional[bool]
    observed_at: str


def run(infile: str, out_dir: str, cache_path: str = ".cache/enrich_cache.sqlite",
        smtp_enabled: bool = False, no_accept_all: bool = False,
        mail_from: str = "verify@example.com") -> list[ContactRow]:

    companies = read_companies(infile)
    # Try to pull slugs from sibling jsonl
    jsonl = str(Path(infile).with_suffix(".jsonl"))
    companies = enrich_with_slugs(companies, jsonl)

    session = _make_session(cache_path)
    limiter = _RateLimiter(0.35)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    contacts: list[ContactRow] = []

    for idx, row in enumerate(companies, 1):
        company = row["company"]
        slug = row.get("slug", "")
        website = row.get("website", "")
        domain = domain_from_website(website)
        team_size = row.get("team_size")
        batch = row.get("batch", "")

        log.info("[%d/%d] %s (slug=%s, domain=%s)",
                 idx, len(companies), company, slug, domain or "?")

        if not domain:
            log.debug("  -> no usable domain, skipping")
            continue

        # Fetch founders from WaaS (cached after first run)
        founders = []
        if slug:
            founders = fetch_company_founders(slug, session, limiter)
            log.debug("  -> %d founder(s): %s",
                      len(founders), [f.full for f in founders])

        candidates = email_patterns(founders, domain)

        # Verify and collect
        domain_done = False
        for email, label in candidates:
            result = verify_email(email, smtp_enabled=smtp_enabled, mail_from=mail_from)
            if result.status in ("no_mx", "webmail", "invalid"):
                # no_mx means domain is dead — skip entire company
                if result.status == "no_mx" and not domain_done:
                    log.debug("  -> no MX for %s, skipping", domain)
                    domain_done = True
                continue

            if no_accept_all and result.status == "accept_all":
                continue

            # For role inboxes, only keep the fallback ones (careers/jobs/founders)
            # Skip generic role@ if we already have personal emails from founders
            is_personal = not label.startswith("role:")
            is_role_inbox = label.startswith("role:")
            has_personal = any(c.email_label not in ("", ) and not c.email_label.startswith("role:")
                               for c in contacts
                               if c.domain == domain and c.company_id == row.get("company_id"))

            if is_role_inbox and has_personal:
                # Already have personal emails — only keep the most useful role inboxes
                if label not in ("role:founders", "role:cto", "role:ceo"):
                    continue

            founder_name = ""
            if is_personal:
                # Try to match email back to a founder name
                local = email.split("@")[0]
                for f in founders:
                    fn = _normalise(f.first)
                    ln = _normalise(f.last)
                    if local in (fn, f"{fn}.{ln}", f"{fn}{ln}", f"{fn[0]}{ln}",
                                 f"{fn[0]}.{ln}", ln):
                        founder_name = f.full
                        break

            contacts.append(ContactRow(
                company=company,
                company_id=str(row.get("company_id") or ""),
                batch=batch,
                team_size=str(team_size or ""),
                website=website,
                domain=domain,
                founder_name=founder_name,
                email=email,
                email_label=label,
                status=result.status,
                mx_ok=result.mx_ok,
                smtp_ok=result.smtp_ok,
                catch_all=result.catch_all,
                observed_at=now,
            ))

    return contacts


def write_csv(contacts: list[ContactRow], out_dir: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / "contacts.csv"
    fields = ["company", "company_id", "batch", "team_size", "website", "domain",
              "founder_name", "email", "email_label", "status",
              "mx_ok", "smtp_ok", "catch_all", "observed_at"]
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in contacts:
            w.writerow({k: getattr(c, k) for k in fields})
    return p


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Find likely email contacts for matched YC startups (no API key needed)."
    )
    p.add_argument("--in", dest="infile", required=True,
                   help="Path to matches.csv (or matches.jsonl)")
    p.add_argument("--out", default="out", help="Output directory")
    p.add_argument("--cache", default=".cache/enrich_cache.sqlite",
                   help="Cache file path")
    p.add_argument("--smtp-verify", action="store_true",
                   help="Enable SMTP probe per address (slower; catches more catch-all)")
    p.add_argument("--no-accept-all", action="store_true",
                   help="Drop addresses where domain is catch-all / unverifiable")
    p.add_argument("--mail-from", default="verify@example.com",
                   help="Envelope sender for SMTP probes (use a domain you control)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not _HAVE_DNS:
        log.warning("dnspython not installed — MX lookup disabled. "
                    "Run: pip install dnspython")

    contacts = run(
        infile=args.infile,
        out_dir=args.out,
        cache_path=args.cache,
        smtp_enabled=args.smtp_verify,
        no_accept_all=args.no_accept_all,
        mail_from=args.mail_from,
    )

    path = write_csv(contacts, args.out)

    valid = sum(1 for c in contacts if c.status == "valid")
    accept_all = sum(1 for c in contacts if c.status == "accept_all")
    companies_hit = len({c.company for c in contacts})

    print(f"\nContacts found: {len(contacts)} across {companies_hit} companies")
    print(f"  valid (SMTP confirmed):  {valid}")
    print(f"  accept_all (MX ok, individual unverified): {accept_all}")
    print(f"  wrote {path}")

    if not args.smtp_verify:
        print("\nNote: most results are 'accept_all' (domain has MX but individual")
        print("mailbox not probed). For tiny startups, {first}@domain is almost")
        print("always correct. Add --smtp-verify to probe individual mailboxes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
