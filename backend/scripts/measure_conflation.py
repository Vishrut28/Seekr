"""Score the conflation detector against the records judged by hand.

    python scripts/measure_conflation.py
    python scripts/measure_conflation.py --cover 0.4 0.6

Prints precision and recall at a range of thresholds, so a proposed change to
rip/conflation.py can be argued with numbers rather than impressions. Labels
live in evaluation/conflation_labels.json and belong to one corpus snapshot;
any that are not in this database are reported as missing, not skipped
quietly, because a shrinking benchmark flatters every change made to it.

A label can also stop being usable while the record is still here. Acting on
what the detector found -- splitting a record into the people it described, or
merging it away -- repairs the very thing the label describes. The detector is
then RIGHT to score it low, but a run that still counts it as a conflation to
be found books that as a miss, and the detector appears to have regressed
because it worked. Remediated positives are therefore excluded from the
denominator and listed, and below a floor of usable positives the table is
withheld entirely: three or four surviving records cannot settle anything, and
a precision printed from them would be quoted as though they could.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LABELS = os.path.join(os.path.dirname(__file__), "..", "evaluation",
                      "conflation_labels.json")
# Below this many usable positives, precision and recall move by whole records
# and mean nothing. Five of seven positives were repaired by acting on them,
# which is the success case, not a reason to keep quoting the leftovers.
MIN_POSITIVES = 5


def standing(label: int, person, split_apart, ruled: str | None) -> tuple:
    """Is this label still something the detector can be scored against?

    "repaired" -- acting on the finding fixed the record the label describes,
    by splitting it into the people it held or merging it away. The detector
    now scores it low and is RIGHT to; counting it as a conflation still to be
    found books a success as a miss.

    "disputed" -- the label and the review table assert different things about
    a record nobody has repaired, so one of them is wrong and a human has to
    say which. Checked only AFTER repair, because a ruling on a record that was
    split describes what is LEFT of it, not what the label described: one
    V. Mishra record was labelled conflated, split, and the remainder then
    correctly ruled one_person. That is the process working, not a dispute.
    """
    if label and (person.id in split_apart or person.merged_into):
        return "repaired", "split" if person.id in split_apart else "merged away"
    if ruled and (ruled == "one_person") == bool(label):
        return "disputed", (f"labelled {'conflated' if label else 'one_person'}, "
                            f"ruled {ruled}")
    return "measure", ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score", nargs="*", type=float,
                        default=[0.3, 0.5, 0.7, 0.9])
    parser.add_argument("--cover", nargs="*", type=float, default=[0.0, 0.4, 0.6])
    args = parser.parse_args()

    from rip import conflation
    from rip.db import SessionLocal, init_db
    from rip.models import ConflationReview, Person, PersonSplit
    from sqlalchemy import select

    raw = json.load(open(LABELS, encoding="utf-8"))
    wanted = {pid: 1 for pid in raw["conflated"]}
    wanted |= {pid: 0 for pid in raw["one_person"]}

    init_db()
    rows, missing, repaired, disputed = [], [], [], []
    with SessionLocal() as session:
        split_apart = {
            pid for (pid,) in session.execute(select(PersonSplit.from_person_id))
        }
        verdicts = {
            row.person_id: row.verdict
            for row in session.execute(select(ConflationReview)).scalars()
        }
        for short, label in wanted.items():
            person = session.execute(
                select(Person).where(Person.id.like(short + "%"))
            ).scalars().first()
            if person is None:
                missing.append(short)
                continue
            state, note = standing(label, person, split_apart, verdicts.get(person.id))
            if state == "repaired":
                repaired.append(f"{short} ({note})")
                continue
            if state == "disputed":
                disputed.append(f"{short} {note}")
            split = conflation.split_of(session, person.id, person.canonical_name)
            big = [g for g in split.groups if len(g) >= conflation.MIN_GROUP]
            cover = (sum(len(g) for g in big[:2]) / split.papers) if split.papers else 0.0
            shared = conflation.shares_an_employer(session, split)
            rows.append((label, split.score, cover, person.canonical_name, shared))

    conflated = sum(1 for label, *_ in rows if label)
    print(f"{conflated} conflated and {len(rows) - conflated} single-person records "
          f"found in this database")
    if missing:
        print(f"MISSING from this database, so not measured: {', '.join(missing)}")
    if repaired:
        print(f"REPAIRED since labelling, so no longer a conflation to find "
              f"({len(repaired)}): {', '.join(repaired)}")
    for note in disputed:
        print(f"DISPUTED: {note}")
    positives = len(wanted) - len(raw["one_person"])
    if conflated < MIN_POSITIVES:
        # Both dead ends mean the same thing -- this set can no longer settle a
        # question -- so both say what to do about it rather than only the one
        # that happens to be reached first.
        sys.exit(f"{conflated or 'no'} unrepaired conflation(s) left of {positives} "
                 f"labelled: too few to measure anything. Re-label against the "
                 f"current corpus before using this to judge a change -- "
                 f"scripts/find_conflated.py lists candidates, and rip/conflation.py "
                 f"records what the figures were when the set was whole.")

    # Reported with the employer check both off and on, so the refinement has
    # to keep earning its place rather than being assumed to.
    for employer_check in (False, True):
        print(f"\nemployer check {'on' if employer_check else 'off'}")
        print(f"{'score>=':<9}{'cover>=':<9}{'flagged':>8}{'hit':>5}{'miss':>6}"
              f"{'precision':>11}{'recall':>9}")
        for score_at in args.score:
            for cover_at in args.cover:
                hit = [r for r in rows if r[1] >= score_at and r[2] >= cover_at
                       and not (employer_check and r[4] is True)]
                true = sum(1 for label, *_ in hit if label)
                precision = true / len(hit) if hit else 0.0
                print(f"{score_at:<9}{cover_at:<9}{len(hit):>8}{true:>5}"
                      f"{len(hit) - true:>6}{precision:>10.0%}{true / conflated:>9.0%}")


if __name__ == "__main__":
    main()
