"""Enrichment configuration.

Reads the *same* config.yaml as the matcher but only the ``enrichment:`` block,
so this layer drops in without touching the existing Config model. The API key
is deliberately NOT a config field -- it is read from the environment
(HUNTER_API_KEY) so a secret never lands in a file that might be committed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import RoleBucket


class SmtpProbeCfg(BaseModel):
    # Self-hosted SMTP verification. OFF by default: probing strangers' mail
    # servers from your IP can get that IP flagged, and catch-all domains make
    # the result unreliable anyway. Prefer the provider's verifier.
    enabled: bool = False
    mail_from: str = "verify@example.com"  # set to a domain you control
    timeout_seconds: float = 10.0
    detect_catch_all: bool = True


class EnrichmentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str = "hunter"  # currently: hunter
    hunter_base: str = "https://api.hunter.io/v2"

    # Which roles you actually want. A contact is kept only if its position maps
    # to one of these buckets (OTHER is never auto-included).
    target_roles: list[RoleBucket] = Field(
        default_factory=lambda: [
            RoleBucket.CEO, RoleBucket.CTO, RoleBucket.FOUNDER,
            RoleBucket.ENGINEERING_LEAD, RoleBucket.HR, RoleBucket.TALENT,
        ]
    )

    # "Verified only" policy.
    keep_statuses: list[str] = Field(default_factory=lambda: ["valid"])
    include_accept_all: bool = False        # catch-all = not truly verifiable
    min_confidence: int = 0                  # extra floor on provider score
    cross_check_with_self_verifier: bool = False  # double-check provider "valid"

    # Role-inbox fallback (careers@, jobs@, founders@ ...). Safe, non-personal,
    # ideal for a job application when no individual is found/verified.
    role_inbox_fallback: bool = True
    role_inbox_localparts: list[str] = Field(
        default_factory=lambda: ["careers", "jobs", "hiring", "talent",
                                 "recruiting", "founders", "hello", "team"]
    )

    # Compliance: addresses here are NEVER contacted/emitted. A "no" is forever.
    suppression_file: Optional[str] = "out/suppression.txt"

    smtp: SmtpProbeCfg = Field(default_factory=SmtpProbeCfg)

    # transport
    user_agent: str = "yc-hiring-agent-enrich/1.0 (+set-a-real-contact@example.com)"
    min_interval_seconds: float = 0.2   # well under Hunter's 15 rps / 500 rpm
    cache_ttl_seconds: int = 21_600
    cache_path: str = ".cache/enrich_cache.sqlite"

    def api_key(self) -> Optional[str]:
        return os.environ.get("HUNTER_API_KEY")


def load_enrichment_config(path: Optional[str | Path]) -> EnrichmentConfig:
    block: dict = {}
    if path:
        p = Path(path)
        if p.exists():
            full = yaml.safe_load(p.read_text()) or {}
            block = full.get("enrichment", {}) or {}
    return EnrichmentConfig(**block)
