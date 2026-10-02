"""Fill what a merge should have carried over and did not.

    python scripts/backfill_merged_fields.py          # report
    python scripts/backfill_merged_fields.py --yes    # fill, with a change-log row each

review.merge_persons copies a field onto the keeper when the keeper has none
(review.MERGE_FILLED_FIELDS). country was added to Person after that list was
written, so a merge into a record without one dropped the country a source
had stated for the other. Found on 2026-10-03: a researcher at BITS Hyderabad,
whose OpenAlex record says India, was merged into his own record with no
country and fell out of every "in India" search.

Fill-blanks only, like ingest: a keeper's own value is never replaced, and a
tombstone that disagrees with it is left alone. The values come from the
merged-away person, which holds what its sources stated.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def missing(session) -> list[tuple[str, str, str, str]]:
    """(keeper id, field, value, merged-away id) for every blank a merge left."""
    from sqlalchemy import select

    from rip.models import Person
    from rip.review import MERGE_FILLED_FIELDS

    out = []
    for gone in session.execute(
            select(Person).where(Person.merged_into.isnot(None))).scalars():
        keep = session.get(Person, gone.merged_into)
        # a chain (A into B, B into C) resolves to its end
        seen = {gone.id}
        while keep is not None and keep.merged_into and keep.id not in seen:
            seen.add(keep.id)
            keep = session.get(Person, keep.merged_into)
        if keep is None:
            continue
        for field in MERGE_FILLED_FIELDS:
            value = getattr(gone, field)
            if value and not getattr(keep, field):
                out.append((keep.id, field, value, gone.id))
    return out


def fill(session, found) -> int:
    from rip.models import ChangeLog, Person

    done = set()
    for keep_id, field, value, gone_id in found:
        if (keep_id, field) in done:
            continue              # two tombstones offering one field: the first wins
        keeper = session.get(Person, keep_id)
        if keeper is None:
            continue
        setattr(keeper, field, value)
        session.add(ChangeLog(person_id=keep_id, field=field, old_value=None,
                              new_value=str(value)))
        session.add(ChangeLog(person_id=keep_id, field="merge_backfill",
                              old_value=f"person:{gone_id}", new_value=field))
        done.add((keep_id, field))
    session.commit()
    return len(done)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="write the fills")
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db
    from rip.models import Person

    init_db()
    with SessionLocal() as session:
        found = missing(session)
        for keep_id, field, value, gone_id in found:
            keeper = session.get(Person, keep_id)
            name = keeper.canonical_name if keeper else keep_id
            print(f"{name} ({keep_id}): {field} = {value!r}  from {gone_id}")
        if not found:
            print("nothing to fill")
        elif args.yes:
            print(f"filled {fill(session, found)}")
        else:
            print(f"{len(found)} to fill; --yes writes them")


if __name__ == "__main__":
    main()
