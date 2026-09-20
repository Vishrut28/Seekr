"""Score the conflation detector against the records judged by hand.

    python scripts/measure_conflation.py
    python scripts/measure_conflation.py --cover 0.4 0.6

Prints precision and recall at a range of thresholds, so a proposed change to
rip/conflation.py can be argued with numbers rather than impressions. Labels
live in evaluation/conflation_labels.json and belong to one corpus snapshot;
any that are not in this database are reported as missing, not skipped
quietly, because a shrinking benchmark flatters every change made to it.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LABELS = os.path.join(os.path.dirname(__file__), "..", "evaluation",
                      "conflation_labels.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score", nargs="*", type=float,
                        default=[0.3, 0.5, 0.7, 0.9])
    parser.add_argument("--cover", nargs="*", type=float, default=[0.0, 0.4, 0.6])
    args = parser.parse_args()

    from rip import conflation
    from rip.db import SessionLocal, init_db
    from rip.models import Person
    from sqlalchemy import select

    raw = json.load(open(LABELS, encoding="utf-8"))
    wanted = {pid: 1 for pid in raw["conflated"]}
    wanted |= {pid: 0 for pid in raw["one_person"]}

    init_db()
    rows, missing = [], []
    with SessionLocal() as session:
        for short, label in wanted.items():
            person = session.execute(
                select(Person).where(Person.id.like(short + "%"))
            ).scalars().first()
            if person is None:
                missing.append(short)
                continue
            split = conflation.split_of(session, person.id, person.canonical_name)
            big = [g for g in split.groups if len(g) >= conflation.MIN_GROUP]
            cover = (sum(len(g) for g in big[:2]) / split.papers) if split.papers else 0.0
            rows.append((label, split.score, cover, person.canonical_name))

    conflated = sum(1 for label, *_ in rows if label)
    print(f"{conflated} conflated and {len(rows) - conflated} single-person records "
          f"found in this database")
    if missing:
        print(f"MISSING from this database, so not measured: {', '.join(missing)}")
    if not conflated:
        sys.exit("nothing labelled conflated is present; the numbers would mean nothing")

    print(f"\n{'score>=':<9}{'cover>=':<9}{'flagged':>8}{'hit':>5}{'miss':>6}"
          f"{'precision':>11}{'recall':>9}")
    for score_at in args.score:
        for cover_at in args.cover:
            hit = [r for r in rows if r[1] >= score_at and r[2] >= cover_at]
            true = sum(1 for label, *_ in hit if label)
            precision = true / len(hit) if hit else 0.0
            print(f"{score_at:<9}{cover_at:<9}{len(hit):>8}{true:>5}"
                  f"{len(hit) - true:>6}{precision:>10.0%}{true / conflated:>9.0%}")


if __name__ == "__main__":
    main()
