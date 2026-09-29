"""Score the conflation detector against the records judged by hand.

    python scripts/measure_conflation.py
    python scripts/measure_conflation.py --cover 0.4 0.6

Labels live in evaluation/conflation_labels.json. Every label records the ARM
that selected it, and the arm decides what it can measure -- which is the
whole reason this script is not one table.

  PRECISION comes from the flagged arm: of the records the detector reports,
  how many really are several people. Nothing else can answer that.

  RECALL comes from the independent and random arms only. Those were chosen
  by career span, by breadth across the OpenAlex domain hierarchy, and by
  unbiased draw -- none of which the detector reads. Recall over the flagged
  arm is 100% by construction, because the flagged arm IS what it found. The
  previous label set was drawn entirely from the detector's own candidates and
  reported 86% recall, which only ever meant: of the conflations it pointed
  at, it pointed at 86%.

A label can also stop being usable while the record is still here. Acting on
what the detector found -- splitting a record into the people it described, or
merging it away -- repairs the very thing the label describes. The detector is
then RIGHT to score it low, but a run that still counts it as a conflation to
be found books that as a miss, and the detector appears to have regressed
because it worked. Remediated positives are excluded from the denominator and
listed, and below a floor of usable positives the affected figure is withheld:
three or four surviving records cannot settle anything, and a number printed
from them gets quoted as though they could.

Records labelled `unsure` are scored by nothing, and `not_a_person` is a
different defect that happened to surface during labelling.

THE JUDGING DRAW (evaluation/conflation_judge_draw.json) is scored last and is
the figure to quote. The default detector -- foreign work or a break -- was
DESIGNED on conflation_labels.json, so its numbers there are printed but are
not evidence of anything; the draw was fixed and labelled before it existed.
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LABELS = os.path.join(os.path.dirname(__file__), "..", "evaluation",
                      "conflation_labels.json")
JUDGE_DRAW = os.path.join(os.path.dirname(__file__), "..", "evaluation",
                          "conflation_judge_draw.json")
# Below this many usable positives, precision and recall move by whole records
# and mean nothing. Five of the previous set's seven positives were repaired by
# acting on them, which is the success case, not a reason to quote leftovers.
MIN_POSITIVES = 5
RECALL_ARMS = ("independent", "random")


def standing(label: int, person, repairs, ruled: str | None, labelled_at) -> tuple:
    """Is this label still something the detector can be scored against?

    "repaired" -- the record was split into the people it held, or merged away,
    AFTER it was labelled. The detector now scores it low and is RIGHT to;
    counting it as a conflation still to be found books a success as a miss.

    Only repairs later than LABELLED_AT count, and getting that wrong is not
    academic: this set was written after twenty splits, and five of its records
    had been split already. They were labelled from the titles they hold NOW --
    one still mixes superconductivity theory with thermal metrology, because
    the earlier split took a different group off it. Treating any past split as
    a repair threw out five valid positives and shrank the benchmark by a
    quarter on its first run.

    "disputed" -- the label and the review table assert different things about
    a record nobody has repaired, so one of them is wrong and a human has to
    say which. Checked only AFTER repair, because a ruling on a record that was
    split describes what is LEFT of it, not what the label described: one
    V. Mishra record was labelled conflated, split, and the remainder then
    correctly ruled one_person. That is the process working, not a dispute.
    """
    when = repairs.get(person.id)
    if label and when is not None and when > labelled_at:
        return "repaired", f"{'merged away' if person.merged_into else 'split'} {when:%Y-%m-%d}"
    if ruled and (ruled == "one_person") == bool(label):
        return "disputed", (f"labelled {'conflated' if label else 'one_person'}, "
                            f"ruled {ruled}")
    return "measure", ""


def by_arm(rows) -> tuple:
    """Which rows can measure precision, and which can measure recall.

    The whole design of this script is in these three lines. A record the
    detector flagged cannot tell you what the detector misses -- it is, by
    definition, something it found -- so recall is computed ONLY over records
    that some other signal selected. A record in both arms counts for
    precision and not for recall: the detector did flag it, so including it
    would credit the detector for finding what it was pointed at.

    Getting this wrong is how the previous label set came to report 86%
    recall. Every one of its records came from the detector's own candidates.
    """
    flagged = [r for r in rows if "flagged" in r["arms"]]
    independent = [r for r in rows
                   if "flagged" not in r["arms"]
                   and any(a in RECALL_ARMS for a in r["arms"])]
    return flagged, independent, [r for r in independent if r["label"]]


def score_rows(rows, score_at, cover_at, employer_check):
    """The rows the detector would report at these settings.

    EITHER signal reports a record, so the score threshold cannot hide one the
    temporal break found — that is the whole point of adding the break, and a
    sweep that ANDed them would have undone it. The employer check applies to
    the score only: it asks whether two bodies of work share an institution,
    which says nothing about a paper fifty years from anything else.
    """
    return [r for r in rows
            if ((r["score"] >= score_at and r["cover"] >= cover_at
                 and not (employer_check and r["employer"] is True))
                or r["break_years"] > 0)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score", nargs="*", type=float, default=[0.3, 0.5, 0.7, 0.9])
    parser.add_argument("--cover", nargs="*", type=float, default=[0.0, 0.4, 0.6])
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db
    from rip.models import ChangeLog, ConflationReview, Person, PersonSplit
    from sqlalchemy import select

    from rip import conflation

    raw = json.load(open(LABELS, encoding="utf-8"))
    arms = raw.get("arms", {})
    # No date means every repair looks later than the labelling, which is the
    # safe direction: it under-counts positives rather than scoring a record
    # whose label may predate the work done to it.
    labelled_at = datetime.fromisoformat(raw.get("labelled_at") or "1970-01-01")
    wanted = dict.fromkeys(raw["conflated"], 1)
    wanted |= dict.fromkeys(raw["one_person"], 0)

    init_db()
    rows, missing, repaired, disputed = [], [], [], []
    with SessionLocal() as session:
        # When each record was last taken apart. A split records its own date;
        # a merge leaves no column to read, but writes a ChangeLog row naming
        # the person that was folded away.
        repairs: dict = {}

        def note_repair(person_id, when):
            if person_id and when and when > repairs.get(person_id, when):
                repairs[person_id] = when
            else:
                repairs.setdefault(person_id, when)

        for person_id, when in session.execute(
                select(PersonSplit.from_person_id, PersonSplit.split_at)):
            note_repair(person_id, when)
        for old_value, when in session.execute(
                select(ChangeLog.old_value, ChangeLog.changed_at)
                .where(ChangeLog.field == "merge")):
            note_repair((old_value or "").removeprefix("person:").split(" ")[0], when)
        verdicts = {row.person_id: row.verdict
                    for row in session.execute(select(ConflationReview)).scalars()}
        hierarchy = conflation.topic_hierarchy(session)
        for short, label in wanted.items():
            person = session.execute(
                select(Person).where(Person.id.like(short + "%"))
            ).scalars().first()
            if person is None:
                missing.append(short)
                continue
            state, note = standing(label, person, repairs,
                                   verdicts.get(person.id), labelled_at)
            if state == "repaired":
                repaired.append(f"{short} ({note})")
                continue
            if state == "disputed":
                disputed.append(f"{short} {note}")
            split = conflation.split_of(session, person.id, person.canonical_name,
                                        hierarchy=hierarchy)
            big = [g for g in split.groups if len(g) >= conflation.MIN_GROUP]
            rows.append({
                "short": short, "label": label,
                "arms": arms.get(short, []),
                "score": split.score,
                "break_years": split.break_years,
                "cover": (sum(len(g) for g in big[:2]) / split.papers) if split.papers else 0.0,
                "employer": conflation.shares_an_employer(session, split),
                "default": split.reportable,
                "name": person.canonical_name,
            })
        judged_design = judge_draw_rows(session, conflation, "round1")
        judged = judge_draw_rows(session, conflation, "round2")

    positives = len(raw["conflated"])
    print(f"labelled {positives} conflated, {len(raw['one_person'])} one_person, "
          f"{len(raw.get('unsure', {}))} unsure (scored by nothing), "
          f"{len(raw.get('not_a_person', {}))} not a person")
    print(f"labelled_at {raw.get('labelled_at', 'unrecorded')}; "
          f"{len(rows)} usable here")
    if missing:
        print(f"MISSING from this database, so not measured: {', '.join(missing)}")
    if repaired:
        print(f"REPAIRED since labelling, so no longer a conflation to find "
              f"({len(repaired)}): {', '.join(repaired)}")
    for note in disputed:
        print(f"DISPUTED: {note}")

    flagged, independent, true_independent = by_arm(rows)

    # ---- precision, from the arm the detector chose -------------------------
    print("\nTHE GROUP RATIO AND THE BREAK -- the default until 2026-09-26; the")
    print("  ratio now reports only when asked for (rip.conflation.REPORT_ABOVE)")
    print(f"\nPRECISION, over the {len(flagged)} records the detector flagged")
    can_measure = sum(1 for r in flagged if r["label"]) >= MIN_POSITIVES
    if not can_measure:
        print("  withheld: too few unrepaired positives in this arm to mean anything")
    else:
        for employer_check in (False, True):
            print(f"  employer check {'on' if employer_check else 'off'}")
            print(f"  {'score>=':<9}{'cover>=':<9}{'flagged':>8}{'right':>7}"
                  f"{'wrong':>7}{'precision':>11}")
            for score_at in args.score:
                for cover_at in args.cover:
                    hit = score_rows(flagged, score_at, cover_at, employer_check)
                    right = sum(1 for r in hit if r["label"])
                    share = right / len(hit) if hit else 0.0
                    print(f"  {score_at:<9}{cover_at:<9}{len(hit):>8}{right:>7}"
                          f"{len(hit) - right:>7}{share:>10.0%}")

    # ---- recall, from arms the detector had no part in choosing -------------
    print(f"\nRECALL, over the {len(true_independent)} conflations found WITHOUT "
          f"the detector\n  (arms: {', '.join(RECALL_ARMS)}; "
          f"{len(independent)} records, {len(true_independent)} of them conflated)")
    can_recall = len(true_independent) >= MIN_POSITIVES
    if not can_recall:
        print("  withheld: too few independently-found positives to mean anything")
    else:
        print(f"  {'score>=':<9}{'found':>7}{'missed':>8}{'recall':>9}")
        for score_at in args.score:
            found = [r for r in true_independent
                     if r["score"] >= score_at or r["break_years"] > 0]
            print(f"  {score_at:<9}{len(found):>7}{len(true_independent) - len(found):>8}"
                  f"{len(found) / len(true_independent):>8.0%}")
        worst = [r for r in true_independent
                 if r["score"] < min(args.score) and r["break_years"] == 0]
        if worst:
            print(f"\n  scored below {min(args.score)} despite being several people:")
            for r in sorted(worst, key=lambda r: r["short"]):
                print(f"    {r['short']}  {r['score']:.2f}  {(r['name'] or '?')[:34]}")

    # ---- what the corpus is actually like ----------------------------------
    drawn = [r for r in rows if "random" in r["arms"]]
    if drawn:
        rate = sum(1 for r in drawn if r["label"]) / len(drawn)
        print(f"\nBASE RATE, from the {len(drawn)} unbiased draws: "
              f"{rate:.0%} conflated")

    # ---- the default, on the labels it was designed on -------------------
    print("\nDEFAULT DETECTOR (foreign work or a break) on these labels -- it was")
    print("  designed on them, so this shows the fit, not how well it works")
    hit = [r for r in rows if r.get("default")]
    right = sum(1 for r in hit if r["label"])
    print(f"  reports {len(hit)}: {right} conflated, {len(hit) - right} one_person; "
          f"finds {sum(1 for r in true_independent if r.get('default'))} of "
          f"{len(true_independent)} independently found conflations")

    report_judge_draw(judged_design, "JUDGING DRAW, round 1 -- it chose today's threshold, "
                      "so this shows the fit")
    report_judge_draw(judged)

    if not (can_measure or can_recall):
        # Nothing was measured, so exiting 0 would let a caller read the run as
        # a pass. The set needs re-labelling against the corpus as it now is:
        # scripts/find_conflated.py lists candidates for the flagged arm, and
        # the label file's own comment says how to sample the other two.
        sys.exit("\nnothing here can be measured: re-label against the current "
                 "corpus before using this to judge a change")


def judge_draw_rows(session, conflation, round_: str = "round2") -> list[dict]:
    """One round of the judging draw's labels, and whether the default reports them.

    ROUND_ is "round1" (the draw's top-level labels, which chose today's
    threshold and so no longer judge it) or "round2" (the labels that judged
    it). Runs candidates() itself rather than rebuilding its logic here, so
    the figure is for exactly what the review queue shows.
    """
    if not os.path.exists(JUDGE_DRAW):
        return []
    from rip.models import Person
    from sqlalchemy import select

    with open(JUDGE_DRAW, encoding="utf-8") as fh:
        draw = json.load(fh)
    labels = (draw.get("labels", {}) if round_ == "round1"
              else draw.get(round_, {}).get("labels", {}))
    reported = {s.person_id[:8] for s in conflation.candidates(session)}
    out = []
    for short, entry in labels.items():
        if entry["label"] not in ("conflated", "one_person"):
            continue
        if session.execute(select(Person.id).where(Person.id.like(short + "%"))).first() is None:
            continue
        out.append({"short": short, "label": entry["label"] == "conflated",
                    "reported": short in reported})
    return out


def report_judge_draw(judged: list[dict],
                      title: str = "JUDGING DRAW, round 2 -- the figure to quote") -> None:
    """Precision and recall on draws fixed before what they judge was built."""
    positives = [r for r in judged if r["label"]]
    print(f"\n{title}")
    if len(positives) < MIN_POSITIVES:
        print(f"  not scored: {len(positives)} of its conflated records are in this "
              f"database, too few to mean anything")
        return
    hit = [r for r in judged if r["reported"]]
    found = sum(1 for r in hit if r["label"])
    print(f"  finds {found} of {len(positives)} conflations "
          f"({found / len(positives):.0%}); of the {len(hit)} records it reports "
          f"here, {found} are conflated"
          + (f" ({found / len(hit):.0%})" if hit else ""))


if __name__ == "__main__":
    main()
