"""Run the durable local CRM workflow worker."""

from __future__ import annotations

import argparse
from pathlib import Path

from workflow_engine import run_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Process CRM workflow events and actions")
    parser.add_argument("--database", type=Path, default=Path("data/leads.db"))
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_worker(args.database, args.poll_seconds, args.once)


if __name__ == "__main__":
    main()
