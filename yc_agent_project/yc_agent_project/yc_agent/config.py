"""Configuration and the batch-window predicate.

All policy lives here so the pipeline code stays mechanical. The one piece of
genuine logic in this module is translating the human filter "funded in the
past N years" into the concrete set of YC batches that qualify, which is done
by mapping each batch to an approximate start date and comparing to a cutoff.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

_SEASON_RE = re.compile(r"^(winter|spring|summer|fall)\s+(\d{4})$", re.IGNORECASE)


class TitleFallback(BaseModel):
    enabled: bool = True
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)


class AlgoliaCfg(BaseModel):
    app_id: str = "45BWZJ1SGC"
    # Public, search-only key served by the WaaS site to every browser. It can
    # rotate; leave null to have the agent fetch the current one at runtime.
    api_key: Optional[str] = None
    jobs_index: str = "WaaSPublicCompanyJob_created_at_desc_production"
    bootstrap_url: str = "https://www.workatastartup.com"
    bootstrap_from_page: bool = True


class HttpCfg(BaseModel):
    user_agent: str = "yc-hiring-agent/1.0 (+set-a-real-contact@example.com)"
    min_interval_seconds: float = 0.34  # ~3 req/s ceiling, global
    timeout_seconds: float = 20.0
    max_retries: int = 5
    backoff_factor: float = 0.8
    cache_ttl_seconds: int = 86_400  # 1 day; matches the mirror's refresh cadence
    cache_path: str = ".cache/http_cache.sqlite"
    respect_robots: bool = True


class OutputCfg(BaseModel):
    dir: str = "out"
    jsonl: bool = True
    csv: bool = True


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- eligibility window -------------------------------------------------
    years_back: int = 3
    as_of: Optional[_dt.date] = None  # None => today (UTC)
    season_start_month: dict[str, int] = Field(
        default_factory=lambda: {"winter": 1, "spring": 4, "summer": 6, "fall": 9}
    )

    # --- "truly a startup, not an MNC" guards ------------------------------
    max_team_size: int = 500
    require_active: bool = True
    exclude_top_company: bool = False

    # --- "software related role" definition --------------------------------
    software_roles: list[str] = Field(default_factory=lambda: ["eng"])
    title_fallback: TitleFallback = Field(default_factory=TitleFallback)

    # --- sources / transport / output --------------------------------------
    yc_oss_base: str = "https://yc-oss.github.io/api"
    algolia: AlgoliaCfg = Field(default_factory=AlgoliaCfg)
    http: HttpCfg = Field(default_factory=HttpCfg)
    output: OutputCfg = Field(default_factory=OutputCfg)

    # ----------------------------------------------------------------------
    @property
    def effective_as_of(self) -> _dt.date:
        return self.as_of or _dt.datetime.now(_dt.timezone.utc).date()

    @property
    def cutoff_date(self) -> _dt.date:
        """First day that still counts as 'within the window'.

        Month-precise so a Summer batch exactly N years ago lands on the
        boundary and is included.
        """
        a = self.effective_as_of
        return _dt.date(a.year - self.years_back, a.month, 1)

    def batch_to_key(self, batch_display: str) -> Optional[str]:
        """'Summer 2024' -> 'summer-2024'. None if unparseable (e.g. 'Unspecified')."""
        m = _SEASON_RE.match((batch_display or "").strip())
        if not m:
            return None
        return f"{m.group(1).lower()}-{m.group(2)}"

    def batch_start(self, batch_display: str) -> Optional[_dt.date]:
        m = _SEASON_RE.match((batch_display or "").strip())
        if not m:
            return None
        season, year = m.group(1).lower(), int(m.group(2))
        month = self.season_start_month.get(season)
        if not month:
            return None
        return _dt.date(year, month, 1)

    def batch_in_window(self, batch_display: str) -> bool:
        start = self.batch_start(batch_display)
        return start is not None and start >= self.cutoff_date


_DEFAULT_INCLUDE = [
    r"\bsoftware\b",
    r"\bback[- ]?end\b",
    r"\bfront[- ]?end\b",
    r"\bfull[- ]?stack\b",
    r"\bswe\b",
    r"\bsde\b",
    r"\bdeveloper\b",
    r"\bprogrammer\b",
    r"\bdevops\b",
    r"\bsre\b",
    r"\bsite reliability\b",
    r"\bplatform engineer",
    r"\binfrastructure engineer",
    r"\bsystems? engineer",
    r"\bembedded\b",
    r"\bfirmware\b",
    r"\b(ios|android|mobile) engineer",
    r"\bweb engineer",
    r"\bsecurity engineer",
    r"\b(machine learning|ml|ai) engineer",
    r"\bapplied (ai|ml)\b",
    r"\bdata engineer",
    r"\bfounding engineer",
]

# Engineering titles that are explicitly NOT software development, so that a
# hard-tech startup's mechanical/EE roles do not slip in under a broad eng tag.
_DEFAULT_EXCLUDE = [
    r"\bmechanical engineer",
    r"\belectrical engineer",
    r"\bhardware engineer",
    r"\bcivil engineer",
    r"\bbiomedical engineer",
    r"\boptical engineer",
    r"\bmanufacturing engineer",
    r"\bprocess engineer",
    r"\bchemical engineer",
    r"\baerospace engineer",
    r"\bsales engineer",
    r"\bsolutions engineer",
    r"\bfield engineer",
]


def load_config(path: Optional[str | Path]) -> Config:
    """Load YAML config, falling back to defaults for any unset key."""
    data: dict = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        data = yaml.safe_load(p.read_text()) or {}
    cfg = Config(**data)
    # Backfill regex defaults if the user did not specify any.
    if not cfg.title_fallback.include_patterns:
        cfg.title_fallback.include_patterns = list(_DEFAULT_INCLUDE)
    if not cfg.title_fallback.exclude_patterns:
        cfg.title_fallback.exclude_patterns = list(_DEFAULT_EXCLUDE)
    return cfg
