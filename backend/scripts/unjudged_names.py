"""People whose name no rule in personhood.py can read.

    python scripts/unjudged_names.py
    python scripts/unjudged_names.py --refuse     # those the gate now rejects

The gate is a set of word lists, plus the CJK rules those lists cannot
express. A name written only in Cyrillic, Arabic, Hebrew, Thai or Devanagari
matches none of them, so it is accepted for want of any reason to refuse it.
That is a gap, not a judgement: "Машинное обучение" is Russian for "machine
learning" and would be refused instantly in English.

The asymmetry personhood.py is built on says keep them -- refusing every name
this codebase cannot parse would lose most of the world, which is the opposite
of the point. So they are listed instead, and the number is the honest measure
of how far the gate reaches.

--refuse lists the people the gate would now reject, which is what
scripts/purge_person.py will act on. Ingest only gates new records, so a rule
added today leaves everything stored before it.
"""

import argparse
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("RIP_DATABASE_URL"))
    parser.add_argument("--refuse", action="store_true",
                        help="list the people the gate would now reject")
    args = parser.parse_args()
    if args.db:
        os.environ["RIP_DATABASE_URL"] = args.db

    from rip.db import SessionLocal
    from rip.models import Person
    from rip.personhood import assess
    from sqlalchemy import select

    with SessionLocal() as session:
        people = session.execute(
            select(Person).where(Person.merged_into.is_(None))
        ).scalars().all()
        refused, unreadable = [], []
        for person in people:
            verdict = assess(person.canonical_name, "openalex")
            if not verdict.is_person:
                refused.append((person, verdict))
            elif not verdict.judged:
                unreadable.append(person)

    if args.refuse:
        print(f"{len(refused)} of {len(people)} would be refused by the gate as it "
              f"stands today:\n")
        for person, verdict in refused:
            print(f"  {person.id[:8]}  {(person.canonical_name or '')[:34]:<36}"
                  f"{verdict.reason}")
        if refused:
            print("\nIngest gates new records only, so these were stored before the "
                  "rule existed.\nscripts/purge_person.py removes one, by id, and "
                  "refuses anything still called a person.")
        return

    print(f"{len(unreadable)} of {len(people)} people have a name no rule here can "
          f"read.\nThey are kept: a script this file cannot parse is a gap in the "
          f"file, not\nevidence about the person.\n")
    for person in unreadable:
        print(f"  {person.id[:8]}  {(person.canonical_name or '')[:34]:<36}"
              f"{person.country or '--'}")
    print(f"\n{len(refused)} would be refused outright; see --refuse.")


if __name__ == "__main__":
    main()
