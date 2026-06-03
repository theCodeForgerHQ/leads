"""Offline tests. No network: the source objects are exercised separately; here
we test the pure logic (batch window, startup guards, role classification, and
the job->company join) against fixtures taken from real API responses.

Run:  python -m pytest -q     (or)     python tests/test_agent.py
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yc_agent.classify import SoftwareClassifier  # noqa: E402
from yc_agent.config import Config, load_config  # noqa: E402
from yc_agent.models import Company, Job  # noqa: E402
from yc_agent.pipeline import join_jobs_to_companies  # noqa: E402
from yc_agent.sources import YcOssSource  # noqa: E402

AS_OF = _dt.date(2026, 6, 2)


def cfg(**over) -> Config:
    c = load_config(None)
    c.as_of = AS_OF
    for k, v in over.items():
        setattr(c, k, v)
    return c


# --- batch window -----------------------------------------------------------
def test_cutoff_is_three_years_back_month_precise():
    assert cfg().cutoff_date == _dt.date(2023, 6, 1)


def test_batch_window_boundaries():
    c = cfg()
    assert c.batch_in_window("Summer 2023") is True     # exactly on the boundary
    assert c.batch_in_window("Winter 2023") is False     # Jan 2023, too old
    assert c.batch_in_window("Spring 2026") is True
    assert c.batch_in_window("Fall 2026") is True
    assert c.batch_in_window("Winter 2012") is False
    assert c.batch_in_window("Unspecified") is False     # unparseable -> excluded


def test_batch_to_key():
    assert cfg().batch_to_key("Summer 2024") == "summer-2024"
    assert cfg().batch_to_key("Unspecified") is None


# --- startup guards (real company shapes) -----------------------------------
# Gusto, straight from hiring.json: old batch + huge headcount + top_company.
GUSTO = {
    "id": 24, "slug": "gusto", "name": "Gusto", "batch": "Winter 2012",
    "status": "Active", "team_size": 2400, "top_company": True,
}
# A plausible recent, small, active startup.
RECENT_SMALL = {
    "id": 1001, "slug": "acme", "name": "Acme", "batch": "Summer 2024",
    "status": "Active", "team_size": 8, "top_company": False,
}
RECENT_ACQUIRED = {
    "id": 1002, "slug": "bought", "name": "Bought", "batch": "Winter 2025",
    "status": "Acquired", "team_size": 12, "top_company": False,
}


def _guard(raw, **over):
    src = YcOssSource(cfg(**over), session=None)  # session unused by _passes_guards
    comp = src._to_company(raw)
    return src._passes_guards(comp)


def test_guards_reject_mnc_scale_company():
    # Even though Gusto's status is Active, the headcount cap removes it; and it
    # is not in-window anyway.
    assert _guard(GUSTO) is False
    assert cfg().batch_in_window(GUSTO["batch"]) is False


def test_guards_keep_recent_small_active():
    assert _guard(RECENT_SMALL) is True


def test_guards_reject_acquired():
    assert _guard(RECENT_ACQUIRED) is False


def test_exclude_top_company_opt_in():
    assert _guard(GUSTO, exclude_top_company=True) is False
    assert _guard(RECENT_SMALL, exclude_top_company=True) is True


# --- software classification ------------------------------------------------
def job(title, role=None, cid=1, oid="o", jt="fulltime"):
    return Job(object_id=oid, company_id=cid, title=title, role=role, job_type=jt)


def test_classifier_accepts_eng_role():
    clf = SoftwareClassifier.from_config(cfg())
    assert clf.is_software(job("Backend Engineer", role="eng")) is True


def test_classifier_rejects_non_software_role():
    clf = SoftwareClassifier.from_config(cfg())
    assert clf.is_software(job("Account Executive", role="sales")) is False


def test_classifier_excludes_hardware_even_if_eng_tagged():
    clf = SoftwareClassifier.from_config(cfg())
    # A hard-tech startup may file mechanical/EE under the eng facet; "software
    # related" must still reject these.
    assert clf.is_software(job("Mechanical Engineer", role="eng")) is False
    assert clf.is_software(job("Electrical Engineer", role="eng")) is False
    assert clf.is_software(job("Sales Engineer", role="eng")) is False


def test_classifier_title_fallback_catches_mistagged_software():
    clf = SoftwareClassifier.from_config(cfg())
    assert clf.is_software(job("Founding Engineer", role="other")) is True
    assert clf.is_software(job("Full Stack Developer", role=None)) is True


def test_classifier_fallback_can_be_disabled():
    c = cfg()
    c.title_fallback.enabled = False
    clf = SoftwareClassifier.from_config(c)
    # Without the eng role and with fallback off, a software-ish title is ignored.
    assert clf.is_software(job("Full Stack Developer", role="other")) is False


# --- the join ---------------------------------------------------------------
def test_join_is_inner_and_role_filtered():
    companies = {
        1001: Company(**RECENT_SMALL),
        2002: Company(id=2002, slug="dev", name="DevCo", batch="Spring 2025",
                      status="Active", team_size=5),
    }
    jobs = [
        job("Senior Software Engineer", role="eng", cid=1001, oid="a"),
        job("Designer", role="design", cid=1001, oid="b"),     # dropped: not software
        job("Backend Engineer", role="eng", cid=2002, oid="c"),
        job("Platform Engineer", role="eng", cid=9999, oid="d"),  # dropped: not eligible
    ]
    clf = SoftwareClassifier.from_config(cfg())
    matched = join_jobs_to_companies(companies, jobs, clf, _dt.datetime.now(_dt.timezone.utc))

    by_name = {m.company.name: m for m in matched}
    assert set(by_name) == {"Acme", "DevCo"}            # 9999 excluded
    assert [j.title for j in by_name["Acme"].jobs] == ["Senior Software Engineer"]  # designer dropped
    assert len(by_name["DevCo"].jobs) == 1


def test_matched_company_csv_rows():
    companies = {1001: Company(**RECENT_SMALL)}
    jobs = [job("Software Engineer", role="eng", cid=1001, oid="a")]
    clf = SoftwareClassifier.from_config(cfg())
    matched = join_jobs_to_companies(companies, jobs, clf, _dt.datetime.now(_dt.timezone.utc))
    rows = matched[0].to_rows()
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"
    assert rows[0]["job_title"] == "Software Engineer"
    assert rows[0]["batch"] == "Summer 2024"


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
