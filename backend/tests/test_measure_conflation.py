"""Whether a hand-made label can still score the conflation detector.

The labels name records in one corpus snapshot, and working the queue changes
that corpus on purpose. Five of the seven records labelled "conflated" have
since been split into the people they held or merged away, which is the
detector being useful -- but a harness that still expects to find a conflation
there books each success as a miss and reports that the rule regressed. These
pin down the distinction, because the only sign of getting it wrong is a
number that looks plausible.
"""

from datetime import datetime

from rip.models import Person
from scripts.measure_conflation import standing

CONFLATED, ONE_PERSON = 1, 0
LABELLED = datetime(2026, 9, 21)
BEFORE, AFTER = datetime(2026, 9, 1), datetime(2026, 10, 1)


def call(label, person, ruled=None, repaired=None, labelled_at=LABELLED):
    repairs = {person.id: repaired} if repaired else {}
    return standing(label, person, repairs, ruled, labelled_at)


def who(session, merged_into=None):
    person = Person(canonical_name="Ann Double", merged_into=merged_into)
    session.add(person)
    session.flush()
    return person


def test_an_untouched_label_is_measured(session):
    person = who(session)
    assert call(CONFLATED, person)[0] == "measure"
    assert call(ONE_PERSON, person)[0] == "measure"


def test_a_label_whose_record_was_split_is_no_longer_a_conflation_to_find(session):
    person = who(session)
    state, note = call(CONFLATED, person, repaired=AFTER)
    assert state == "repaired" and note.startswith("split")


def test_a_split_that_happened_BEFORE_the_labelling_is_not_a_repair(session):
    """The case that cost a quarter of this benchmark on its first run. The
    set was written after twenty splits, and five of its records had already
    been split -- they were labelled from the titles they hold now, one of
    them still mixing superconductivity with thermal metrology because the
    earlier split took a different group off it. A past split is part of what
    was labelled, not a repair of it."""
    person = who(session)
    assert call(CONFLATED, person, repaired=BEFORE)[0] == "measure"


def test_a_label_whose_record_was_merged_away_is_repaired_too(session):
    survivor = who(session)
    person = who(session, merged_into=survivor.id)
    state, note = call(CONFLATED, person, repaired=AFTER)
    assert state == "repaired" and note.startswith("merged away")


def test_only_a_conflated_label_can_be_repaired(session):
    """Splitting a record labelled one_person does not excuse the label: the
    label says there was nothing to split, so a split there is a disagreement
    to settle, not a repair to discount."""
    person = who(session)
    assert call(ONE_PERSON, person, repaired=AFTER)[0] != "repaired"


def test_a_ruling_that_contradicts_the_label_is_flagged(session):
    person = who(session)
    state, note = call(CONFLATED, person, ruled="one_person")
    assert state == "disputed" and "labelled conflated, ruled one_person" in note

    assert call(ONE_PERSON, person, ruled="several_people")[0] == "disputed"


def test_a_ruling_that_agrees_with_the_label_is_not_a_dispute(session):
    person = who(session)
    assert call(CONFLATED, person, ruled="several_people")[0] == "measure"
    assert call(ONE_PERSON, person, ruled="one_person")[0] == "measure"


def test_a_split_record_ruled_one_person_afterwards_is_not_disputed(session):
    """The real sequence, and the ordering bug it caused: a V. Mishra record
    was labelled conflated, split, and what remained was then correctly ruled
    one_person. Testing the ruling first calls that a contradiction and sends
    somebody to reconcile a record that was handled exactly right."""
    person = who(session)
    state, note = call(CONFLATED, person, ruled="one_person", repaired=AFTER)
    assert state == "repaired", note


def test_the_harness_refuses_to_print_a_table_it_cannot_support(tmp_path):
    """End to end, because the floor is the whole point of the script: with
    too few usable positives it must fail loudly and say to re-label, rather
    than print precision computed from a couple of records.

    Run against its own database holding only the labelled positives, every
    one of them split after the labelling date -- which is what a future run
    looks like once this set has been worked the way the last one was.
    """
    import json
    import os
    import subprocess
    import sys
    from datetime import timedelta
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rip.db import Base
    from rip.models import Person, PersonSplit, SourceRecord

    backend = Path(__file__).resolve().parents[1]
    labels = json.loads((backend / "evaluation" / "conflation_labels.json")
                        .read_text(encoding="utf-8"))
    db = tmp_path / "rip.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    after = datetime.fromisoformat(labels["labelled_at"]) + timedelta(days=1)
    with sessionmaker(bind=engine)() as session:
        for short in labels["conflated"]:
            # an id the script's LIKE 'short%' lookup will find
            person = Person(id=f"{short}-0000-0000-0000-000000000000",
                            canonical_name="Ann Double")
            record = SourceRecord(source="openalex", source_type="scholarly",
                                  external_id=short, raw={})
            session.add_all([person, record])
            session.flush()
            session.add(PersonSplit(source_record_id=record.id,
                                    from_person_id=person.id,
                                    to_person_id=person.id, publication_ids=[],
                                    split_at=after))
        session.commit()

    env = dict(os.environ, RIP_DATABASE_URL=f"sqlite:///{db}",
               PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "scripts/measure_conflation.py"],
                          cwd=backend, env=env, capture_output=True, text=True)
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "REPAIRED since labelling" in output, output
    assert output.count("withheld") == 2, f"both figures must be withheld:\n{output}"
    assert "re-label" in output, output
    # the tables' own column header, which must not appear at all
    assert "score>=" not in output, f"a table was printed anyway:\n{output}"


def row(short, label, *arms):
    return {"short": short, "label": label, "arms": list(arms), "score": 0.0,
            "cover": 0.0, "employer": None, "name": "Ann Double"}


def test_recall_is_measured_only_where_the_detector_did_not_choose():
    """The design of the whole script. A record the detector flagged cannot
    say what the detector misses, so it must not reach the recall denominator
    -- that is how the previous label set came to report 86% recall from
    records that were all its own candidates."""
    from scripts.measure_conflation import by_arm

    rows = [row("aaaa", 1, "flagged"),
            row("bbbb", 1, "independent"),
            row("cccc", 0, "random"),
            row("dddd", 1, "flagged", "independent")]
    flagged, independent, positives = by_arm(rows)

    assert {r["short"] for r in flagged} == {"aaaa", "dddd"}
    assert {r["short"] for r in independent} == {"bbbb", "cccc"}
    assert [r["short"] for r in positives] == ["bbbb"]


def test_a_record_in_both_arms_counts_for_precision_and_not_recall():
    """It was flagged. Crediting the detector with finding what it was
    already pointed at is the error this split exists to prevent."""
    from scripts.measure_conflation import by_arm

    flagged, independent, positives = by_arm([row("x", 1, "flagged", "random")])
    assert len(flagged) == 1
    assert independent == [] and positives == []


def test_an_arm_nobody_has_vouched_for_measures_nothing():
    """RECALL_ARMS is an allow-list, not "everything except flagged".

    A later arm -- say a targeted sweep of one name group -- may well be
    influenced by what the detector reports, and would then quietly inflate
    recall the way the whole previous label set did. Adding an arm has to be
    a decision, so an unrecognised one is counted by neither figure.
    """
    from scripts.measure_conflation import by_arm

    flagged, independent, positives = by_arm([row("y", 1, "targeted-sweep")])
    assert flagged == [] and independent == [] and positives == []


def test_the_judging_draw_is_not_scored_from_a_handful(capsys):
    """Below the floor the draw is named and left unscored, like the arms."""
    from scripts.measure_conflation import report_judge_draw

    report_judge_draw([{"short": "a", "label": True, "reported": True}] * 4)
    out = capsys.readouterr().out
    assert "not scored" in out and "finds" not in out


def test_the_judging_draw_reports_recall_and_precision(capsys):
    """Recall over its conflated records; precision over what it reported."""
    from scripts.measure_conflation import report_judge_draw

    judged = ([{"short": "c", "label": True, "reported": True}] * 4
              + [{"short": "m", "label": True, "reported": False}] * 7
              + [{"short": "o", "label": False, "reported": True}] * 2
              + [{"short": "q", "label": False, "reported": False}] * 50)
    report_judge_draw(judged)
    out = capsys.readouterr().out
    assert "finds 4 of 11 conflations (36%)" in out
    assert "of the 6 records it reports here, 4 are conflated (67%)" in out
