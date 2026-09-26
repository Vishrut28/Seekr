"""List person records that look like several people, with the evidence.

    python scripts/find_conflated.py                 # score 0.5 and up
    python scripts/find_conflated.py --above 0.3     # cast wider
    python scripts/find_conflated.py --person 74929d68

Read-only. See rip/conflation.py for what the score does and does not mean —
in particular a low score is not a clean bill of health.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--above", type=float, default=None,
                        help="also report group ratios at or above this score")
    parser.add_argument("--person", help="just this person (id or its first characters)")
    parser.add_argument("--titles", type=int, default=3, help="titles shown per group")
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db
    from rip.models import Person
    from sqlalchemy import select

    from rip import conflation

    init_db()
    with SessionLocal() as session:
        if args.person:
            person = session.execute(
                select(Person).where(Person.id.like(args.person + "%"))
            ).scalars().first()
            if person is None:
                sys.exit(f"no person whose id starts with {args.person}")
            found = [conflation.split_of(session, person.id, person.canonical_name)]
        else:
            found = conflation.candidates(session, above=args.above)
            ratio = "" if args.above is None else f", or group ratio >= {args.above}"
            print(f"{len(found)} records reported by foreign work or a break in "
                  f"time{ratio}\n")

        for split in found:
            shared = conflation.shares_an_employer(session, split)
            employer = {True: "shares an employer — probably one person",
                        False: "no employer in common",
                        None: "no institutions on file to compare"}[shared]
            print(f"{split.score:.2f}  {split.name or '?'}  [{split.person_id[:8]}]  "
                  f"{split.papers} papers, groups {split.sizes[:6]}")
            print(f"      {employer}")
            print(f"      reported for: {', '.join(split.reasons(args.above)) or 'nothing'}")
            for paper in conflation.foreign_evidence(session, split)[:args.titles]:
                print(f"      foreign ({paper['distance']}) {paper['year']}  "
                      f"{(paper['title'] or '')[:64]}")
            for group in conflation.describe(session, split, per_group=args.titles):
                print(f"      {group['papers']:>3} papers  {group['years']:<11}"
                      f"{', '.join(group['topics'][:3])}")
                for title in group["titles"]:
                    print(f"           {(title or '')[:76]}")
            print()


if __name__ == "__main__":
    main()
