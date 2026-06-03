"""Polite HTTP session: cache + retry + global rate limit + robots gate.

This is the SAME file shipped in v1. It is included here so the enrichment
add-on runs standalone; if you already have yc_agent/http.py you can keep yours.
The enrichment provider client depends only on get_json(); the email verifier
does its own DNS/SMTP and does not use this session.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import requests
import requests_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class _RateLimiter:
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
    def __init__(self, *, user_agent: str, min_interval_seconds: float = 0.34,
                 timeout_seconds: float = 20.0, max_retries: int = 5,
                 backoff_factor: float = 0.8, cache_ttl_seconds: int = 21_600,
                 cache_path: str = ".cache/enrich_cache.sqlite") -> None:
        self.timeout = timeout_seconds
        self._limiter = _RateLimiter(min_interval_seconds)
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self.session = requests_cache.CachedSession(
            cache_name=cache_path.removesuffix(".sqlite"),
            backend="sqlite",
            expire_after=cache_ttl_seconds,
            allowable_methods=("GET",),
            stale_if_error=True,
        )
        retry = Retry(
            total=max_retries, backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True, raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": user_agent})

    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        self._limiter.wait()
        resp = self.session.get(url, params=params or {}, timeout=self.timeout)
        # Surface Hunter's structured error bodies instead of a bare raise.
        if resp.status_code >= 400:
            try:
                return resp.json()
            except (ValueError, json.JSONDecodeError):
                resp.raise_for_status()
        return resp.json()
