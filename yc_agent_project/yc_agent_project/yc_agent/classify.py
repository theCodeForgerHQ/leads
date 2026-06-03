"""Decide whether a job is a software role.

Two signals, in order of trust:

1. YC's own ``role`` facet (default: ``eng``). This is the authoritative
   classification and is what we rely on by default.
2. A title regex fallback, for the occasional role that is genuinely software
   but tagged under a different facet (a "Founding Engineer" filed as "other",
   say). It is paired with an exclude regex so that a hard-tech company's
   mechanical / electrical / hardware engineering roles do *not* get pulled in
   under a broad eng tag -- "software related" should mean software.

The exclude check runs first and wins: a "Mechanical Engineer" never counts,
regardless of facet. This trades a sliver of recall for precision, which is the
right call when the requirement is strict accuracy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .models import Job


@dataclass(frozen=True)
class SoftwareClassifier:
    roles: frozenset[str]
    fallback_enabled: bool
    include_re: re.Pattern
    exclude_re: re.Pattern

    # Default include/exclude patterns used when config lists are empty.
    _DEFAULT_INCLUDE = [
        r"\bsoftware\b",
        r"\bfrontend\b", r"\bback[\s-]?end\b", r"\bfull[\s-]?stack\b",
        r"\bdevops\b", r"\bsre\b", r"\bplatform\s+eng",
        r"\binfrastructure\s+eng", r"\bcloud\s+eng",
        r"\bmachine\s+learning\b", r"\bml\s+eng", r"\bai\s+eng",
        r"\bdata\s+eng", r"\bdata\s+scientist",
        r"\biosios\b", r"\bandroid\b", r"\bmobile\s+eng",
        r"\bsecurity\s+eng",
        r"\bfounding\s+eng", r"\bstaff\s+eng", r"\bprincipal\s+eng",
        r"\bengineering\s+manager",
        r"\bswe\b", r"\b(?:cto|vp\s+eng)",
    ]
    _DEFAULT_EXCLUDE = [
        r"\bmechanical\b", r"\belectrical\b", r"\bhardware\b",
        r"\bcivil\b", r"\bchemical\b", r"\bstructural\b",
        r"\bsales\s+eng", r"\bfield\s+eng", r"\bsupport\s+eng",
    ]

    @classmethod
    def from_config(cls, cfg: Config) -> "SoftwareClassifier":
        def compile_any(patterns: list[str], defaults: list[str]) -> re.Pattern:
            # Use provided patterns or fall back to built-in defaults.
            effective = patterns if patterns else defaults
            joined = "|".join(f"(?:{p})" for p in effective) if effective else r"(?!x)x"
            return re.compile(joined, re.IGNORECASE)

        return cls(
            roles=frozenset(r.lower() for r in cfg.software_roles),
            fallback_enabled=cfg.title_fallback.enabled,
            include_re=compile_any(cfg.title_fallback.include_patterns, cls._DEFAULT_INCLUDE),
            exclude_re=compile_any(cfg.title_fallback.exclude_patterns, cls._DEFAULT_EXCLUDE),
        )

    def is_software(self, job: Job) -> bool:
        title = (job.title or "")
        if self.exclude_re.search(title):
            return False
        if job.role and job.role.lower() in self.roles:
            return True
        if self.fallback_enabled and self.include_re.search(title):
            return True
        return False
