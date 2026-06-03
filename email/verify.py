"""A self-hosted email verifier, used as an optional cross-check.

The provider (Hunter) is the primary verifier because it runs from warmed,
reputable infrastructure and maintains a B2B database. This module exists for
two reasons: (1) to double-check a provider "valid" if you're paranoid, and
(2) to verify role-inbox guesses without spending provider credits.

Layers, cheapest first:
  1. Syntax (RFC-ish) -- free, instant, catches typos.
  2. MX lookup        -- does the domain accept mail at all? (needs dnspython)
  3. SMTP RCPT probe  -- OPTIONAL, OFF by default. Connect to the MX and ask if
     the mailbox exists, plus a random-localpart probe to detect catch-all.

Honesty about limits: SMTP probing strangers' servers from your IP can hurt that
IP's reputation, many providers greylist or block it, and a catch-all domain
will answer 250 to everything -- so a "pass" there is NOT proof of a real
mailbox. We return ACCEPT_ALL in that case rather than pretending it's VALID.
"""

from __future__ import annotations

import logging
import random
import smtplib
import socket
import string
from dataclasses import dataclass
from typing import Optional

from .models import VerificationStatus

log = logging.getLogger(__name__)

try:
    import dns.resolver  # type: ignore

    _HAVE_DNS = True
except Exception:  # pragma: no cover - optional dep
    _HAVE_DNS = False

try:
    from email_validator import EmailNotValidError, validate_email  # type: ignore

    _HAVE_EV = True
except Exception:  # pragma: no cover - optional dep
    _HAVE_EV = False

_BASIC_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DISPOSABLE_HINTS = {"mailinator.com", "10minutemail.com", "guerrillamail.com",
                     "trashmail.com", "yopmail.com", "tempmail.com"}
_WEBMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
            "yahoo.com", "icloud.com", "proton.me", "protonmail.com", "aol.com"}


@dataclass
class VerifyResult:
    status: VerificationStatus
    mx_records: Optional[bool] = None
    smtp_check: Optional[bool] = None
    accept_all: Optional[bool] = None
    reason: str = ""


def check_syntax(email: str) -> bool:
    if _HAVE_EV:
        try:
            validate_email(email, check_deliverability=False)
            return True
        except EmailNotValidError:
            return False
    return bool(_BASIC_RE.match(email or ""))


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def mx_hosts(domain: str) -> list[str]:
    if not _HAVE_DNS:
        return []
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8.0)
        ranked = sorted(answers, key=lambda r: r.preference)
        return [str(r.exchange).rstrip(".") for r in ranked]
    except Exception:  # NXDOMAIN, no MX, timeout, etc.
        return []


class SelfVerifier:
    def __init__(self, *, mail_from: str, timeout: float = 10.0,
                 detect_catch_all: bool = True, smtp_enabled: bool = False) -> None:
        self.mail_from = mail_from
        self.timeout = timeout
        self.detect_catch_all = detect_catch_all
        self.smtp_enabled = smtp_enabled

    def verify(self, email: str) -> VerifyResult:
        if not check_syntax(email):
            return VerifyResult(VerificationStatus.INVALID, reason="bad_syntax")

        domain = _domain(email)
        if domain in _DISPOSABLE_HINTS:
            return VerifyResult(VerificationStatus.DISPOSABLE, reason="disposable_domain")
        if domain in _WEBMAIL:
            # B2B context: we don't SMTP-probe webmail; treat as its own bucket.
            return VerifyResult(VerificationStatus.WEBMAIL, mx_records=True, reason="webmail")

        hosts = mx_hosts(domain)
        if _HAVE_DNS and not hosts:
            return VerifyResult(VerificationStatus.INVALID, mx_records=False,
                                reason="no_mx_records")

        if not (self.smtp_enabled and hosts):
            # Without an SMTP probe we cannot confirm the mailbox. Be honest:
            # syntax + MX only is "unknown", not "valid".
            return VerifyResult(VerificationStatus.UNKNOWN, mx_records=bool(hosts),
                                reason="mx_only_no_smtp_probe")

        return self._smtp_probe(email, domain, hosts)

    # -- SMTP --------------------------------------------------------------
    def _rcpt(self, server: smtplib.SMTP, addr: str) -> int:
        server.mail(self.mail_from)
        code, _ = server.rcpt(addr)
        return code

    def _smtp_probe(self, email: str, domain: str, hosts: list[str]) -> VerifyResult:
        last_err = ""
        for host in hosts[:2]:  # try the top two MX hosts, then give up
            try:
                with smtplib.SMTP(host, 25, timeout=self.timeout) as srv:
                    srv.ehlo_or_helo_if_needed()
                    real_code = self._rcpt(srv, email)

                    catch_all = None
                    if self.detect_catch_all:
                        rnd = "".join(random.choices(string.ascii_lowercase, k=16))
                        catch_code = self._rcpt(srv, f"{rnd}@{domain}")
                        catch_all = catch_code in (250, 251)

                    if catch_all:
                        return VerifyResult(VerificationStatus.ACCEPT_ALL, mx_records=True,
                                            smtp_check=True, accept_all=True,
                                            reason="catch_all_domain")
                    if real_code in (250, 251):
                        return VerifyResult(VerificationStatus.VALID, mx_records=True,
                                            smtp_check=True, accept_all=False,
                                            reason="rcpt_accepted")
                    if real_code in (550, 551, 553):
                        return VerifyResult(VerificationStatus.INVALID, mx_records=True,
                                            smtp_check=False, reason=f"rcpt_rejected_{real_code}")
                    return VerifyResult(VerificationStatus.UNKNOWN, mx_records=True,
                                        smtp_check=None, reason=f"rcpt_code_{real_code}")
            except (smtplib.SMTPException, socket.timeout, OSError) as exc:
                last_err = type(exc).__name__
                continue
        return VerifyResult(VerificationStatus.UNKNOWN, mx_records=True,
                            reason=f"smtp_unreachable_{last_err}")
