"""Split the judged queries into DESIGN and JUDGE halves for the field-shelf rule.

    python scripts/split_field_shelf_queries.py            # print the split
    python scripts/split_field_shelf_queries.py --write    # record it

The rule under test keeps an OpenAlex field or subfield label only when the
topics filed under it carry at least a share THETA of the person's topic work.
THETA is chosen on the design half and judged once on the other, so the
choice cannot be fitted to the queries that decide whether it is kept.

Within each kind of query the ids are ordered by sha256(SALT + id) and dealt
alternately, design first, so every kind is split evenly and nobody chose
which query went where.
"""

import argparse
import hashlib
import json
import os
from collections import defaultdict

HERE = os.path.dirname(__file__)
JUDGMENTS = os.path.join(HERE, "..", "evaluation", "judgments.json")
OUT = os.path.join(HERE, "..", "evaluation", "field_shelf_split.json")
SALT = "seekr field shelf split 2026-09-27"
THETAS = [0.0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30]


def split() -> tuple[list[str], list[str]]:
    with open(JUDGMENTS, encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]
    by_kind = defaultdict(list)
    for case in cases:
        by_kind[case["kind"]].append(case["id"])
    design: list[str] = []
    judge: list[str] = []
    for kind in sorted(by_kind):
        ordered = sorted(by_kind[kind],
                         key=lambda i: hashlib.sha256((SALT + i).encode()).hexdigest())
        for n, case_id in enumerate(ordered):
            (design if n % 2 == 0 else judge).append(case_id)
    return sorted(design), sorted(judge)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    design, judge = split()
    print(f"design {len(design)}, judge {len(judge)}")
    if not args.write:
        return
    record = {
        "_comment": [
            "Fixed 2026-09-27, BEFORE any run of the share rule, by",
            "scripts/split_field_shelf_queries.py.",
            "",
            "THE RULE: an OpenAlex subfield or field label is emitted for a person",
            "only if the counts of their top-15 topics filed under it make up at",
            "least THETA of the counts of all their top-15 topics. THETA = 0 is",
            "today's behaviour (every shelf of every topic).",
            "",
            "PROTOCOL:",
            "1. For each THETA in `thetas`, on a copy of the corpus: reparse the",
            "   OpenAlex records offline, rebuild the index, run",
            "   scripts/eval_ranking.py. Only DESIGN-half figures are looked at.",
            "2. Choose THETA on the design half: the highest mean nDCG@10, ties to",
            "   the smaller THETA, excluding any THETA under which a design query",
            "   loses more than 0.10 nDCG@10 against THETA = 0.",
            "3. If the choice is 0, stop: nothing is adopted and the judge half",
            "   is never scored.",
            "4. Otherwise score the chosen THETA against THETA = 0 on the JUDGE",
            "   half, once. ADOPT only if, on the judge half: (a) mean nDCG@10",
            "   does not fall; (b) mean recall@50 does not fall by more than",
            "   0.01; (c) no judge query loses more than 0.10 nDCG@10; (d) mean",
            "   nDCG@10 or mean P@10 rises.",
            "5. Anything changed after step 4 is marked tuned.",
            "",
            "The grader reads topics, stated interests and titles, never",
            "research_field, so the relevance judgments cannot move with the rule.",
            "",
            "PREDICTIONS, written before any run:",
            "- a-physicists and k-physics-us improve: people filed under Physics",
            "  by one small topic stop matching.",
            "- h-radiologists survives at the chosen THETA: its people reach it",
            "  through one LARGE topic, which is a large share.",
            "- name, typo and protected queries do not move.",
            "- Falsified if the chosen THETA breaks a shelf query the way the",
            "  two-topic rule broke h-radiologists (a loss over 0.10).",
        ],
        "split_at": "2026-09-27",
        "salt": SALT,
        "thetas": THETAS,
        "design": design,
        "judge": judge,
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, indent=1)
        fh.write("\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
