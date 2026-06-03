"""End-to-end orchestration and output.

The whole strategy is a funnel:

  Stage 1  cheap, static, no-credential   -> the eligible company universe
           (in-window batch AND active AND under the headcount cap)
  Stage 2  live, authoritative            -> all currently-open software roles
  Stage 3  inner join on company_id       -> companies that satisfy every filter
  Stage 4  emit JSONL + CSV with provenance and an observed-at timestamp

Each stage's logic is a pure function over already-parsed models, so the
join/classify behaviour is unit-tested without any network access. Only the two
source objects touch the wire.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .classify import SoftwareClassifier
from .config import Config
from .models import Company, Job, MatchedCompany
from .sources import WaasJobsSource, YcOssSource

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    eligible_companies: int
    open_software_jobs: int
    matched_companies: int
    matched_jobs: int
    as_of: str
    cutoff: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def join_jobs_to_companies(
    companies: dict[int, Company],
    jobs: list[Job],
    classifier: SoftwareClassifier,
    observed_at: _dt.datetime,
) -> list[MatchedCompany]:
    """Inner-join software jobs onto eligible companies, grouped per company.

    A company appears in the output iff it is in the eligible universe *and* has
    at least one currently-open role the classifier accepts as software.
    """
    by_company: dict[int, list[Job]] = defaultdict(list)
    for job in jobs:
        if job.company_id in companies and classifier.is_software(job):
            by_company[job.company_id].append(job)

    matched: list[MatchedCompany] = []
    for cid, comp_jobs in by_company.items():
        comp_jobs.sort(key=lambda j: (-(j.created_at or 0), j.title))
        matched.append(
            MatchedCompany(
                company=companies[cid], jobs=comp_jobs, observed_at=observed_at
            )
        )
    # Newest-hiring, then alphabetical, for a stable and useful ordering.
    matched.sort(key=lambda m: (-max((j.created_at or 0) for j in m.jobs), m.company.name.lower()))
    return matched


class Pipeline:
    def __init__(self, cfg: Config, yc: YcOssSource, jobs_src: WaasJobsSource) -> None:
        self.cfg = cfg
        self.yc = yc
        self.jobs_src = jobs_src
        self.classifier = SoftwareClassifier.from_config(cfg)

    def run(self) -> tuple[list[MatchedCompany], RunSummary]:
        observed_at = _dt.datetime.now(_dt.timezone.utc)

        companies = self.yc.eligible_companies()          # Stage 1
        jobs = self.jobs_src.software_jobs()              # Stage 2
        matched = join_jobs_to_companies(                 # Stage 3
            companies, jobs, self.classifier, observed_at
        )

        summary = RunSummary(
            eligible_companies=len(companies),
            open_software_jobs=len(jobs),
            matched_companies=len(matched),
            matched_jobs=sum(len(m.jobs) for m in matched),
            as_of=self.cfg.effective_as_of.isoformat(),
            cutoff=self.cfg.cutoff_date.isoformat(),
        )
        log.info("Run summary: %s", summary.as_dict())
        return matched, summary


# -- output -----------------------------------------------------------------
def write_outputs(
    matched: list[MatchedCompany], summary: RunSummary, cfg: Config
) -> list[Path]:
    out_dir = Path(cfg.output.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if cfg.output.jsonl:
        p = out_dir / "matches.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for m in matched:
                f.write(json.dumps(m.model_dump(mode="json"), ensure_ascii=False) + "\n")
        written.append(p)

    if cfg.output.csv:
        p = out_dir / "matches.csv"
        rows = [r for m in matched for r in m.to_rows()]
        fields = [
            "company", "batch", "status", "team_size", "company_id",
            "job_title", "role", "job_type", "job_locations", "job_posted",
            "company_one_liner", "company_website", "yc_profile",
            "company_locations", "industries", "observed_at",
        ]
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        written.append(p)

    manifest = out_dir / "run_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "summary": summary.as_dict(),
                "filters": {
                    "years_back": cfg.years_back,
                    "max_team_size": cfg.max_team_size,
                    "require_active": cfg.require_active,
                    "exclude_top_company": cfg.exclude_top_company,
                    "software_roles": cfg.software_roles,
                    "title_fallback_enabled": cfg.title_fallback.enabled,
                },
            },
            indent=2,
        )
    )
    written.append(manifest)
    return written
