"""Idempotently import Missouri vacant-land double-close research into the CRM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from crm_db import CRMDatabase
from crm_phase13 import import_land_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/leads.db"))
    parser.add_argument("--organization-id", type=int, default=1)
    args = parser.parse_args()
    summary = import_land_csv(CRMDatabase(args.database), args.csv_path, args.organization_id)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
