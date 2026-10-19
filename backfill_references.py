"""Backfill lead reference numbers from call transcripts via fuzzy offer matching.

For every lead with calls, this script:
1. Extracts canonical references and digit-group fragments from the stored
   transcripts (tolerating garbled prefixes like “0RMO” or truncated ones
   like “C-M-O dash two six one four dash one six seven seven”).
2. Matches digit groups against the offers database (unique matches only).
3. Falls back to owner-name matching when no reference evidence exists.
4. Fills empty lead reference numbers (never overwrites existing values).

Usage: py -3.12 backfill_references.py [--apply]
Without --apply it only reports what it would change.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from crm_db import CRMDatabase  # noqa: E402
from lead_pipeline import (  # noqa: E402
    extract_reference_digit_groups, extract_references_from_text,
)
from offer_db import OfferDatabase  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/leads.db"))
    parser.add_argument("--offers", type=Path, default=Path("all_fop_offers.db"))
    parser.add_argument("--index", type=Path, default=Path("data/offers_index.db"))
    parser.add_argument("--apply", action="store_true", help="Write changes to the leads database")
    args = parser.parse_args()

    db = CRMDatabase(args.database)
    offers = OfferDatabase(args.offers, args.index)
    if not offers.available:
        raise SystemExit(f"Offers database not found: {args.offers}")

    with db.connect() as connection:
        leads = [dict(row) for row in connection.execute(
            """SELECT l.id, l.phone_display, l.owner_name, l.reference_number,
                      (SELECT COUNT(*) FROM calls c WHERE c.lead_id=l.id) AS call_count
               FROM leads l WHERE EXISTS (SELECT 1 FROM calls c WHERE c.lead_id=l.id)"""
        ).fetchall()]

    filled = 0
    suggestions = 0
    for lead in leads:
        if lead["reference_number"].strip():
            continue
        with db.connect() as connection:
            transcripts = [row["transcript"] or "" for row in connection.execute(
                "SELECT transcript FROM calls WHERE lead_id=?", (lead["id"],)).fetchall()]
        combined = "\n".join(transcripts)
        if not combined.strip():
            continue

        canonical = []
        for transcript in transcripts:
            for reference in extract_references_from_text(transcript):
                if reference not in canonical:
                    canonical.append(reference)
        digit_groups = extract_reference_digit_groups(combined)

        resolved = ""
        reason = ""
        if canonical:
            resolved, reason = canonical[0], "canonical transcript reference"
        elif digit_groups:
            matches = offers.lookup_by_digits(digit_groups)
            if len(matches) == 1:
                resolved, reason = matches[0]["reference"], matches[0].get("match_reason", "digits")
        if not resolved and lead["owner_name"].strip():
            matches = offers.lookup_by_owner(lead["owner_name"])
            if len(matches) == 1:
                resolved, reason = matches[0]["reference"], matches[0].get("match_reason", "owner name")

        if not resolved:
            continue
        suggestions += 1
        print(f"lead {lead['id']} {lead['phone_display']} ({lead['owner_name'] or 'unnamed'}): "
              f"{resolved} via {reason}")
        if args.apply:
            with db.connect() as connection:
                connection.execute(
                    "UPDATE leads SET reference_number=?, updated_at=updated_at "
                    "WHERE id=? AND TRIM(COALESCE(reference_number,''))=''",
                    (resolved, lead["id"]),
                )
            filled += 1

    print(f"\n{'applied' if args.apply else 'would apply'} {filled if args.apply else suggestions} "
          f"reference updates across {len(leads)} leads with calls")
    if args.apply:
        from lead_pipeline import export_crm
        export_crm(db, Path("exports"))
        print("exports refreshed")


if __name__ == "__main__":
    main()
