"""A deferred pair comes back when there is something new to look at — and
brings its reason with it.

Deferring is for a pair nothing reachable can settle: it leaves the queue
without anybody claiming to know, and dedupe.plan is meant to return it "if
either person later gains evidence". It returned it whenever there was ANY
evidence, on every run, because plan() read a deferred row as "had no
evidence when last looked at". That is true of the triage's deferrals and
false of a reviewer's, who defers a pair while looking at its shared
co-authors and judges them short of proof.

apply() then made it worse: re-queueing overwrote the row's signals, and the
written reason went with them. Run against a copy of the live queue,
`rip.cli dedupe --yes` flipped 12 deferred pairs back to pending and erased
all 12 notes — the ORCID-fragment reasoning, the ALICE mass-authorship one.
Eleven of the twelve had exactly the evidence they were deferred on.
"""

import pytest
from rip.dedupe import EVIDENCE_THAT_CAN_SETTLE, grew_since_deferral
from rip.ingest import ingest_profile
from rip.models import MergeCandidate
from tests.test_dedupe import profile

from rip import dedupe

NOTE = ("two shared co-authors on one mass-authored paper; no ORCID on either "
        "record and OpenAlex holds them as different authors")


def snapshot(*, pubs=0, coauthors=2, orgs=0, topic=0.3, note=NOTE):
    return {"shared_publications": pubs, "shared_coauthors": coauthors,
            "shared_organizations": orgs, "topic_overlap": topic,
            "deferred_because": note}


@pytest.fixture()
def pair(session):
    """Two records of one name sharing two co-authors and no paper: judge()
    calls that 'some evidence, short of proof' — the kind that gets deferred."""
    a = ingest_profile(session, profile("openalex", "A1", "Asha Rao", pubs=[
        ("W1", "Face recognition", ["Asha Rao", "Priya Raman", "Arjun Mehta"])]))
    b = ingest_profile(session, profile("semanticscholar", "S1", "Asha Rao", pubs=[
        ("W9", "Speech enhancement", ["Asha Rao", "Priya Raman", "Arjun Mehta"])]))
    assert a.id != b.id, "fixture merged at ingest; it no longer tests a queue"
    session.query(MergeCandidate).delete()
    session.commit()
    planned = dedupe.plan(session)
    assert len(planned.reviews) == 1, "fixture no longer produces a review pair"
    live_signals = planned.reviews[0][2].signals
    return a, b, live_signals


def defer(session, a, b, signals):
    row = MergeCandidate(person_id=a.id, candidate_person_id=b.id, score=1.0,
                         signals=signals, status="deferred")
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------
# the rule, directly
# --------------------------------------------------------------------------

def test_nothing_recorded_means_anything_now_is_new():
    """The triage's deferrals: nothing to judge at the time."""
    assert grew_since_deferral({}, {"shared_coauthors": 1})
    assert grew_since_deferral({"deferred_because": "nothing reachable"},
                               {"shared_coauthors": 1})


@pytest.mark.parametrize("axis", EVIDENCE_THAT_CAN_SETTLE)
def test_more_of_anything_that_can_settle_a_pair_is_new(axis):
    then = snapshot()
    now = dict(then)
    now[axis] = (then.get(axis) or 0) + 1
    assert grew_since_deferral(then, now)


def test_the_same_evidence_is_not_new():
    assert not grew_since_deferral(snapshot(), snapshot())


def test_less_evidence_is_not_new():
    assert not grew_since_deferral(snapshot(coauthors=3), snapshot(coauthors=2))


def test_topic_overlap_alone_never_brings_a_pair_back():
    """A Jaccard ratio rises when a record merely LOSES an unrelated topic,
    and judge() never accepts it as proof. Ten Dhruv Dixit pairs are topic
    overlap and nothing else; they would cycle forever."""
    assert not grew_since_deferral(snapshot(topic=0.33), snapshot(topic=0.67))


# --------------------------------------------------------------------------
# through plan() and apply()
# --------------------------------------------------------------------------

def test_a_pair_deferred_on_this_evidence_stays_deferred(session, pair):
    a, b, live = pair
    defer(session, a, b, {**live, "deferred_because": NOTE})
    assert dedupe.plan(session).reviews == []


def test_a_pair_that_gained_a_co_author_comes_back(session, pair):
    a, b, live = pair
    defer(session, a, b, {**live, "shared_coauthors": live["shared_coauthors"] - 1,
                          "deferred_because": NOTE})
    assert len(dedupe.plan(session).reviews) == 1


def test_a_triage_deferral_with_nothing_recorded_comes_back(session, pair):
    """Unchanged behaviour for the 149 rows that recorded no evidence."""
    a, b, _live = pair
    defer(session, a, b, {"deferred_because": "nothing reachable settles it"})
    assert len(dedupe.plan(session).reviews) == 1


def test_bringing_a_pair_back_keeps_what_was_already_said(session, pair):
    """The note is the thing the next reviewer needs most, and apply() used
    to overwrite it."""
    a, b, live = pair
    row = defer(session, a, b, {**live, "shared_coauthors": live["shared_coauthors"] - 1,
                                "deferred_because": NOTE})
    dedupe.apply(session, dedupe.plan(session))

    session.expire_all()
    row = session.get(MergeCandidate, row.id)
    assert row.status == "pending"
    assert row.signals["previously_deferred_because"] == NOTE
    assert row.signals["evidence_when_deferred"]["shared_coauthors"] == \
        live["shared_coauthors"] - 1
    assert row.signals["shared_coauthors"] == live["shared_coauthors"]


def test_running_dedupe_again_and_again_changes_nothing(session, pair):
    """The documented workflow is `dedupe --yes`, repeated until stable. A
    deferred pair must survive being run over."""
    a, b, live = pair
    row = defer(session, a, b, {**live, "deferred_because": NOTE})
    for _ in range(3):
        dedupe.apply(session, dedupe.plan(session))

    session.expire_all()
    row = session.get(MergeCandidate, row.id)
    assert row.status == "deferred"
    assert row.signals["deferred_because"] == NOTE
