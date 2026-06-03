"""Hunter.io client.

Three endpoints, mapped onto our normalised models:
  * domain-search : people at a company (name, position, type, confidence, and a
                    per-email verification status). Our primary discovery source.
  * email-finder  : best-guess address for a known name at a domain.
  * email-verifier: deliverability for a single address.

We talk to the documented v2 REST API with plain GET requests through the shared
PoliteSession, so every call is cached and rate-limited. Hunter's own limits
(15 req/s, 500 req/min on domain-search) sit far above our configured pace.

Hunter's verifier ``status`` is one of: valid, invalid, accept_all, webmail,
disposable, unknown (plus a ``block`` flag we fold into unknown). We map those
straight onto VerificationStatus so downstream policy is provider-agnostic.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..http import PoliteSession
from .models import VerificationStatus

log = logging.getLogger(__name__)

_STATUS_MAP = {
    "valid": VerificationStatus.VALID,
    "accept_all": VerificationStatus.ACCEPT_ALL,
    "invalid": VerificationStatus.INVALID,
    "disposable": VerificationStatus.DISPOSABLE,
    "webmail": VerificationStatus.WEBMAIL,
    "unknown": VerificationStatus.UNKNOWN,
}


def normalise_status(raw: Optional[str], *, block: bool = False) -> VerificationStatus:
    if block:
        return VerificationStatus.UNKNOWN
    return _STATUS_MAP.get((raw or "").lower(), VerificationStatus.UNKNOWN)


class HunterError(RuntimeError):
    pass


class HunterClient:
    def __init__(self, api_key: str, session: PoliteSession,
                 base: str = "https://api.hunter.io/v2") -> None:
        if not api_key:
            raise HunterError(
                "No Hunter API key. Export HUNTER_API_KEY in your environment "
                "(get a free key at https://hunter.io). Keys are never read "
                "from the config file."
            )
        self.api_key = api_key
        self.session = session
        self.base = base.rstrip("/")

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "api_key": self.api_key}
        data = self.session.get_json(f"{self.base}/{path}", params=params)
        if isinstance(data, dict) and data.get("errors"):
            msgs = "; ".join(e.get("details", str(e)) for e in data["errors"])
            raise HunterError(f"Hunter API error on /{path}: {msgs}")
        return data

    # -- endpoints ---------------------------------------------------------
    def domain_search(self, domain: str, *, limit: int = 25,
                      seniority: Optional[str] = None,
                      department: Optional[str] = None) -> dict:
        """Return the raw 'data' block for a company domain.

        ``seniority`` ('senior'/'executive') and ``department`` ('executive',
        'hr', 'engineering', ...) let Hunter pre-filter server-side so we pull
        fewer, more relevant people.
        """
        params: dict = {"domain": domain, "limit": limit}
        if seniority:
            params["seniority"] = seniority
        if department:
            params["department"] = department
        return self._get("domain-search", params).get("data", {}) or {}

    def email_finder(self, domain: str, first_name: str, last_name: str) -> dict:
        return self._get(
            "email-finder",
            {"domain": domain, "first_name": first_name, "last_name": last_name},
        ).get("data", {}) or {}

    def email_verifier(self, email: str) -> dict:
        return self._get("email-verifier", {"email": email}).get("data", {}) or {}
