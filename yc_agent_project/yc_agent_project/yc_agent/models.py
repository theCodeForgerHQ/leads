"""Typed domain models.

Every record that enters or leaves the pipeline is validated against one of
these models. This is deliberate: the two upstream sources (the yc-oss mirror
and YC's Algolia job index) are *not* governed by a contract we control, so
schema drift upstream is the single most likely cause of silent corruption.
Validating at the boundary turns that failure mode into a loud, early error
instead of a quietly wrong result file.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Company(BaseModel):
    """A YC company as described by the company-level index (via yc-oss).

    These fields change slowly (a company's batch, status and rough headcount
    do not move intraday), which is why we are comfortable sourcing them from a
    once-a-day mirror rather than a live call.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    slug: str
    name: str
    batch: str  # display form, e.g. "Summer 2024"
    status: str  # Active | Acquired | Public | Inactive
    team_size: Optional[int] = None
    top_company: bool = False
    one_liner: Optional[str] = None
    website: Optional[str] = None
    all_locations: Optional[str] = None
    industries: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    stage: Optional[str] = None
    yc_url: Optional[str] = None
    is_hiring: bool = False

    # Filled in by config.batch_to_key(); kept separate from the raw display
    # string so we never lose the original value.
    batch_key: Optional[str] = None


class Job(BaseModel):
    """A single open role from the live Work-at-a-Startup job index.

    Presence of a row in that index is the authoritative signal that the role
    is *currently* open, which is how we satisfy the "currently hiring"
    requirement without trusting a company-level boolean flag that can lag.
    """

    model_config = ConfigDict(extra="ignore")

    object_id: str
    company_id: int
    title: str
    role: Optional[str] = None  # YC's own role facet, e.g. "eng"
    job_type: Optional[str] = None  # e.g. "fulltime", "intern", "contract"
    company_name: Optional[str] = None
    company_website: Optional[str] = None
    locations: list[str] = Field(default_factory=list)
    created_at: Optional[int] = None  # unix seconds, if exposed by the index

    @property
    def location_str(self) -> str:
        return "; ".join(self.locations)

    @property
    def created_at_iso(self) -> Optional[str]:
        if self.created_at is None:
            return None
        return _dt.datetime.fromtimestamp(
            self.created_at, tz=_dt.timezone.utc
        ).date().isoformat()


class MatchedCompany(BaseModel):
    """A company that passed every filter, with the software roles that matched."""

    model_config = ConfigDict(extra="ignore")

    company: Company
    jobs: list[Job]
    observed_at: _dt.datetime  # when the live job data was read (UTC)

    def to_rows(self) -> list[dict]:
        """Flatten to one row per matching job for CSV output.

        A job-per-row shape is what an operator actually wants from a hiring
        search: each line is an applyable role with the company context repeated
        alongside it.
        """
        rows = []
        c = self.company
        for j in self.jobs:
            rows.append(
                {
                    "company": c.name,
                    "batch": c.batch,
                    "status": c.status,
                    "team_size": c.team_size,
                    "company_id": c.id,
                    "job_title": j.title,
                    "role": j.role,
                    "job_type": j.job_type,
                    "job_locations": j.location_str,
                    "job_posted": j.created_at_iso,
                    "company_one_liner": c.one_liner,
                    "company_website": c.website,
                    "yc_profile": c.yc_url,
                    "company_locations": c.all_locations,
                    "industries": ", ".join(c.industries),
                    "observed_at": self.observed_at.isoformat(timespec="seconds"),
                }
            )
        return rows
