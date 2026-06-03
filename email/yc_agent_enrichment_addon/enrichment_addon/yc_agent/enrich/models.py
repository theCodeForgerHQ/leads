"""Typed models for the contact-enrichment layer.

A Contact carries not just an email but the *evidence* for it: which verification
status it earned, the confidence score, what role bucket it maps to, and where it
came from. That provenance is the whole point -- "verified only" is meaningless
unless every row says exactly how it was verified and when.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class VerificationStatus(str, Enum):
    """Normalised across providers and the self-hosted verifier.

    VALID        deliverable, mailbox confirmed (SMTP check passed, not catch-all)
    ACCEPT_ALL   domain accepts everything; individual mailbox UNVERIFIABLE
    INVALID      address failed checks; do not use
    DISPOSABLE   temporary/burner address
    WEBMAIL      gmail/outlook/etc.; B2B verifiers don't probe these
    UNKNOWN      greylisted/blocked/timed out; could not determine
    """

    VALID = "valid"
    ACCEPT_ALL = "accept_all"
    INVALID = "invalid"
    DISPOSABLE = "disposable"
    WEBMAIL = "webmail"
    UNKNOWN = "unknown"


class RoleBucket(str, Enum):
    CEO = "ceo"
    CTO = "cto"
    FOUNDER = "founder"
    ENGINEERING_LEAD = "engineering_lead"
    HR = "hr"
    TALENT = "talent"
    OTHER = "other"


class Contact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company: str
    company_id: Optional[int] = None
    domain: str

    email: str
    email_type: str = "personal"  # personal | generic (role-based inbox)
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    position: Optional[str] = None
    role_bucket: RoleBucket = RoleBucket.OTHER

    status: VerificationStatus
    confidence: Optional[int] = None  # 0-100 provider score
    smtp_check: Optional[bool] = None
    mx_records: Optional[bool] = None
    accept_all: Optional[bool] = None

    source: str = "hunter"  # hunter | role_inbox | other
    verified_on: Optional[str] = None
    observed_at: str = Field(
        default_factory=lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    def to_row(self) -> dict:
        return {
            "company": self.company,
            "company_id": self.company_id,
            "domain": self.domain,
            "name": self.full_name,
            "position": self.position,
            "role_bucket": self.role_bucket.value,
            "email": self.email,
            "email_type": self.email_type,
            "status": self.status.value,
            "confidence": self.confidence,
            "smtp_check": self.smtp_check,
            "accept_all": self.accept_all,
            "source": self.source,
            "verified_on": self.verified_on,
            "observed_at": self.observed_at,
        }


class CompanyContacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company: str
    company_id: Optional[int] = None
    domain: str
    pattern: Optional[str] = None  # Hunter's detected address pattern, e.g. {first}
    contacts: list[Contact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
