"""Draw the records a conflation detector will be JUDGED on, before it is built.

    python scripts/draw_conflation_judge_set.py            # print the draw
    python scripts/draw_conflation_judge_set.py --write    # record it

Every earlier recall figure for the detector was spoiled the same way: its
signals were designed on the records they were then measured on. This fixes
the judging records first, by a rule that reads nothing a detector could use:

  population  living people with at least six papers who carry no label yet
  order       sha256(SALT + person id), ascending -- reproducible without any
              random-number generator whose output could change between
              Python versions, and not choosable after the fact

The file this writes (evaluation/conflation_judge_draw.json) is committed
before any design work, so the history shows which came first.
"""

import argparse
import hashlib
import json
import os
import sqlite3

HERE = os.path.dirname(__file__)
LABELS = os.path.join(HERE, "..", "evaluation", "conflation_labels.json")
OUT = os.path.join(HERE, "..", "evaluation", "conflation_judge_draw.json")
SALT = "seekr conflation judge draw 2026-09-26"
FIRST_BLOCK = 120
EXTENSION_BLOCK = 40
MIN_PAPERS = 6


def order(db_path: str) -> list[str]:
    with open(LABELS, encoding="utf-8") as fh:
        labels = json.load(fh)
    taken = {short for bucket in ("conflated", "one_person", "unsure", "not_a_person")
             for short in labels[bucket]}
    rows = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True).execute(
        """select p.id from person p join authorship a on a.person_id = p.id
           where p.merged_into is null group by p.id
           having count(distinct a.publication_id) >= ?""", (MIN_PAPERS,)).fetchall()
    pool = [pid for (pid,) in rows if pid[:8] not in taken]
    return sorted(pool, key=lambda pid: hashlib.sha256((SALT + pid).encode()).hexdigest())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(HERE, "..", "rip.db"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    ordered = order(args.db)
    print(f"pool {len(ordered)}; first block {min(FIRST_BLOCK, len(ordered))}")
    if not args.write:
        return
    record = {
        "_comment": [
            "The judging set for any new conflation detector. Drawn 2026-09-26",
            "BEFORE the redesign started, by scripts/draw_conflation_judge_set.py.",
            "",
            "PROTOCOL, fixed now so it cannot be bent to fit a result:",
            "1. Design uses conflation_labels.json only. Nobody opens these records",
            "   until the new detector is frozen in a commit.",
            "2. Label the first block in `order`, from titles, years and per-paper",
            "   institutions, with no detector score (old or new) on screen.",
            "   Labels: conflated, one_person, unsure, not_a_person -- the same",
            "   meanings as conflation_labels.json.",
            "3. If fewer than 10 are conflated, label the next 40 in order, and",
            "   repeat until 10 are or the order runs out. The stopping rule reads",
            "   only the labels, never a detector.",
            "4. Score the deployed detector and the frozen new one ONCE each.",
            "5. ADOPT the new one only if, on this set, it (a) finds more of the",
            "   conflated records than the deployed one, (b) at least 30% of what",
            "   it flags here is conflated, and (c) it flags no more than 20% of",
            "   the living people with six or more papers -- a queue a person can",
            "   actually read.",
            "6. Anything changed after step 4 is marked tuned, and its figures on",
            "   this set are no longer evidence.",
        ],
        "drawn_at": "2026-09-26",
        "salt": SALT,
        "population": f"living people with >= {MIN_PAPERS} papers and no label",
        "pool_size": len(ordered),
        "first_block": FIRST_BLOCK,
        "extension_block": EXTENSION_BLOCK,
        "order": [pid[:8] for pid in ordered],
        "labels": {},
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, indent=1)
        fh.write("\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
