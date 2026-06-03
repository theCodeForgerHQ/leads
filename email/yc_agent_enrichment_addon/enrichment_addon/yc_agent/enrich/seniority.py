"""Classify a person's position into the role bucket you care about.

Hunter returns a free-text ``position`` (e.g. "Co-Founder & CTO", "Head of
People"). We map that to one of a small set of buckets so the user can say "I
want CEO/CTO/HR" and have it mean something precise. Order matters: the most
specific/seniormost match wins, so "Founder & CEO" classifies as CEO, and an
engineering leader is distinguished from an individual contributor engineer.
"""

from __future__ import annotations

import re
from typing import Iterable

from .models import RoleBucket

# Checked in priority order; first hit wins.
_RULES: list[tuple[RoleBucket, re.Pattern]] = [
    (RoleBucket.CEO, re.compile(r"\bC\.?E\.?O\.?\b|chief executive", re.I)),
    (RoleBucket.CTO, re.compile(r"\bC\.?T\.?O\.?\b|chief techn(ology|ical) officer", re.I)),
    (RoleBucket.TALENT, re.compile(
        r"\b(talent|recruit(er|ing|ment)?|sourcer|technical recruiter)\b", re.I)),
    (RoleBucket.HR, re.compile(
        r"\b(head|director|vp|chief)\b.*\b(people|hr|human resources|talent)\b"
        r"|\b(people ops|peopleops|human resources|hr\b|chief people officer|chro)\b", re.I)),
    (RoleBucket.ENGINEERING_LEAD, re.compile(
        r"\b(head|vp|director|lead|manager|chief)\b.*\bengineer(ing)?\b"
        r"|\bengineering (lead|manager|director)\b|\bvp(,)? eng\b", re.I)),
    (RoleBucket.FOUNDER, re.compile(r"\b(co[- ]?)?founder\b|founding (team|member)", re.I)),
]


def classify_position(position: str | None) -> RoleBucket:
    text = (position or "").strip()
    if not text:
        return RoleBucket.OTHER
    for bucket, pat in _RULES:
        if pat.search(text):
            return bucket
    return RoleBucket.OTHER


def is_wanted(position: str | None, wanted: Iterable[RoleBucket]) -> tuple[bool, RoleBucket]:
    bucket = classify_position(position)
    wanted_set = set(wanted)
    return (bucket in wanted_set and bucket is not RoleBucket.OTHER), bucket
