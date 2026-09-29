"""Collapse evidence that differs from its neighbour only by letter case.

Europe PMC writes MeSH terms both ways, and evidence used to match on exact
case, so one person could carry "Artificial Intelligence" AND "Artificial
intelligence" as two separate interests — the profile listed the same subject
twice, and neither copy counted as corroborating the other. Ingest now folds
them on the way in (rip.ingest._add_evidence); this fixes the rows already
stored, which would otherwise wait for each person's next refresh.

    python scripts/fix_case_duplicates.py            # what it would do
    python scripts/fix_case_duplicates.py --yes      # do it

The spelling that arrived first wins, matching the rule ingest applies, so a
profile settles on one rather than flipping each time a source is refreshed.
Back up the database before running it with --yes.
"""

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def groups(session):
    """Every (person, attribute) whose values collide only on case.

    Keyed by the folded value; the rows come back oldest first, so the first
    is the spelling to keep.
    """
    from rip.models import Evidence
    from sqlalchemy import select

    rows = session.execute(
        select(Evidence).order_by(Evidence.observed_at, Evidence.id)
    ).scalars().all()
    buckets = defaultdict(list)
    for row in rows:
        buckets[(row.person_id, row.attribute_type, (row.value or "").lower())].append(row)
    return {k: v for k, v in buckets.items()
            if len({r.value for r in v}) > 1}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="write the changes")
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        found = groups(session)
        renamed = dropped = corroborated = 0
        for (_person_id, attribute_type, _folded), rows in sorted(found.items()):
            keep = rows[0].value
            print(f"  {attribute_type:18s} {sorted({r.value for r in rows})} -> {keep!r}")
            # one source record cannot claim the same thing twice: the second
            # row would break uq_evidence the moment the value matched
            seen_records = {rows[0].source_record_id}
            for row in rows[1:]:
                if row.source_record_id in seen_records:
                    dropped += 1
                    if args.yes:
                        session.delete(row)
                    continue
                seen_records.add(row.source_record_id)
                if row.value != keep:
                    renamed += 1
                    if args.yes:
                        row.value = keep
            # a claim two source records now agree on is corroborated, which is
            # what it should have been all along
            if len(seen_records) > 1:
                for row in rows:
                    if row.verification_state == "unverified":
                        corroborated += 1
                        if args.yes:
                            row.verification_state = "corroborated"
        if args.yes:
            session.commit()
        people = len({k[0] for k in found})
    verb = "" if args.yes else " (dry run — pass --yes to apply)"
    print(f"\n{len(found)} collisions across {people} people: "
          f"{renamed} renamed, {dropped} removed, {corroborated} corroborated{verb}")


if __name__ == "__main__":
    main()
