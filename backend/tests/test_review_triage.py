"""Triaging the merge-review queue: the pairs nobody can decide leave it, the
pairs with a verdict get one, and the judgement calls stay.

Every case here comes from the real queue, which held 196 pairs of which 18
were actually decidable by a person.
"""

from rip import dedupe
from rip.ingest import ingest_profile
from rip.models import MergeCandidate, Person
from rip.review import merge_persons, triage
from test_dedupe import ML_COAUTHORS, profile


def queue(session, a, b, score=0.9):
    """The pair as a human would find it: pending review. Ingest queues some of
    these itself, so an existing row is reused rather than duplicated."""
    mc = next((m for m in session.query(MergeCandidate).all()
               if {m.person_id, m.candidate_person_id} == {a, b}), None)
    if mc is None:
        mc = MergeCandidate(person_id=a, candidate_person_id=b, score=score,
                            signals={"reason": "near-identical name"})
        session.add(mc)
    mc.status = "pending"
    session.commit()
    return mc


def people(session, name):
    return [p for p in session.query(Person).filter(Person.merged_into.is_(None)).all()
            if p.canonical_name == name]


def test_a_pair_with_nothing_in_common_leaves_the_queue_undecided(session):
    """'Karan Singh' against a bare 'K. Singh': no paper, no co-author, no org.
    Deferring is not a verdict — the pair must stay mergeable later."""
    ingest_profile(session, profile("openalex", "A1", "Karan Singh"))
    ingest_profile(session, profile("dblp", "D1", "K. Singh"))
    a, b = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)

    out = triage(session, apply=True)
    assert [r["candidate_id"] for r in out["deferred"]] == [mc.id]
    assert session.get(MergeCandidate, mc.id).status == "deferred"
    assert len(out["merged"]) == 0 and len(out["pending"]) == 0


def test_a_deferred_pair_comes_back_once_there_is_evidence(session):
    """Deferred is 'ask me again', so dedupe may re-queue it for a human."""
    ingest_profile(session, profile("openalex", "A1", "Vishesh Jain",
                                    pubs=[("W1", "Graphs", ["Vishesh Jain", *ML_COAUTHORS])]))
    ingest_profile(session, profile("semanticscholar", "S1", "Vishesh Jain"))
    a, b = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)
    triage(session, apply=True)
    assert session.get(MergeCandidate, mc.id).status == "deferred"

    # the thin record gains a paper of its own with two of those co-authors:
    # evidence, short of proof, so a human should see the pair again
    ingest_profile(session, profile(
        "semanticscholar", "S1", "Vishesh Jain",
        pubs=[("W9", "Random graphs", ["Vishesh Jain", *ML_COAUTHORS[:2]])]))
    _, queued = dedupe.apply(session, dedupe.plan(session))
    assert queued == 1
    row = session.get(MergeCandidate, mc.id)
    assert row.status == "pending" and row.signals["shared_coauthors"] == 2


def test_two_people_with_different_orcids_are_rejected_not_left_waiting(session):
    shared = [("P1", "Paper", ["Karan Singh", *ML_COAUTHORS]),
              ("P2", "Paper two", ["Karan Singh", *ML_COAUTHORS])]
    ingest_profile(session, profile("openalex", "A1", "Karan Singh", pubs=shared,
                                    orcid="0000-0001-0000-0001"))
    ingest_profile(session, profile("openalex", "A2", "Karan Singh", pubs=shared,
                                    orcid="0000-0002-0000-0002"))
    a, b = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)

    out = triage(session, apply=True)
    assert len(out["rejected"]) == 1
    assert "ORCID" in out["rejected"][0]["reason"]
    assert session.get(MergeCandidate, mc.id).status == "rejected"


def test_a_proven_pair_is_merged_and_the_row_closed(session):
    papers = [("W1", "Face recognition", ["Dhruv Dixit", *ML_COAUTHORS]),
              ("W2", "Multimodal learning", ["Dhruv Dixit", *ML_COAUTHORS[:3]])]
    ingest_profile(session, profile("openalex", "A1", "Dhruv Dixit", pubs=papers,
                                    orgs=["BITS Hyderabad"]))
    ingest_profile(session, profile("semanticscholar", "S1", "Dhruv Dixit", pubs=papers))
    a, b = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)

    out = triage(session, apply=True)
    assert len(out["merged"]) == 1 and "shared papers" in out["merged"][0]["reason"]
    assert session.get(MergeCandidate, mc.id).status == "merged"
    assert len(people(session, "Dhruv Dixit")) == 1


def test_an_initials_name_with_shared_papers_is_left_for_a_human(session):
    """'D. Dixit' shares two papers with 'Dhruv Utpalkumar Dixit'. That is
    evidence, and still not enough to merge a name that thin."""
    papers = [("W1", "Face recognition", ["Dhruv Dixit", *ML_COAUTHORS]),
              ("W2", "Multimodal learning", ["Dhruv Dixit", *ML_COAUTHORS[:3]])]
    ingest_profile(session, profile("openalex", "A1", "Dhruv Utpalkumar Dixit", pubs=papers))
    ingest_profile(session, profile("semanticscholar", "S1", "D. Dixit", pubs=papers))
    a, b = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)

    out = triage(session, apply=True)
    assert len(out["pending"]) == 1 and "initials-only name" in out["pending"][0]["reason"]
    assert session.get(MergeCandidate, mc.id).status == "pending"
    assert len(people(session, "D. Dixit")) == 1


def test_a_row_is_closed_when_both_sides_end_up_as_one_person(session):
    """A pair queued between two records that each later merged into a third.
    The row still says "pending" and there is nothing left to ask."""
    papers = [("W1", "Face recognition", ["Dhruv Dixit", *ML_COAUTHORS]),
              ("W2", "Multimodal learning", ["Dhruv Dixit", *ML_COAUTHORS[:3]])]
    for ext in ("A1", "S1", "D1"):
        source = {"A1": "openalex", "S1": "semanticscholar", "D1": "dblp"}[ext]
        ingest_profile(session, profile(source, ext, "Dhruv Dixit", pubs=papers))
    a, b, c = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)
    merge_persons(session, c, a)
    merge_persons(session, c, b)
    assert session.get(MergeCandidate, mc.id).status == "pending"

    out = triage(session, apply=True)
    assert [r["reason"] for r in out["stale"]] == ["already one person"]
    assert session.get(MergeCandidate, mc.id).status == "merged"


def test_a_merge_never_records_the_pair_as_two_different_people(session):
    """Merging closes a pending row as merged. Marking it rejected recorded a
    veto against the very pair just proven to be one person."""
    papers = [("W1", "Face recognition", ["Dhruv Dixit", *ML_COAUTHORS])]
    ingest_profile(session, profile("openalex", "A1", "Dhruv Dixit", pubs=papers))
    ingest_profile(session, profile("semanticscholar", "S1", "Dhruv Dixit", pubs=papers))
    a, b = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)
    merge_persons(session, a, b)
    assert session.get(MergeCandidate, mc.id).status == "merged"


def test_reporting_writes_nothing(session):
    ingest_profile(session, profile("openalex", "A1", "Karan Singh"))
    ingest_profile(session, profile("dblp", "D1", "K. Singh"))
    a, b = (p.id for p in session.query(Person).all())
    mc = queue(session, a, b)

    assert len(triage(session)["deferred"]) == 1
    assert session.get(MergeCandidate, mc.id).status == "pending"


def test_a_middle_name_that_disagrees_is_not_the_same_person(session):
    """'Michael G. Aman' and 'M. Javad Aman' align on surname and first
    initial. Sharing a paper must not make them one person."""
    papers = [("W1", "Antibodies", ["Michael G. Aman", "M. Javad Aman", *ML_COAUTHORS]),
              ("W2", "More antibodies", ["Michael G. Aman", "M. Javad Aman", *ML_COAUTHORS])]
    ingest_profile(session, profile("openalex", "A1", "Michael G. Aman", pubs=papers))
    ingest_profile(session, profile("openalex", "A2", "M. Javad Aman", pubs=papers))
    a, b = (p.id for p in session.query(Person).all())
    queue(session, a, b)

    out = triage(session, apply=True)
    assert out["merged"] == [] and out["pending"] == []
    assert [r["reason"] for r in out["deferred"]] == ["names differ"]
    assert len(people(session, "Michael G. Aman")) == 1


def test_a_name_that_identifies_nobody_is_never_queued_for_review(session):
    """Where the flood came from: an existing person carries "K Singh" among
    its aliases, so every incoming "K. Singh" scored 100/100 against it and was
    queued. A name-only near miss needs a name that names someone."""
    first = profile("openalex", "A1", "Karan Singh")
    first.aliases = ["K. Singh", "Singh, Karan"]
    ingest_profile(session, first)
    ingest_profile(session, profile("dblp", "D1", "K. Singh"))
    assert session.query(Person).count() == 2
    assert session.query(MergeCandidate).count() == 0

    # a full name on both sides is still a question worth asking — and this
    # third record scores 100 against BOTH, so the thin one is skipped from
    # either direction
    ingest_profile(session, profile("semanticscholar", "S1", "Karan Singh"))
    queued = session.query(MergeCandidate).all()
    assert len(queued) == 1
    names = {session.get(Person, queued[0].person_id).canonical_name,
             session.get(Person, queued[0].candidate_person_id).canonical_name}
    assert names == {"Karan Singh"}


def test_a_record_that_fits_two_people_is_not_merged_from_the_queue(session):
    """The real case: one 'Vishesh Jain' record overlapped two others holding
    different ORCIDs. Judged as a lone pair it looks proven; the sweep sees the
    rival and says no, and triage has to follow the sweep."""
    stanford = ["Priya Raman", "Arjun Mehta", "Kavya Iyer", "Sanjay Kulkarni",
                "Rhea Pillai", "Ishan Verma"]
    delhi = ["Neha Kapoor", "Ravi Shankar", "Amit Bose", "Leela Menon",
             "Tara Das", "Farid Sheikh"]
    us_papers = [(f"U{i}", "US paper", ["Vishesh Jain", *stanford]) for i in range(2)]
    in_papers = [(f"I{i}", "IN paper", ["Vishesh Jain", *delhi]) for i in range(2)]
    ingest_profile(session, profile("openalex", "US", "Vishesh Jain", pubs=us_papers,
                                    orcid="0000-0001-1111-1111", orgs=["Stanford"]))
    ingest_profile(session, profile("openalex", "IN", "Vishesh Jain", pubs=in_papers,
                                    orcid="0000-0002-2222-2222", orgs=["IIT Delhi"]))
    torn = [("T1", "Mixed", ["Vishesh Jain", *stanford, *delhi])]
    ingest_profile(session, profile("openalex", "TORN", "Vishesh Jain", pubs=torn,
                                    orgs=["Stanford", "IIT Delhi"]))
    us, _in, ambiguous = (p.id for p in session.query(Person).all())
    mc = queue(session, us, ambiguous)

    out = triage(session, apply=True)
    assert out["merged"] == []
    assert "matches a different person almost as well" in out["pending"][0]["reason"]
    assert session.get(MergeCandidate, mc.id).status == "pending"
    assert len(people(session, "Vishesh Jain")) == 3
