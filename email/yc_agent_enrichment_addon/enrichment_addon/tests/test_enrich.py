"""Offline tests for the enrichment layer.

No network. The provider client is exercised through a fake that returns the
exact response shapes documented by Hunter's API. We test: domain parsing,
match-file reading (in the user's real CSV format), role classification, the
"verified only" policy (incl. the catch-all demotion), suppression, and the
role-inbox fallback.

Run:  python -m pytest -q   (or)   python tests/test_enrich.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yc_agent.enrich.config import EnrichmentConfig  # noqa: E402
from yc_agent.enrich.models import RoleBucket, VerificationStatus  # noqa: E402
from yc_agent.enrich.pipeline import (  # noqa: E402
    EnrichmentPipeline, domain_from_website, load_suppression,
    passes_policy, read_match_companies,
)
from yc_agent.enrich.providers import normalise_status  # noqa: E402
from yc_agent.enrich.seniority import classify_position  # noqa: E402

# A slice of the user's real matches.csv.
SAMPLE_CSV = (
    "company,batch,status,team_size,company_id,job_title,role,job_type,job_locations,"
    "job_posted,company_one_liner,company_website,yc_profile,company_locations,industries,observed_at\n"
    "14.ai,Winter 2024,Active,3,29341,Founding engineer,,Full-time,\"San Francisco, CA, US\",,"
    "AI engine,https://14.ai,https://www.ycombinator.com/companies/14-ai,\"San Francisco, CA, USA\","
    "\"B2B, Infrastructure\",2026-06-02T13:30:24+00:00\n"
    "Accend,Summer 2023,Active,18,28786,Full Stack Engineer,,Full-time,\"San Francisco, CA, US\",,"
    "AI agents,https://www.withaccend.com/,https://www.ycombinator.com/companies/accend,,"
    "Fintech,2026-06-02T13:30:24+00:00\n"
    # second Accend row (different job) must NOT create a duplicate company.
    "Accend,Summer 2023,Active,18,28786,Backend Engineer,,Full-time,Remote,,"
    "AI agents,https://www.withaccend.com/,https://www.ycombinator.com/companies/accend,,"
    "Fintech,2026-06-02T13:30:24+00:00\n"
)


# --- domain parsing ---------------------------------------------------------
def test_domain_from_website():
    assert domain_from_website("https://www.withaccend.com/") == "withaccend.com"
    assert domain_from_website("https://14.ai") == "14.ai"
    assert domain_from_website("afterquery.com") == "afterquery.com"
    assert domain_from_website("") is None
    assert domain_from_website("not a url") is None


def test_read_match_csv_dedupes_companies():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "matches.csv"
        p.write_text(SAMPLE_CSV, encoding="utf-8")
        rows = read_match_companies(p)
    assert len(rows) == 2  # Accend appears twice in the file, once here
    names = {r["company"] for r in rows}
    assert names == {"14.ai", "Accend"}


# --- status mapping (real Hunter values) ------------------------------------
def test_status_normalisation():
    assert normalise_status("valid") is VerificationStatus.VALID
    assert normalise_status("accept_all") is VerificationStatus.ACCEPT_ALL
    assert normalise_status("invalid") is VerificationStatus.INVALID
    assert normalise_status("webmail") is VerificationStatus.WEBMAIL
    assert normalise_status("unknown") is VerificationStatus.UNKNOWN
    assert normalise_status("valid", block=True) is VerificationStatus.UNKNOWN
    assert normalise_status(None) is VerificationStatus.UNKNOWN


# --- role classification ----------------------------------------------------
def test_classify_position_priority():
    assert classify_position("Founder & CEO") is RoleBucket.CEO          # CEO wins
    assert classify_position("Co-Founder & CTO") is RoleBucket.CTO
    assert classify_position("Founder") is RoleBucket.FOUNDER
    assert classify_position("Head of People") is RoleBucket.HR
    assert classify_position("Technical Recruiter") is RoleBucket.TALENT
    assert classify_position("VP of Engineering") is RoleBucket.ENGINEERING_LEAD
    assert classify_position("Software Engineer") is RoleBucket.OTHER     # IC, not a lead
    assert classify_position("") is RoleBucket.OTHER


# --- policy -----------------------------------------------------------------
def test_policy_verified_only_default():
    cfg = EnrichmentConfig()
    assert passes_policy(VerificationStatus.VALID, 95, cfg) is True
    assert passes_policy(VerificationStatus.ACCEPT_ALL, 95, cfg) is False  # catch-all excluded
    assert passes_policy(VerificationStatus.INVALID, 95, cfg) is False
    assert passes_policy(VerificationStatus.UNKNOWN, 95, cfg) is False
    assert passes_policy(VerificationStatus.WEBMAIL, 95, cfg) is False


def test_policy_include_accept_all_opt_in():
    cfg = EnrichmentConfig(include_accept_all=True)
    assert passes_policy(VerificationStatus.ACCEPT_ALL, 80, cfg) is True


def test_policy_confidence_floor():
    cfg = EnrichmentConfig(min_confidence=90)
    assert passes_policy(VerificationStatus.VALID, 80, cfg) is False
    assert passes_policy(VerificationStatus.VALID, 92, cfg) is True


# --- suppression ------------------------------------------------------------
def test_suppression_loading():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "supp.txt"
        p.write_text("# do not contact\nNo@Example.com\n\nfoo@bar.com\n", encoding="utf-8")
        s = load_suppression(str(p))
    assert s == {"no@example.com", "foo@bar.com"}
    assert load_suppression(None) == set()


# --- pipeline with a fake provider ------------------------------------------
class FakeHunter:
    """Returns the documented Hunter response shapes; records calls."""

    def __init__(self, domain_emails: dict, verifier_status: dict | None = None):
        self._emails = domain_emails
        self._verify = verifier_status or {}
        self.verifier_calls: list[str] = []

    def domain_search(self, domain, **kw):
        return {"domain": domain, "pattern": "{first}", "emails": self._emails.get(domain, [])}

    def email_verifier(self, email):
        self.verifier_calls.append(email)
        return self._verify.get(email, {"status": "unknown"})


def _person(value, position, status, *, ptype="personal", conf=90, accept_all=False,
            first="A", last="B"):
    return {"value": value, "type": ptype, "confidence": conf, "first_name": first,
            "last_name": last, "position": position, "accept_all": accept_all,
            "verification": {"status": status, "date": "2026-05-01"}}


def test_pipeline_keeps_only_verified_wanted_roles():
    emails = {
        "acme.com": [
            _person("ceo@acme.com", "Founder & CEO", "valid"),
            _person("eng@acme.com", "Software Engineer", "valid"),       # IC role -> dropped
            _person("sales@acme.com", "Account Executive", "valid"),     # not wanted -> dropped
            _person("cto@acme.com", "CTO", "invalid"),                   # not verified -> dropped
        ]
    }
    cfg = EnrichmentConfig()
    pipe = EnrichmentPipeline(cfg, FakeHunter(emails), verifier=None)
    [cc] = pipe.run([{"company": "Acme", "company_id": 1, "website": "https://acme.com"}])
    got = {(c.email, c.role_bucket.value) for c in cc.contacts}
    assert got == {("ceo@acme.com", "ceo")}


def test_pipeline_demotes_catch_all_even_if_valid():
    emails = {"acme.com": [_person("ceo@acme.com", "CEO", "valid", accept_all=True)]}
    cfg = EnrichmentConfig()  # include_accept_all defaults False
    pipe = EnrichmentPipeline(cfg, FakeHunter(emails), verifier=None)
    [cc] = pipe.run([{"company": "Acme", "company_id": 1, "website": "https://acme.com"}])
    assert cc.contacts == []  # valid+catch-all -> ACCEPT_ALL -> excluded


def test_pipeline_role_inbox_fallback_when_no_person():
    # No people returned; fallback should verify role inboxes via the verifier API.
    fake = FakeHunter(
        domain_emails={"acme.com": []},
        verifier_status={
            "careers@acme.com": {"status": "valid", "score": 88, "smtp_check": True, "mx_records": True},
            # everything else unknown by default
        },
    )
    cfg = EnrichmentConfig()
    pipe = EnrichmentPipeline(cfg, fake, verifier=None)
    [cc] = pipe.run([{"company": "Acme", "company_id": 1, "website": "https://acme.com"}])
    emails = {c.email for c in cc.contacts}
    assert emails == {"careers@acme.com"}
    assert cc.contacts[0].source == "role_inbox"
    assert any("role inbox" in n for n in cc.notes)


def test_pipeline_respects_suppression(tmp_path=None):
    emails = {"acme.com": [_person("ceo@acme.com", "CEO", "valid")]}
    cfg = EnrichmentConfig()
    pipe = EnrichmentPipeline(cfg, FakeHunter(emails), verifier=None)
    pipe.suppressed = {"ceo@acme.com"}  # simulate a loaded suppression list
    [cc] = pipe.run([{"company": "Acme", "company_id": 1, "website": "https://acme.com"}])
    assert all(c.email != "ceo@acme.com" for c in cc.contacts)


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
