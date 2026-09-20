"""The conflation queue behind the Review page.

The detector recomputes from the papers on every request and so remembers
nothing by itself. Without a record of what a person has already ruled on, a
record somebody cleared last week comes back for ever — which is the whole
reason this queue has a write endpoint at all.
"""

import pytest

from rip import api
from rip.models import ConflationReview, Person
from tests.test_conflation import one_field, researcher


def split_record(session, tag="two", name="Ann Double"):
    papers = one_field(6, "Congenital Anomalies", "Surgeon Colleague")
    papers += one_field(6, "Markov Chains", "Maths Colleague")
    return researcher(session, tag, name, papers)


def listed(session):
    return api.review_conflations(above=0.5, limit=50, db=session)


def test_a_split_record_is_offered_with_the_papers_that_split_it(session):
    who = split_record(session)
    out = listed(session)
    assert [c["person_id"] for c in out["conflations"]] == [who.id]

    entry = out["conflations"][0]
    assert entry["papers"] == 12
    # the decision is made on the work, so the work has to be in the payload
    subjects = {g["topics"][0] for g in entry["groups"]}
    assert subjects == {"Congenital Anomalies", "Markov Chains"}
    assert all(g["titles"] for g in entry["groups"])


def test_ruling_on_a_record_takes_it_out_of_the_queue(session):
    who = split_record(session)
    assert len(listed(session)["conflations"]) == 1

    out = api.review_conflation(who.id, {"verdict": "one_person"}, db=session)
    assert out["recorded"] is True

    after = listed(session)
    assert after["conflations"] == [] and after["reviewed"] == 1


def test_calling_it_several_people_changes_nobody(session):
    """Nothing here can split one source record into two people yet, so this
    records the finding. It must not quietly do something else instead."""
    who = split_record(session)
    api.review_conflation(who.id, {"verdict": "several_people"}, db=session)

    person = session.get(Person, who.id)
    assert person is not None and person.merged_into is None
    assert session.query(Person).count() == 1
    row = session.query(ConflationReview).one()
    assert row.person_id == who.id and row.verdict == "several_people"


def test_a_second_ruling_replaces_the_first(session):
    who = split_record(session)
    api.review_conflation(who.id, {"verdict": "one_person"}, db=session)
    api.review_conflation(who.id, {"verdict": "several_people"}, db=session)
    assert session.query(ConflationReview).count() == 1
    assert session.query(ConflationReview).one().verdict == "several_people"


def test_a_verdict_it_does_not_understand_is_refused(session):
    who = split_record(session)
    with pytest.raises(Exception) as caught:
        api.review_conflation(who.id, {"verdict": "maybe"}, db=session)
    assert "one_person" in str(caught.value)
    assert session.query(ConflationReview).count() == 0


def test_ruling_on_somebody_who_does_not_exist_is_a_404(session):
    with pytest.raises(Exception) as caught:
        api.review_conflation("no-such-person", {"verdict": "one_person"}, db=session)
    assert "not found" in str(caught.value)


def test_a_coherent_record_is_never_offered(session):
    researcher(session, "clean", "Ann Solo",
               one_field(10, "Forensic Pathology", "Bob Helper"))
    assert listed(session)["conflations"] == []
