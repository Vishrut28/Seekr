"""What each judged criterion actually selects out of the corpus it grades.

    python scripts/audit_judgments.py --db sqlite:///corpus-copy.db
    python scripts/audit_judgments.py --terms          # the loose-term report
    python scripts/audit_judgments.py --case h-chemists

The criteria in evaluation/judgments.json were written by the same machine
that built the ranker they score, and that is the standing risk: when both
make the same mistake, the benchmark certifies it rather than catching it.
On 2026-09-22 this script found six criteria grading the wrong field, and
two queries scoring a PERFECT 1.000 for exactly that reason -- the ranker
matched "neuroscientists" on "neural" and so did the criterion, so both
agreed that Geoffrey Hinton is a neuroscientist.

Every one of the six failed the same way: a `strong` term of ONE WORD.
A word carries its field only in company. "synthesis" is image synthesis,
speech synthesis, protein synthesis and chemical synthesis; "pathology" in
medicine is the disease itself, not the discipline; "brain" is brain
metastases. `--terms` is therefore the report to read first: for every
one-word term it prints how many people that term ALONE brings to grade 2,
and what their stated topics actually say. A term pulling in people whose
topics have nothing to do with the query is the whole finding.

Two numbers no criterion can fix, printed with the table:

  RELEVANT SHARE. A query calling a sixth of the corpus relevant cannot
  separate a good ranker from a mediocre one.

  THE RECALL CEILING. recall@50 cannot exceed 50/|relevant|, so a query with
  126 relevant people is capped at 0.40 however good the ranker is. The
  ceiling is reported, never divided out: dividing would raise the score most
  where the criterion is loosest, which is backwards.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evaluation.grader import _mentions, grade, load_cases, load_profiles  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("RIP_DATABASE_URL"))
    parser.add_argument("--terms", action="store_true",
                        help="what each one-word strong term brings in on its own")
    parser.add_argument("--case", help="one case id, in full")
    parser.add_argument("--at", type=int, default=50, help="k for the recall ceiling")
    return parser.parse_args(argv)


def graded(case, profiles):
    return {pid: grade(case, p) for pid, p in profiles.items()}


def ceiling(relevant: int, at: int = 50) -> float:
    """The best recall@AT any ranker could reach on a query with RELEVANT
    people. Fifty results cannot hold 126 of them. Reported, never divided
    out: normalising by it would award 1.00 for returning a fifth of everyone,
    which flatters the loosest criterion most."""
    if not relevant:
        return 0.0
    return min(1.0, at / relevant)


def term_report(cases, profiles, only=None):
    """Who a one-word term brings in that no other term of its case would.

    Alone is the test. A word sharing its case with the phrase that really
    means the query is doing no harm; a word that is the ONLY reason someone
    is graded relevant is the whole criterion for that person.
    """
    reported = 0
    for case in cases:
        if only and case.id != only:
            continue
        singles = [p for p in case.strong if len(p) == 1]
        if not singles:
            continue
        for phrase in singles:
            rest = [q for q in case.strong if q != phrase]
            alone = [p for p in profiles.values()
                     if grade(case, p) == 2 and _mentions(p.topics, [phrase])
                     and not _mentions(p.topics, rest)]
            if not alone:
                continue
            reported += 1
            print(f"{case.id:<18}{case.query[:32]:<34}'{' '.join(phrase)}' alone "
                  f"brings {len(alone)}")
            for p in alone[:6]:
                said = [" ".join(t) for t in p.topics if phrase[0] in t]
                print(f"      {p.name[:26]:<28}{said[:2]}")
    if only and not reported:
        print(f"{only}: no one-word term is the only reason anyone grades 2")
    return reported


def main(argv=None) -> None:
    import io

    args = parse_args(argv)
    if args.db:
        os.environ["RIP_DATABASE_URL"] = args.db
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    from rip.db import SessionLocal

    session = SessionLocal()
    profiles = load_profiles(session)
    cases = load_cases()
    total = len(profiles)
    if not total:
        sys.exit("no living people in this database: nothing to audit")

    if args.terms or args.case:
        term_report(cases, profiles, args.case)
        if args.case:
            return
        print()

    rows = []
    for case in cases:
        marks = graded(case, profiles)
        two = sum(1 for v in marks.values() if v == 2)
        relevant = sum(1 for v in marks.values() if v > 0)
        rows.append((case, two, relevant))
    rows.sort(key=lambda r: -r[2])

    print(f"{total} living people, {len(cases)} criteria\n")
    print(f"{'id':<18}{'kind':<11}{'g2':>4}{'rel':>5}{'share':>7}{'ceiling':>9}  query")
    for case, two, relevant in rows:
        top = ceiling(relevant, args.at)
        flag = " <-" if relevant and top < 1.0 else ""
        print(f"{case.id:<18}{case.kind:<11}{two:>4}{relevant:>5}"
              f"{100 * relevant / total:>6.1f}%{top:>9.2f}  {case.query[:34]}{flag}")

    dead = [c.id for c, _, relevant in rows if not relevant]
    capped = [c.id for c, _, relevant in rows if relevant > args.at]
    ceilings = [ceiling(r, args.at) for _, _, r in rows if r]
    print(f"\nmean ceiling on recall@{args.at}: {sum(ceilings) / len(ceilings):.3f}"
          f"   ({len(capped)} of {len(cases)} queries capped below 1.0)")
    if dead:
        print(f"grading nobody, so counted in no metric: {', '.join(dead)}")


if __name__ == "__main__":
    main()
