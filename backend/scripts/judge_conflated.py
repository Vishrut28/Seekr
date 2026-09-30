"""Run the conflation detector, then ask a model to judge what it found.

    python scripts/judge_conflated.py --dry-run     # what it would ask, free
    python scripts/judge_conflated.py               # ask (costs money)
    python scripts/judge_conflated.py --benchmark   # score against the labels

Needs ANTHROPIC_API_KEY (or an `ant auth login` profile) and the anthropic
package. It sends publication titles, subjects and years — public bibliographic
data — and no names from .env, no keys, and nothing about whoever is running it.

--benchmark judges only the records in evaluation/conflation_labels.json and
prints precision and recall, so the second stage can be compared against the
detector's measured 50% rather than assumed to beat it.
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
    parser.add_argument("--above", type=float, default=None,
                        help="also judge group ratios at or above this score")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the questions and stop, without asking")
    parser.add_argument("--benchmark", action="store_true",
                        help="judge the hand-labelled records and score the result")
    parser.add_argument("--limit", type=int, default=0, help="stop after N records")
    args = parser.parse_args()

    from sqlalchemy import select

    from rip import conflation, conflation_judge
    from rip.db import SessionLocal, init_db
    from rip.models import Person

    init_db()
    truth: dict[str, int] = {}
    with SessionLocal() as session:
        if args.benchmark:
            with open(LABELS, encoding="utf-8") as fh:
                raw = json.load(fh)
            wanted = dict.fromkeys(raw["conflated"], 1) | dict.fromkeys(raw["one_person"], 0)
            splits = []
            hierarchy = conflation.topic_hierarchy(session)
            for short, label in wanted.items():
                person = session.execute(
                    select(Person).where(Person.id.like(short + "%"))
                ).scalars().first()
                if person is None:
                    print(f"  missing from this database, not judged: {short}")
                    continue
                truth[person.id] = label
                splits.append(conflation.split_of(session, person.id, person.canonical_name,
                                                  hierarchy=hierarchy))
        else:
            splits = conflation.candidates(session, above=args.above)
        if args.limit:
            splits = splits[: args.limit]

        asked = [(s, conflation.describe(session, s, per_group=conflation_judge.TITLES_PER_GROUP))
                 for s in splits]

    print(f"{len(asked)} records to judge\n")
    if args.dry_run:
        for split, groups in asked:
            print("=" * 72)
            print(conflation_judge._question(split.name, groups))
        print("nothing was sent; drop --dry-run to ask")
        return

    client, why = conflation_judge.client_or_reason()
    if client is None:
        sys.exit(f"cannot ask: {why}")

    verdicts = []
    for split, groups in asked:
        try:
            out = conflation_judge.judge(client, split.person_id, split.name, groups)
        except Exception as exc:                 # one bad record must not end the run
            print(f"  {split.name}: {type(exc).__name__}: {exc}")
            continue
        verdicts.append(out)
        mark = {"several_people": "SPLIT ", "one_person": "one   ",
                "unsure": "unsure"}[out.verdict]
        print(f"  {mark} {(out.name or '?')[:26]:<28} {out.reason[:74]}")

    if not truth:
        splits_found = sum(1 for v in verdicts if v.conflated)
        print(f"\n{splits_found} of {len(verdicts)} judged as several people")
        return

    # Scored the same way as the detector: a record is "flagged" when the
    # judgement says several people. "unsure" is not a flag — it is the model
    # declining, and counting it either way would flatter or punish unfairly.
    hit = [v for v in verdicts if v.conflated]
    true = sum(1 for v in hit if truth.get(v.person_id))
    total = sum(1 for v in verdicts if truth.get(v.person_id))
    unsure = sum(1 for v in verdicts if v.verdict == "unsure")
    print(f"\njudged {len(verdicts)} labelled records, {unsure} unsure")
    if hit and total:
        print(f"precision {true / len(hit):.0%}   recall {true / total:.0%}"
              f"   (detector alone: 50% and 86%)")
    for v in verdicts:
        want = truth.get(v.person_id)
        if want is not None and v.conflated != bool(want):
            print(f"  WRONG  {(v.name or '?')[:24]:<26} said {v.verdict:<15} {v.reason[:60]}")


if __name__ == "__main__":
    main()
