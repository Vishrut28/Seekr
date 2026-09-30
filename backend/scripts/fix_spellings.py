"""Repair misspelled subject keywords already stored as evidence.

A keyword is what a person typed about themselves, and ORCID keeps the slip:
four profiles here say "Reinforcment Learning". Ingest now repairs the known
misspellings on the way in (rip.ingest.correct_spelling); this fixes the rows
already stored, which would otherwise wait for each person's next refresh.

    python scripts/fix_spellings.py            # what it would do
    python scripts/fix_spellings.py --yes      # do it

What the typo cost was not only that those four were unfindable under the
right spelling. The misspelling was itself a vocabulary term, held by real
people, so a query that repeated the typo matched it exactly, the
typo-correction pass never ran, and "reinforcment learning" came back with
those four instead of the sixty who do reinforcement learning.

The corrected row records what the source said, and the source record still
holds the payload as it arrived, so nothing is rewritten out of existence.
Back up the database before running it with --yes.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("RIP_DATABASE_URL"))
    parser.add_argument("--yes", action="store_true", help="apply the repairs")
    args = parser.parse_args()
    if args.db:
        os.environ["RIP_DATABASE_URL"] = args.db

    from sqlalchemy import select

    from rip.db import SessionLocal
    from rip.ingest import SELF_TYPED_ATTRS, correct_spelling
    from rip.models import Evidence, Person

    renamed = dropped = 0
    with SessionLocal() as session:
        rows = session.execute(
            select(Evidence).where(Evidence.attribute_type.in_(tuple(SELF_TYPED_ATTRS)))
        ).scalars().all()
        for row in rows:
            fixed, mistyped = correct_spelling(row.value)
            if not mistyped:
                continue
            person = session.get(Person, row.person_id)
            # the right spelling may already be on this record from another
            # source, and two rows cannot hold the same claim
            twin = session.execute(
                select(Evidence).where(
                    Evidence.person_id == row.person_id,
                    Evidence.attribute_type == row.attribute_type,
                    Evidence.value == fixed,
                    Evidence.source_record_id == row.source_record_id,
                )
            ).scalars().first()
            # a person may have no canonical name, and slicing None crashed
            who = ((person.canonical_name if person else None) or row.person_id)[:28]
            if twin is not None:
                dropped += 1
                print(f"  drop    {who:<30} {mistyped!r} (already has {fixed!r})")
                if args.yes:
                    session.delete(row)
                continue
            renamed += 1
            print(f"  repair  {who:<30} {mistyped!r} -> {fixed!r}")
            if args.yes:
                note = f"spelled {mistyped!r} by the source"
                row.extracted_info = (f"{row.extracted_info}; {note}"
                                      if row.extracted_info else note)
                row.value = fixed
        if args.yes:
            session.commit()
            from rip.nlq import invalidate_vocab

            invalidate_vocab()

    verb = "" if args.yes else " (dry run — pass --yes to apply)"
    print(f"\n{renamed} repaired, {dropped} removed{verb}")


if __name__ == "__main__":
    main()
