"""Whether a hand-made label can still score the conflation detector.

The labels name records in one corpus snapshot, and working the queue changes
that corpus on purpose. Five of the seven records labelled "conflated" have
since been split into the people they held or merged away, which is the
detector being useful -- but a harness that still expects to find a conflation
there books each success as a miss and reports that the rule regressed. These
pin down the distinction, because the only sign of getting it wrong is a
number that looks plausible.
"""

from scripts.measure_conflation import standing
from rip.models import Person

CONFLATED, ONE_PERSON = 1, 0


def who(session, merged_into=None):
    person = Person(canonical_name="Ann Double", merged_into=merged_into)
    session.add(person)
    session.flush()
    return person


def test_an_untouched_label_is_measured(session):
    person = who(session)
    assert standing(CONFLATED, person, set(), None)[0] == "measure"
    assert standing(ONE_PERSON, person, set(), None)[0] == "measure"


def test_a_label_whose_record_was_split_is_no_longer_a_conflation_to_find(session):
    person = who(session)
    state, note = standing(CONFLATED, person, {person.id}, None)
    assert state == "repaired" and note == "split"


def test_a_label_whose_record_was_merged_away_is_repaired_too(session):
    survivor = who(session)
    person = who(session, merged_into=survivor.id)
    state, note = standing(CONFLATED, person, set(), None)
    assert state == "repaired" and note == "merged away"


def test_only_a_conflated_label_can_be_repaired(session):
    """Splitting a record labelled one_person does not excuse the label: the
    label says there was nothing to split, so a split there is a disagreement
    to settle, not a repair to discount."""
    person = who(session)
    assert standing(ONE_PERSON, person, {person.id}, None)[0] != "repaired"


def test_a_ruling_that_contradicts_the_label_is_flagged(session):
    person = who(session)
    state, note = standing(CONFLATED, person, set(), "one_person")
    assert state == "disputed" and "labelled conflated, ruled one_person" in note

    state, _note = standing(ONE_PERSON, person, set(), "several_people")
    assert state == "disputed"


def test_a_ruling_that_agrees_with_the_label_is_not_a_dispute(session):
    person = who(session)
    assert standing(CONFLATED, person, set(), "several_people")[0] == "measure"
    assert standing(ONE_PERSON, person, set(), "one_person")[0] == "measure"


def test_a_split_record_ruled_one_person_afterwards_is_not_disputed(session):
    """The real sequence, and the ordering bug it caused: a V. Mishra record
    was labelled conflated, split, and what remained was then correctly ruled
    one_person. Testing the ruling first calls that a contradiction and sends
    somebody to reconcile a record that was handled exactly right."""
    person = who(session)
    state, note = standing(CONFLATED, person, {person.id}, "one_person")
    assert state == "repaired", note


def test_the_harness_refuses_to_print_a_table_it_cannot_support(tmp_path):
    """End to end, because the floor is the whole point of the script: with
    too few usable positives it must fail loudly and say to re-label, not
    print precision computed from two records.

    Run against its own database, repairing all but two of the labelled
    positives -- exactly where the real corpus stands after the queue was
    worked, and the case in which a table would otherwise be printed from two
    records and quoted as though it meant something.
    """
    import json
    import os
    import subprocess
    import sys
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
    with sessionmaker(bind=engine)() as session:
        # all but two, so the run has positives and still cannot use them
        for short in list(labels["conflated"])[:-2]:
            # an id the script's LIKE 'short%' lookup will find
            person = Person(id=f"{short}-0000-0000-0000-000000000000",
                            canonical_name="Ann Double")
            record = SourceRecord(source="openalex", source_type="scholarly",
                                  external_id=short, raw={})
            session.add_all([person, record])
            session.flush()
            session.add(PersonSplit(source_record_id=record.id,
                                    from_person_id=person.id,
                                    to_person_id=person.id, publication_ids=[]))
        session.commit()

    env = dict(os.environ, RIP_DATABASE_URL=f"sqlite:///{db}",
               PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "scripts/measure_conflation.py"],
                          cwd=backend, env=env, capture_output=True, text=True)
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "REPAIRED since labelling" in output, output
    assert "too few to measure" in output, output
    assert "Re-label" in output or "re-label" in output, output
    assert "precision" not in output, f"a table was printed anyway:\n{output}"
