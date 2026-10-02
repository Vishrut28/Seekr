"""Score Seekr, the self-written criteria and a naive baseline against the
relevance labels in evaluation/relevance_labels.json.

The pool (scripts/draw_relevance_pool.py) recorded how each card entered it:
`seekr` is search's top ten for the query, `criteria` a salted sample of people
evaluation/judgments.json calls relevant, `words` a salted sample of people
holding every content word of the query, `random` anyone. A card can carry
several arms. These measures were fixed before any were computed:

    per arm      over the cards that arm brought in: the share graded 2, the
                 share graded 1 or 2, the mean grade; a 95% Wilson interval on
                 the share graded 2 (cards within a query are not independent,
                 so read it as narrower than the truth), and the same share
                 averaged per query, so a query with many cards does not count
                 for more
    by kind      the share graded 2 per arm, per kind of query
    criteria     on every card, the grade judgments.json gives (0/1/2) against
                 the label: the 3x3 table and exact agreement -- the labels
                 testing the benchmark this project wrote for itself
    seekr top 10 the share of Seekr's top ten the criteria call relevant next
                 to the share the labels do: how much the old benchmark
                 flatters search
    pool recall  per query, of the cards labelled 2 anywhere in the pool, the
                 share in Seekr's top ten. Relative to this pool only, and the
                 pool holds just ten Seekr cards a query, so it is a floor.

Search's order inside its top ten was not kept in the pool, so there is no
rank-aware measure (nDCG) here: only what came back, not where.

    python scripts/score_relevance_labels.py      # writes evaluation/relevance_scores.json

Criteria grades need profiles, so this reads a COPY of rip.db with the
connectors tripwired, as every benchmark here must.
"""

import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

EVAL = HERE.parent / "evaluation"
ARMS = ("seekr", "criteria", "words", "random")
OUT = EVAL / "relevance_scores.json"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 3), round(centre + half, 3))


def share(grades: list[int], at_least: int) -> float | None:
    return round(sum(g >= at_least for g in grades) / len(grades), 3) if grades else None


def arm_scores(labels: dict[str, int], arms: dict[str, list[str]],
               query_of: dict[str, str]) -> dict[str, dict]:
    """Per arm: how the cards it brought in were graded."""
    out = {}
    for arm in ARMS:
        cards = [c for c in labels if arm in arms[c]]
        grades = [labels[c] for c in cards]
        per_query: dict[str, list[int]] = defaultdict(list)
        for c in cards:
            per_query[query_of[c]].append(labels[c])
        macro = [sum(g == 2 for g in gs) / len(gs) for gs in per_query.values()]
        twos = sum(g == 2 for g in grades)
        out[arm] = {
            "cards": len(cards),
            "queries": len(per_query),
            "graded_2": share(grades, 2),
            "graded_2_wilson95": list(wilson(twos, len(grades))),
            "graded_1_or_2": share(grades, 1),
            "mean_grade": round(sum(grades) / len(grades), 3) if grades else None,
            "graded_2_per_query_mean": round(sum(macro) / len(macro), 3) if macro else None,
        }
    return out


def by_kind(labels: dict[str, int], arms: dict[str, list[str]],
            kind_of: dict[str, str]) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for kind in sorted(set(kind_of.values())):
        out[kind] = {arm: share([labels[c] for c in labels
                                 if kind_of[c] == kind and arm in arms[c]], 2)
                     for arm in ARMS}
    return out


def agreement(labels: dict[str, int], criteria: dict[str, int]) -> dict:
    """The criteria's grade against the label, on every card both cover."""
    both = [c for c in labels if c in criteria]
    table = [[0] * 3 for _ in range(3)]
    for c in both:
        table[criteria[c]][labels[c]] += 1
    exact = sum(table[i][i] for i in range(3))
    return {
        "cards": len(both),
        "rows_criteria_cols_label": table,
        "exact": round(exact / len(both), 3) if both else None,
        "criteria_2_label_0": table[2][0],
        "criteria_0_label_2": table[0][2],
    }


def pool_recall(labels: dict[str, int], arms: dict[str, list[str]],
                query_of: dict[str, str]) -> dict:
    per_query = {}
    for q in sorted(set(query_of.values())):
        relevant = [c for c in labels if query_of[c] == q and labels[c] == 2]
        if relevant:
            per_query[q] = round(sum("seekr" in arms[c] for c in relevant) / len(relevant), 3)
    values = list(per_query.values())
    return {"mean": round(sum(values) / len(values), 3) if values else None,
            "per_query": per_query}


def criteria_grades(pool: dict) -> dict[str, int]:
    """judgments.json's grade for every card, from a copy of the corpus."""
    from scripts.draw_relevance_pool import tripwire

    live = HERE.parent / "rip.db"
    work = Path(tempfile.mkdtemp(prefix="relevance-scores-")) / "corpus.db"
    src, dst = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True), sqlite3.connect(work)
    src.backup(dst)
    src.close()
    dst.close()
    os.environ["RIP_DATABASE_URL"] = f"sqlite:///{work.as_posix()}"
    tripwire()

    from evaluation.grader import grade, load_cases, load_profiles
    from rip.db import SessionLocal

    cases = {c.id: c for c in load_cases()}
    with SessionLocal() as session:
        profiles = load_profiles(session)
    out = {}
    for cid, card in pool["cards"].items():
        prof = profiles.get(card["person_id"])
        if prof is not None:
            out[cid] = grade(cases[card["query_id"]], prof)
    return out


def main() -> None:
    pool = json.loads((EVAL / "relevance_pool.json").read_text(encoding="utf-8"))
    labelled = json.loads((EVAL / "relevance_labels.json").read_text(encoding="utf-8"))
    labels = {c: v["grade"] for c, v in labelled["labels"].items()}
    assert set(labels) == set(pool["cards"]), "every pooled card must be labelled"
    arms = pool["arms"]
    query_of = {c: card["query_id"] for c, card in pool["cards"].items()}
    kinds = {q["id"]: q["kind"] for q in pool["queries"]}
    kind_of = {c: kinds[q] for c, q in query_of.items()}

    criteria = criteria_grades(pool)
    seekr = [c for c in labels if "seekr" in arms[c] and c in criteria]
    report: dict[str, Any] = {
        "_comment": "Written by scripts/score_relevance_labels.py; its docstring defines every measure.",
        "labels": {"cards": len(labels), "graded": {g: sum(v == g for v in labels.values())
                                                    for g in (2, 1, 0)}},
        "arms": arm_scores(labels, arms, query_of),
        "by_kind_graded_2": by_kind(labels, arms, kind_of),
        "criteria_vs_labels": agreement(labels, criteria),
        "seekr_top10": {
            "cards": len(seekr),
            "criteria_call_relevant": share([criteria[c] for c in seekr], 1),
            "labels_graded_2": share([labels[c] for c in seekr], 2),
            "criteria_call_2": share([criteria[c] for c in seekr], 2),
            "labels_graded_1_or_2": share([labels[c] for c in seekr], 1),
        },
        "seekr_pool_recall": pool_recall(labels, arms, query_of),
        "cards_without_profile": sorted(set(labels) - set(criteria)),
    }
    OUT.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "seekr_pool_recall"}, indent=1))
    print("seekr pool recall (mean of queries):", report["seekr_pool_recall"]["mean"])


if __name__ == "__main__":
    main()
