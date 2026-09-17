"""Advisory report: suggests updated WEIGHTS / NAME_ONLY_WEIGHTS from
accumulated MatchFeedback.

This changes NOTHING in rip/nlq.py. It prints a side-by-side comparison of
the current hand-set weights against what a small logistic regression fit
to real "good"/"bad" votes would suggest, plus explicit sample-size and
data-quality caveats. A human reads the report and decides whether to
hand-edit the WEIGHTS / NAME_ONLY_WEIGHTS constants in rip/nlq.py.

MatchFeedback's own docstring (rip/models.py) says it is "deliberately
inert" and exists so a downstream tool has "labelled training data" —
this script respects that boundary. Nothing here writes back into ranking.

Usage:
    python scripts/fit_weights.py
"""
import sys

sys.path.insert(0, ".")

from rip.db import SessionLocal
from rip.models import MatchFeedback
from rip.nlq import NAME_ONLY_WEIGHTS, WEIGHTS, parse, relevance_scores
from rip.weight_fitting import (
    MIN_PER_CLASS,
    MIN_TOTAL_VOTES,
    coefficients_to_weights,
    fit_logistic_regression,
)

GENERAL_COMPONENTS = ["depth", "output", "confidence", "recency", "corroboration", "breadth"]
NAME_ONLY_COMPONENTS = ["name_fit"] + GENERAL_COMPONENTS


def collect_training_rows(session):
    """Recompute each feedback row's score components against the CURRENT
    corpus and split into (general-query rows, name-only-query rows).

    Score components are not stored at vote time — relevance is a property
    of a query, not something MatchFeedback persists, by this codebase's own
    design (see relevance_scores' docstring: "deliberately not persisted").
    Recomputing against today's corpus is the best available reconstruction,
    but it can drift from what the voter actually saw if the corpus changed
    materially since the vote — e.g. a person's evidence grew, or the query's
    vocabulary match changed. This is reported as a caveat, not hidden.

    Rows where the person has since been merged into another record or no
    longer exists (resolution/data changes since the vote) are skipped and
    counted, since scoring a tombstoned or missing person is meaningless —
    relevance_scores() itself always returns a components dict (zeros by
    default) for any id it is asked about, so "the person currently has weak
    or no matching evidence" is NOT a skip condition here: an all-zero
    component vector on a "good" vote is real, if concerning, information —
    it says the current scoring found nothing to justify a judgement a human
    made, which is exactly the kind of case worth surfacing to a fit, not
    hiding from it.
    """
    from rip.models import Person

    general_X, general_y = [], []
    name_X, name_y = [], []
    skipped = 0
    for fb in session.query(MatchFeedback).all():
        person = session.get(Person, fb.person_id)
        if person is None or person.merged_into is not None:
            skipped += 1
            continue
        parsed = parse(session, fb.query_raw)
        scored = relevance_scores(session, parsed, [fb.person_id])
        comp = scored.get(fb.person_id, {}).get("components", {})
        label = 1 if fb.verdict == "good" else 0
        if "name_fit" in comp:
            name_X.append([comp.get(c, 0.0) for c in NAME_ONLY_COMPONENTS])
            name_y.append(label)
        else:
            general_X.append([comp.get(c, 0.0) for c in GENERAL_COMPONENTS])
            general_y.append(label)
    return (general_X, general_y), (name_X, name_y), skipped


def report_for(label: str, names: list, X: list, y: list, current: dict) -> None:
    print(f"\n=== {label} ===")
    n_good = sum(y)
    n_bad = len(y) - n_good
    print(f"votes: {len(y)} (good={n_good}, bad={n_bad})")
    if len(y) < MIN_TOTAL_VOTES or min(n_good, n_bad) < MIN_PER_CLASS:
        print(
            f"Not enough data yet — need at least {MIN_TOTAL_VOTES} total "
            f"votes AND at least {MIN_PER_CLASS} of each verdict. A fit on "
            f"fewer votes than that would be reporting noise as if it were "
            f"a real finding, so nothing is suggested here yet."
        )
        return

    coefs = fit_logistic_regression(X, y)
    suggested, negative = coefficients_to_weights(names, coefs)

    print(f"\n{'component':<15}{'current':>10}{'suggested':>12}")
    print("-" * 37)
    for n in names:
        cur = current.get(n, 0.0)
        print(f"{n:<15}{cur:>10.3f}{suggested.get(n, 0.0):>12.3f}")

    if negative:
        print(
            f"\nCAUTION: {', '.join(negative)} correlated NEGATIVELY with "
            f"'good' in this data — more of that signal predicted WORSE "
            f"feedback, not better. That is either a real, actionable "
            f"finding about the current scoring, or a sign the sample is "
            f"still too small/skewed to trust. Worth checking manually "
            f"before adopting anything above; excluded from the suggested "
            f"weights rather than silently folded in."
        )

    print(
        "\nThis is advisory only — rip.nlq.WEIGHTS is unchanged. Components "
        "were recomputed against the CURRENT corpus, not a snapshot from "
        "when each vote was cast, so a corpus that changed a lot since "
        "early votes were collected makes this less reliable; re-run "
        "periodically as more votes come in rather than trusting one report."
    )


def main() -> None:
    with SessionLocal() as session:
        total_feedback = session.query(MatchFeedback).count()
        (gX, gy), (nX, ny), skipped = collect_training_rows(session)

    print(f"Total MatchFeedback rows: {total_feedback}")
    if skipped:
        print(
            f"Skipped {skipped} row(s): the person no longer matches that "
            f"query's filters against the current corpus (resolution/data "
            f"changes since the vote was cast)."
        )

    report_for("General queries -> WEIGHTS", GENERAL_COMPONENTS, gX, gy, WEIGHTS)
    report_for(
        "Name-only queries -> NAME_ONLY_WEIGHTS", NAME_ONLY_COMPONENTS, nX, ny,
        NAME_ONLY_WEIGHTS,
    )


if __name__ == "__main__":
    main()
