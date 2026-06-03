"""Transport layer: a single well-behaved HTTP session plus a thin Algolia client.

Design choices that matter for not getting blocked and not getting wrong data:

* One global rate limiter (a minimum interval between requests) so total
  outbound pressure is bounded no matter how the pipeline is structured.
* On-disk response caching keyed by URL, with a TTL equal to the data's natural
  refresh cadence. Re-runs are nearly free and a mid-run crash resumes cheaply.
* Automatic retries with exponential backoff that honour ``Retry-After`` on
  429/5xx, so transient throttling is absorbed rather than hammered through.
* Honest, contactable User-Agent. No authentication anywhere: every endpoint we
  touch is public, so there is no account that can be suspended.
* robots.txt is consulted before any HTML page fetch.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path
from typing import Any, Optional

import requests
import requests_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HttpCfg


class _RateLimiter:
    """Enforce a minimum wall-clock interval between calls, thread-safe."""

    def __init__(self, min_interval: float) -> None:
        self._min = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._min:
                time.sleep(self._min - delta)
            self._last = time.monotonic()


class PoliteSession:
    """A cached, retrying, rate-limited requests session with a robots gate."""

    def __init__(self, cfg: HttpCfg) -> None:
        self.cfg = cfg
        self._limiter = _RateLimiter(cfg.min_interval_seconds)
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._robots_lock = threading.Lock()

        Path(cfg.cache_path).parent.mkdir(parents=True, exist_ok=True)
        self.session = requests_cache.CachedSession(
            cache_name=cfg.cache_path.removesuffix(".sqlite"),
            backend="sqlite",
            expire_after=cfg.cache_ttl_seconds,
            allowable_methods=("GET", "POST"),  # Algolia search is POST
            stale_if_error=True,  # serve cached copy if upstream errors
        )
        retry = Retry(
            total=cfg.max_retries,
            backoff_factor=cfg.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    # -- robots --------------------------------------------------------------
    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if base in self._robots:
                return self._robots[base]
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{base}/robots.txt")
            try:
                # Fetch robots itself politely, but never through the cache layer
                # logic above (keep it simple and tolerant of failure).
                self._limiter.wait()
                resp = requests.get(
                    f"{base}/robots.txt",
                    timeout=self.cfg.timeout_seconds,
                    headers={"User-Agent": self.cfg.user_agent},
                )
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None  # no robots => allowed
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
            return rp

    def allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.cfg.user_agent, url)

    # -- requests ------------------------------------------------------------
    def get_json(self, url: str) -> Any:
        self._limiter.wait()
        resp = self.session.get(url, timeout=self.cfg.timeout_seconds)
        resp.raise_for_status()
        return resp.json()

    def get_text(self, url: str, check_robots: bool = True) -> Optional[str]:
        if check_robots and not self.allowed(url):
            return None
        self._limiter.wait()
        resp = self.session.get(url, timeout=self.cfg.timeout_seconds)
        resp.raise_for_status()
        return resp.text

    def post_json(self, url: str, *, headers: dict, body: dict) -> Any:
        self._limiter.wait()
        resp = self.session.post(
            url, headers=headers, data=json.dumps(body), timeout=self.cfg.timeout_seconds
        )
        resp.raise_for_status()
        return resp.json()


class AlgoliaError(RuntimeError):
    pass


class AlgoliaClient:
    """Minimal read-only client for Algolia's REST search endpoint.

    We talk to the REST API directly with ``requests`` rather than pulling in
    the official SDK: the surface we need is one endpoint, and doing it by hand
    keeps the request shape fully visible and auditable.
    """

    def __init__(self, app_id: str, api_key: str, session: PoliteSession) -> None:
        if not app_id or not api_key:
            raise AlgoliaError("Algolia app_id and api_key are required")
        self.app_id = app_id
        self.api_key = api_key
        self.session = session
        self._url_tmpl = f"https://{app_id}-dsn.algolia.net/1/indexes/{{index}}/query"

    def _headers(self) -> dict:
        return {
            "X-Algolia-Application-Id": self.app_id,
            "X-Algolia-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _params(**kwargs: Any) -> str:
        """Build the URL-encoded ``params`` string Algolia expects.

        List/array values are JSON-encoded first (e.g. attributesToRetrieve),
        which is what the Algolia REST API requires.
        """
        flat: dict[str, str] = {}
        for k, v in kwargs.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                flat[k] = json.dumps(list(v))
            elif isinstance(v, bool):
                flat[k] = "true" if v else "false"
            else:
                flat[k] = str(v)
        return urllib.parse.urlencode(flat)

    def query(
        self,
        index: str,
        *,
        filters: str = "",
        page: int = 0,
        hits_per_page: int = 1000,
        attributes: Optional[list[str]] = None,
        facets: Optional[list[str]] = None,
    ) -> dict:
        params = self._params(
            query="",
            filters=filters,
            page=page,
            hitsPerPage=hits_per_page,
            attributesToRetrieve=attributes,
            attributesToHighlight=[],
            attributesToSnippet=[],
            facets=facets,
            distinct="true",
        )
        url = self._url_tmpl.format(index=urllib.parse.quote(index, safe=""))
        return self.session.post_json(url, headers=self._headers(), body={"params": params})
