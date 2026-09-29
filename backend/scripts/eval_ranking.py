"""Measure search quality against the judged queries in evaluation/judgments.json.

Runs every query through the same path /v1/query uses (parse, then progressive
execution) with live discovery off, grades the ranked people with the
criteria-based judgments, and reports NDCG@10, P@10 and recall@50 — overall,
per kind of query, and per query.

Usage (from backend/):
    python scripts/eval_ranking.py --db sqlite:///path/to/corpus.db
    python scripts/eval_ranking.py --db ... --save baseline.json
    python scripts/eval_ranking.py --db ... --compare baseline.json
    python scripts/eval_ranking.py --db ... --show c-deeplearning     # top 10, graded
    python scripts/eval_ranking.py --db ... --audit t-nlp             # who the judgments count

Compare runs on the same database only: the metrics describe a corpus as much
as a ranker.
"""

import argparse
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
parser = argparse.ArgumentParser()
parser.add_argument("--db", default=os.environ.get("RIP_DATABASE_URL"))
parser.add_argument("--save")
parser.add_argument("--compare")
parser.add_argument("--show")
parser.add_argument("--audit")
args = parser.parse_args()
if args.db:
    os.environ["RIP_DATABASE_URL"] = args.db
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evaluation.grader import grade, load_cases, load_profiles, score_case, summarize  # noqa: E402
from rip.db import SessionLocal, init_db  # noqa: E402
from rip.nlq import execute_progressive, parse  # noqa: E402


def main() -> None:
    init_db()
    cases = load_cases()
    with SessionLocal() as session:
        profiles = load_profiles(session)
        if args.audit:
            case = next(c for c in cases if c.id == args.audit)
            graded = sorted(((grade(case, p), p) for p in profiles.values()),
                            key=lambda gp: (-gp[0], gp[1].name))
            for g, p in graded:
                if g:
                    print(f"  {g}  {p.name[:40]:40s} {p.country or '':3s} "
                          f"{[' '.join(t) for t in p.topics][:4]}")
            return
        rows = []
        started = time.perf_counter()
        for case in cases:
            t = time.perf_counter()
            parsed = parse(session, case.query)
            people, _effective, _dropped = execute_progressive(session, parsed)
            ms = (time.perf_counter() - t) * 1000
            ranked = [p.id for p in people]
            row = score_case(case, ranked, profiles)
            row["ms"] = round(ms, 1)
            rows.append(row)
            if args.show == case.id:
                print(f"\n{case.query!r}: {len(ranked)} returned")
                for i, pid in enumerate(ranked[:10], 1):
                    p = profiles.get(pid)
                    print(f"  {i:2d}. grade {grade(case, p) if p else '?'}  "
                          f"{(p.name if p else pid)[:40]:40s} {[' '.join(t) for t in p.topics][:3] if p else ''}")
        total_s = time.perf_counter() - started

    summary = summarize(rows)
    summary["seconds"] = round(total_s, 2)
    print(f"\n{'id':16s} {'kind':12s} {'ret':>4s} {'rel':>4s} {'nDCG':>6s} {'P@10':>6s} {'R@50':>6s} {'ms':>6s}  query")
    for r in rows:
        def fmt(v):
            return f"{v:6.3f}" if v is not None else "     -"

        print(f"{r['id']:16s} {r['kind']:12s} {r['returned']:4d} {r['relevant_in_corpus']:4d} "
              f"{fmt(r['ndcg_at_10'])} {fmt(r['p_at_10'])} {fmt(r['recall_at_50'])} {r['ms']:6.1f}  {r['query']}")
    print("\nsummary:", json.dumps({k: v for k, v in summary.items() if k != "by_kind"}))
    for kind, s in summary["by_kind"].items():
        print(f"  {kind:12s} {json.dumps(s)}")
    for r in rows:
        if r.get("system_informed"):
            print(f"  {r['id']}: the search code was changed after this query was "
                  f"measured failing, so its score is not evidence that search "
                  f"generalises to an unseen one")
    for r in rows:
        if r.get("subject_ingested"):
            print(f"  {r['id']}: people for this subject were ingested after it "
                  f"failed, so this score is coverage added on its behalf, not "
                  f"evidence that search generalises")
    for r in rows:
        if not r["relevant_in_corpus"]:
            print(f"  {r['id']}: the corpus holds nobody this query calls relevant, "
                  f"so every figure for it is withheld")
    for r in rows:
        if r.get("subject_only"):
            print(f"  {r['id']}: nobody meets the constraint AND the subject "
                  f"({r['meets_constraint']} meet the constraint at all), so this "
                  f"score measures the subject only")

    if args.compare:
        with open(args.compare, encoding="utf-8") as fh:
            base = json.load(fh)
        before = {r["id"]: r for r in base["rows"]}
        print("\nchange against", args.compare)
        for key in ("ndcg_at_10", "p_at_10", "recall_at_50", "zero_results_with_relevant_people"):
            print(f"  {key:36s} {base['summary'][key]} -> {summary[key]}")
        for kind, s in summary["by_kind"].items():
            b = base["summary"]["by_kind"].get(kind, {})
            print(f"  {kind:12s} nDCG {b.get('ndcg_at_10')} -> {s['ndcg_at_10']}   "
                  f"R@50 {b.get('recall_at_50')} -> {s['recall_at_50']}")
        worse = [(r["id"], before[r["id"]]["ndcg_at_10"], r["ndcg_at_10"]) for r in rows
                 if r["id"] in before and (r["ndcg_at_10"] or 0) + 1e-9 < (before[r["id"]]["ndcg_at_10"] or 0)]
        print("  queries that got worse:", worse or "none")
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "rows": rows}, fh, indent=1)
        print("saved", args.save)


if __name__ == "__main__":
    main()
