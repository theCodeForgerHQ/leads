"""Command-line entrypoint.

    python -m yc_agent.cli --config config.yaml
    python -m yc_agent.cli --config config.yaml --years-back 2 --max-team-size 200

Network-touching wiring lives here; everything it calls is independently
testable. Run with --dry-run to print the resolved filter window and the set of
in-window batches without fetching the job index.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .http import PoliteSession
from .pipeline import Pipeline, write_outputs
from .sources import CompanyPageJobsSource, YcOssSource


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Find recently-funded YC startups hiring software roles.")
    p.add_argument("--config", help="Path to YAML config (optional; defaults used otherwise).")
    p.add_argument("--years-back", type=int, help="Override eligibility window in years.")
    p.add_argument("--max-team-size", type=int, help="Override the headcount cap.")
    p.add_argument("--exclude-top-company", action="store_true", help="Drop YC 'top company' badge holders.")
    p.add_argument("--out", help="Override output directory.")
    p.add_argument("--dry-run", action="store_true", help="Resolve filters + in-window batches only; no job fetch.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    if args.years_back is not None:
        cfg.years_back = args.years_back
    if args.max_team_size is not None:
        cfg.max_team_size = args.max_team_size
    if args.exclude_top_company:
        cfg.exclude_top_company = True
    if args.out:
        cfg.output.dir = args.out

    session = PoliteSession(cfg.http)
    yc = YcOssSource(cfg, session)

    if args.dry_run:
        meta = yc.fetch_meta()
        batches = yc.recent_batch_urls(meta)
        print(f"as_of={cfg.effective_as_of}  cutoff={cfg.cutoff_date}  years_back={cfg.years_back}")
        print(f"in-window batches ({len(batches)}):")
        for display, _ in sorted(batches):
            print(f"  - {display}")
        return 0

    jobs_src = CompanyPageJobsSource(cfg, session)

    matched, summary = Pipeline(cfg, yc, jobs_src).run()
    written = write_outputs(matched, summary, cfg)

    print(
        f"\nMatched {summary.matched_companies} startups "
        f"({summary.matched_jobs} open software roles) "
        f"from {summary.eligible_companies} eligible companies."
    )
    for p in written:
        print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
