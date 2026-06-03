"""The two data sources, plus runtime discovery of the public Algolia key.

``YcOssSource`` provides the slow-changing company universe (batch, status,
headcount) from the daily yc-oss mirror -- no credentials needed.

``WaasJobsSource`` provides the fast-changing, authoritative list of *currently
open* roles straight from the Work-at-a-Startup job index. A row's existence is
proof the role is live, and the ``role`` facet lets us ask for software roles
specifically. It exhaustively paginates and, if a result slice would exceed
Algolia's per-query retrieval ceiling, partitions by a facet rather than
silently truncating -- silent truncation is exactly the kind of invisible
accuracy loss we refuse to ship.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from .config import AlgoliaCfg, Config
from .http import AlgoliaClient, PoliteSession
from .models import Company, Job

log = logging.getLogger(__name__)

_JOB_ATTRS = [
    "company_id",
    "company_name",
    "company_website",
    "title",
    "role",
    "job_type",
    "locations_for_search",
    "created_at",
]

# Algolia refuses to page past this many results for a single query by default.
_ALGOLIA_RETRIEVAL_CEILING = 1000


class YcOssSource:
    """Company-level facts from https://yc-oss.github.io/api ."""

    def __init__(self, cfg: Config, session: PoliteSession) -> None:
        self.cfg = cfg
        self.session = session
        self.base = cfg.yc_oss_base.rstrip("/")

    def fetch_meta(self) -> dict:
        return self.session.get_json(f"{self.base}/meta.json")

    def recent_batch_urls(self, meta: dict) -> list[tuple[str, str]]:
        """Batch endpoints whose start date is within the configured window."""
        out = []
        for key, info in (meta.get("batches") or {}).items():
            display = info.get("name", "")
            if self.cfg.batch_in_window(display):
                out.append((display, info["api"]))
        return out

    def eligible_companies(self) -> dict[int, Company]:
        """All in-window companies that also pass the 'truly a startup' guards.

        Keyed by company id for an O(1) join against job rows downstream.
        """
        meta = self.fetch_meta()
        batch_urls = self.recent_batch_urls(meta)
        log.info("In-window batches: %d", len(batch_urls))

        companies: dict[int, Company] = {}
        kept, dropped = 0, 0
        for display, url in batch_urls:
            for raw in self.session.get_json(url):
                comp = self._to_company(raw)
                if self._passes_guards(comp):
                    companies[comp.id] = comp
                    kept += 1
                else:
                    dropped += 1
        log.info("Company universe: kept=%d dropped_by_guards=%d", kept, dropped)
        return companies

    def _to_company(self, raw: dict) -> Company:
        comp = Company(
            id=raw["id"],
            slug=raw["slug"],
            name=raw["name"],
            batch=raw.get("batch", ""),
            status=raw.get("status", ""),
            team_size=raw.get("team_size"),
            top_company=bool(raw.get("top_company", False)),
            one_liner=raw.get("one_liner"),
            website=raw.get("website"),
            all_locations=raw.get("all_locations"),
            industries=list(raw.get("industries") or []),
            tags=list(raw.get("tags") or []),
            regions=list(raw.get("regions") or []),
            stage=raw.get("stage"),
            yc_url=raw.get("url"),
        )
        comp.batch_key = self.cfg.batch_to_key(comp.batch)
        return comp

    def _passes_guards(self, c: Company) -> bool:
        if self.cfg.require_active and c.status != "Active":
            return False
        if self.cfg.exclude_top_company and c.top_company:
            return False
        if c.team_size is not None and c.team_size > self.cfg.max_team_size:
            return False
        return True


class WaasJobsSource:
    """Live open-role rows from the Work-at-a-Startup Algolia index."""

    def __init__(self, cfg: Config, client: AlgoliaClient) -> None:
        self.cfg = cfg
        self.client = client
        self.index = cfg.algolia.jobs_index

    def _role_filter(self) -> str:
        roles = self.cfg.software_roles or ["eng"]
        return " OR ".join(f"role:{r}" for r in roles)

    def software_jobs(self) -> list[Job]:
        """Every currently-open role matching the configured software role set."""
        filters = self._role_filter()
        raw_hits = self._fetch_exhaustive(filters, partitions=["job_type"])
        jobs, seen = [], set()
        for h in raw_hits:
            oid = h.get("objectID")
            if oid in seen:
                continue
            seen.add(oid)
            job = self._to_job(h)
            if job is not None:
                jobs.append(job)
        log.info("Open software-role jobs fetched: %d", len(jobs))
        return jobs

    # -- pagination that refuses to silently truncate -----------------------
    def _fetch_exhaustive(self, filters: str, partitions: list[str]) -> list[dict]:
        first = self.client.query(
            self.index, filters=filters, page=0,
            hits_per_page=_ALGOLIA_RETRIEVAL_CEILING, attributes=_JOB_ATTRS,
        )
        nb_hits = int(first.get("nbHits", 0))
        nb_pages = int(first.get("nbPages", 1))

        if nb_hits <= _ALGOLIA_RETRIEVAL_CEILING or not partitions:
            hits = list(first.get("hits", []))
            for p in range(1, nb_pages):
                hits += self.client.query(
                    self.index, filters=filters, page=p,
                    hits_per_page=_ALGOLIA_RETRIEVAL_CEILING, attributes=_JOB_ATTRS,
                ).get("hits", [])
            if nb_hits > len(hits):
                log.warning(
                    "TRUNCATION RISK: %d hits reported but %d retrievable for "
                    "filter %r and no partition facet left. Results may be "
                    "incomplete; add a finer partition (e.g. created_at ranges).",
                    nb_hits, len(hits), filters,
                )
            return hits

        # Too many results: split by the next facet and recurse.
        facet = partitions[0]
        log.info("Partitioning %r by %r (%d hits exceed ceiling)", filters, facet, nb_hits)
        hits = []
        for value in self._facet_values(filters, facet):
            sub = f'{filters} AND {facet}:"{value}"'
            hits += self._fetch_exhaustive(sub, partitions[1:])
        return hits

    def _facet_values(self, filters: str, facet: str) -> list[str]:
        resp = self.client.query(
            self.index, filters=filters, hits_per_page=0, facets=[facet]
        )
        return list((resp.get("facets", {}).get(facet, {}) or {}).keys())

    def _to_job(self, h: dict) -> Optional[Job]:
        cid = h.get("company_id")
        if cid is None:
            return None
        locs = h.get("locations_for_search") or []
        if isinstance(locs, str):
            locs = [locs]
        try:
            return Job(
                object_id=str(h.get("objectID", "")),
                company_id=int(cid),
                title=h.get("title", "") or "",
                role=h.get("role"),
                job_type=h.get("job_type"),
                company_name=h.get("company_name"),
                company_website=h.get("company_website"),
                locations=[str(x) for x in locs],
                created_at=h.get("created_at"),
            )
        except (ValueError, TypeError):
            return None


# --------------------------------------------------------------------------
_KEY_PATTERNS = [
    re.compile(r'["\']?(?:algolia[_-]?api[_-]?key|apiKey|search[_-]?key)["\']?\s*[:=]\s*["\']([A-Za-z0-9]{20,})["\']', re.I),
    re.compile(r'["\']([0-9a-f]{32})["\']'),  # last-resort: a 32-hex blob nearby
]
_APP_PATTERNS = [
    re.compile(r'["\']?(?:algolia[_-]?app[_-]?id|appId|application[_-]?id)["\']?\s*[:=]\s*["\']([A-Z0-9]{6,})["\']', re.I),
]


def resolve_algolia_credentials(cfg: AlgoliaCfg, session: PoliteSession) -> tuple[str, str]:
    """Return (app_id, api_key), preferring config and falling back to the page.

    The public search key is the same one WaaS serves to every browser; keys can
    rotate, so when one is not pinned in config we try to read the current value
    off the site. The app id is stable and defaulted.
    """
    if cfg.api_key:
        return cfg.app_id, cfg.api_key

    if not cfg.bootstrap_from_page:
        raise RuntimeError(
            "No Algolia api_key in config and bootstrap_from_page is off. "
            "Set algolia.api_key (grab it from the WaaS page: DevTools > Network "
            "> the *.algolia.net request > X-Algolia-API-Key header)."
        )

    html = session.get_text(cfg.bootstrap_url)
    if not html:
        raise RuntimeError(
            f"Could not read {cfg.bootstrap_url} to discover the Algolia key "
            "(blocked by robots or unreachable). Set algolia.api_key in config."
        )
    app_id = cfg.app_id
    for pat in _APP_PATTERNS:
        m = pat.search(html)
        if m:
            app_id = m.group(1)
            break
    for pat in _KEY_PATTERNS:
        m = pat.search(html)
        if m:
            log.info("Discovered Algolia search key from page (len=%d).", len(m.group(1)))
            return app_id, m.group(1)
    raise RuntimeError(
        "Failed to extract an Algolia search key from the page. Pin it manually "
        "in config.algolia.api_key."
    )
