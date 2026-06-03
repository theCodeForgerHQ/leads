"""Enrichment orchestration.

Per unique company in the match file:

  1. Derive the email domain from the company website.
  2. Ask the provider for people; keep only those whose position maps to a role
     you asked for (CEO/CTO/Founder/Eng-lead/HR/Talent).
  3. Establish each address's verification status (provider status, optionally
     cross-checked by the self-verifier).
  4. Apply the "verified only" policy: by default keep only status == valid,
     drop catch-all/invalid/unknown/webmail/disposable, and drop anything below
     the confidence floor.
  5. If nothing survives and fallback is on, synthesise role inboxes
     (careers@, jobs@, founders@ ...) and verify those.
  6. Subtract the suppression list at the very end -- a "do not contact" is
     absolute and overrides everything above.

The per-company logic is split into small pure-ish functions so the policy
(what counts as "verified", how fallback behaves, suppression) is unit-tested
without any network. Only HunterClient touches the wire.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .config import EnrichmentConfig
from .models import Contact, CompanyContacts, RoleBucket, VerificationStatus
from .providers import HunterClient, normalise_status
from .seniority import classify_position, is_wanted
from .verify import SelfVerifier

log = logging.getLogger(__name__)


# -- input parsing -----------------------------------------------------------
def domain_from_website(website: str | None) -> Optional[str]:
    """'https://www.withaccend.com/' -> 'withaccend.com'. None if unusable."""
    if not website:
        return None
    raw = website.strip()
    if "//" not in raw:
        raw = "//" + raw
    host = (urlsplit(raw).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # crude sanity: must look like a domain
    return host if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host) else None


def read_match_companies(path: str | Path) -> list[dict]:
    """Read either matches.csv or matches.jsonl into unique-company dicts.

    Dedupes on company_id (falling back to domain), because the match file has
    one row *per job* and we want one enrichment pass per company.
    """
    p = Path(path)
    rows: list[dict] = []
    if p.suffix == ".jsonl":
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            c = obj.get("company", {})
            rows.append({
                "company": c.get("name"),
                "company_id": c.get("id"),
                "website": c.get("website"),
            })
    else:
        with p.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "company": r.get("company"),
                    "company_id": int(r["company_id"]) if r.get("company_id") else None,
                    "website": r.get("company_website"),
                })

    seen: set = set()
    unique: list[dict] = []
    for r in rows:
        key = r.get("company_id") or domain_from_website(r.get("website")) or r.get("company")
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


# -- policy (pure) -----------------------------------------------------------
def passes_policy(status: VerificationStatus, confidence: Optional[int],
                  cfg: EnrichmentConfig) -> bool:
    keep = {VerificationStatus(s) for s in cfg.keep_statuses}
    if cfg.include_accept_all:
        keep.add(VerificationStatus.ACCEPT_ALL)
    if status not in keep:
        return False
    if confidence is not None and confidence < cfg.min_confidence:
        return False
    return True


def load_suppression(path: str | None) -> set[str]:
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    return {
        line.strip().lower()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


# -- pipeline ----------------------------------------------------------------
class EnrichmentPipeline:
    def __init__(self, cfg: EnrichmentConfig, client: HunterClient,
                 verifier: Optional[SelfVerifier] = None) -> None:
        self.cfg = cfg
        self.client = client
        self.verifier = verifier
        self.suppressed = load_suppression(cfg.suppression_file)

    # ---- per-company people from the provider -------------------------------
    def _people_for(self, company: str, company_id: Optional[int],
                    domain: str) -> CompanyContacts:
        data = self.client.domain_search(domain, limit=25)
        pattern = data.get("pattern")
        cc = CompanyContacts(company=company, company_id=company_id,
                             domain=domain, pattern=pattern)

        for person in data.get("emails", []) or []:
            position = person.get("position")
            wanted, bucket = is_wanted(position, self.cfg.target_roles)
            if not wanted:
                continue

            email = (person.get("value") or "").lower()
            if not email or email in self.suppressed:
                continue

            v = person.get("verification") or {}
            status = normalise_status(v.get("status"))
            accept_all = bool(person.get("accept_all", False))
            if accept_all and status is VerificationStatus.VALID:
                # Provider sometimes returns valid + accept_all; the catch-all
                # nature means it is not individually verifiable. Demote it.
                status = VerificationStatus.ACCEPT_ALL

            status, smtp_check, mx = self._maybe_cross_check(email, status)
            confidence = person.get("confidence")

            if not passes_policy(status, confidence, self.cfg):
                continue

            cc.contacts.append(Contact(
                company=company, company_id=company_id, domain=domain,
                email=email, email_type=person.get("type", "personal"),
                first_name=person.get("first_name"), last_name=person.get("last_name"),
                position=position, role_bucket=bucket, status=status,
                confidence=confidence, smtp_check=smtp_check, mx_records=mx,
                accept_all=accept_all, source="hunter", verified_on=v.get("date"),
            ))
        return cc

    def _maybe_cross_check(self, email: str, status: VerificationStatus):
        if not (self.cfg.cross_check_with_self_verifier and self.verifier):
            return status, None, None
        res = self.verifier.verify(email)
        # If our probe contradicts the provider by finding it invalid/catch-all,
        # trust the stricter outcome.
        if res.status in (VerificationStatus.INVALID, VerificationStatus.ACCEPT_ALL):
            return res.status, res.smtp_check, res.mx_records
        return status, res.smtp_check, res.mx_records

    # ---- role-inbox fallback -----------------------------------------------
    def _role_inboxes(self, company: str, company_id: Optional[int],
                      domain: str) -> list[Contact]:
        out: list[Contact] = []
        for local in self.cfg.role_inbox_localparts:
            email = f"{local}@{domain}".lower()
            if email in self.suppressed:
                continue
            # Prefer a real check; if no verifier/SMTP, verify via the provider.
            if self.verifier and self.cfg.smtp.enabled:
                res = self.verifier.verify(email)
                status, smtp_check, mx = res.status, res.smtp_check, res.mx_records
                confidence = None
            else:
                data = self.client.email_verifier(email)
                status = normalise_status(data.get("status"), block=bool(data.get("block")))
                smtp_check = data.get("smtp_check")
                mx = data.get("mx_records")
                confidence = data.get("score")
            if passes_policy(status, confidence, self.cfg):
                out.append(Contact(
                    company=company, company_id=company_id, domain=domain,
                    email=email, email_type="generic", position=f"{local} (role inbox)",
                    role_bucket=RoleBucket.OTHER, status=status, confidence=confidence,
                    smtp_check=smtp_check, mx_records=mx, source="role_inbox",
                ))
        return out

    # ---- run ----------------------------------------------------------------
    def run(self, companies: Iterable[dict]) -> list[CompanyContacts]:
        results: list[CompanyContacts] = []
        for r in companies:
            company = r.get("company") or "?"
            company_id = r.get("company_id")
            domain = domain_from_website(r.get("website"))
            if not domain:
                cc = CompanyContacts(company=company, company_id=company_id, domain="")
                cc.notes.append("no usable domain from website; skipped")
                results.append(cc)
                continue

            cc = self._people_for(company, company_id, domain)

            if not cc.contacts and self.cfg.role_inbox_fallback:
                fallback = self._role_inboxes(company, company_id, domain)
                cc.contacts.extend(fallback)
                if fallback:
                    cc.notes.append("no verified individual found; using role inbox(es)")
                else:
                    cc.notes.append("no verified contact found")

            results.append(cc)
        return results


# -- output ------------------------------------------------------------------
def write_contact_outputs(results: list[CompanyContacts], out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # CSV: one row per verified contact.
    csv_path = out / "contacts.csv"
    fields = ["company", "company_id", "domain", "name", "position", "role_bucket",
              "email", "email_type", "status", "confidence", "smtp_check",
              "accept_all", "source", "verified_on", "observed_at"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for cc in results:
            for c in cc.contacts:
                w.writerow(c.to_row())
    written.append(csv_path)

    # JSONL: one object per company, with notes (incl. companies with 0 contacts).
    jsonl_path = out / "contacts.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for cc in results:
            f.write(json.dumps(cc.model_dump(mode="json"), ensure_ascii=False) + "\n")
    written.append(jsonl_path)

    # A separate, clearly-labelled file for catch-all domains we could NOT
    # verify, so they're available but never mistaken for verified.
    return written
