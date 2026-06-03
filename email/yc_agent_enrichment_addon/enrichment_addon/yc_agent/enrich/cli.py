"""Enrichment CLI.

    export HUNTER_API_KEY=sk_...           # never put the key in a file
    python -m yc_agent.enrich.cli --in out/matches.csv --config config.yaml

Reads the match file produced by the matcher, finds verified contacts in the
roles you care about, and writes out/contacts.csv + out/contacts.jsonl.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..http import PoliteSession
from .config import load_enrichment_config
from .models import RoleBucket
from .pipeline import EnrichmentPipeline, read_match_companies, write_contact_outputs
from .providers import HunterClient
from .verify import SelfVerifier


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Find verified contacts for matched YC startups.")
    p.add_argument("--in", dest="infile", required=True, help="matches.csv or matches.jsonl")
    p.add_argument("--config", help="YAML config (reads its 'enrichment:' block).")
    p.add_argument("--out", default="out", help="Output directory.")
    p.add_argument("--roles", help="Comma list to override target roles "
                                    "(ceo,cto,founder,engineering_lead,hr,talent).")
    p.add_argument("--include-accept-all", action="store_true",
                   help="Also emit catch-all addresses (NOT individually verifiable).")
    p.add_argument("--smtp-verify", action="store_true",
                   help="Enable self-hosted SMTP probing (see README caveats).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_enrichment_config(args.config)
    if args.roles:
        cfg.target_roles = [RoleBucket(x.strip()) for x in args.roles.split(",") if x.strip()]
    if args.include_accept_all:
        cfg.include_accept_all = True
    if args.smtp_verify:
        cfg.smtp.enabled = True
        cfg.cross_check_with_self_verifier = True

    key = cfg.api_key()
    if not key:
        print("ERROR: set HUNTER_API_KEY in your environment (free key at hunter.io).",
              file=sys.stderr)
        return 2

    session = PoliteSession(
        user_agent=cfg.user_agent, min_interval_seconds=cfg.min_interval_seconds,
        cache_ttl_seconds=cfg.cache_ttl_seconds, cache_path=cfg.cache_path,
    )
    client = HunterClient(key, session, base=cfg.hunter_base)
    verifier = SelfVerifier(
        mail_from=cfg.smtp.mail_from, timeout=cfg.smtp.timeout_seconds,
        detect_catch_all=cfg.smtp.detect_catch_all, smtp_enabled=cfg.smtp.enabled,
    )

    companies = read_match_companies(args.infile)
    logging.getLogger(__name__).info("Unique companies to enrich: %d", len(companies))

    results = EnrichmentPipeline(cfg, client, verifier).run(companies)
    written = write_contact_outputs(results, args.out)

    total = sum(len(cc.contacts) for cc in results)
    with_contacts = sum(1 for cc in results if cc.contacts)
    print(f"\nFound {total} verified contacts across {with_contacts}/{len(companies)} companies.")
    for p in written:
        print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
