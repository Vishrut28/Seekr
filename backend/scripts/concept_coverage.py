"""Which subjects the concept map can widen, and which it cannot.

    python scripts/concept_coverage.py
    python scripts/concept_coverage.py --judged    # only the judged queries

A query reaches the people whose stated topic contains what was typed, and
nothing else, unless concepts.CONCEPTS knows a wider set. "Alzheimer's
researchers" found the one person whose topic says Alzheimer's and missed the
dementia researchers next to them, because the map has no entry for it.

The map is a hand-written list of 77 subjects, which does not reach worldwide,
and the two obvious ways to replace it were tried and measured first. Both
failed, and it is worth knowing why before reaching for them again.

CO-OCCURRENCE -- subjects held by the same people are related. At this corpus
size it is noise: 574 of 2,551 terms had any neighbour at all, none of the
subjects actually being missed had one, and "deep learning" came out related
to OPHTHALMOLOGY, because the handful of people carrying that literal keyword
happen to be retina-AI researchers. It encodes who is in the corpus, not what
the subjects mean.

OPENALEX'S OWN HIERARCHY -- every topic in a stored payload carries a
subfield, curated rather than inferred. Two problems. It covers 33% of the
vocabulary, missing every term in question, because payloads carry only an
author's top few topics. And a subfield is a shelf: "Wildlife Ecology and
Conservation" sits in Ecology beside "hydrology and sediment transport
processes", which is exactly the parent-category mistake that `related` lists
had to have removed from them.

So the map stays a list, and this counts what it reaches.
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
    parser.add_argument("--judged", action="store_true",
                        help="only the terms the judged queries ask about")
    args = parser.parse_args()
    if args.db:
        os.environ["RIP_DATABASE_URL"] = args.db

    from rip.concepts import CONCEPTS, related_subjects
    from rip.db import SessionLocal
    from rip.nlq import parse

    print(f"{len(CONCEPTS)} subjects have related ones listed\n")

    if args.judged:
        from evaluation.grader import load_cases

        with SessionLocal() as session:
            rows = []
            for case in load_cases():
                if case.kind == "name" or not case.query:
                    continue
                parsed = parse(session, case.query)
                for group in parsed.skill_groups:
                    term = group.get("term") or ""
                    if not term:
                        continue
                    rows.append((case.id, term, len(related_subjects(term))))
        blind = [r for r in rows if not r[2]]
        print(f"{len(blind)} of {len(rows)} query terms have no related subjects:")
        for case_id, term, _n in blind:
            print(f"  {case_id:<18}{term}")
        print(f"\nthose queries can only reach people whose stated topic contains "
              f"the words typed.")
        return

    for subject in sorted(CONCEPTS):
        print(f"  {subject:<34}{len(CONCEPTS[subject])} related")


if __name__ == "__main__":
    main()
