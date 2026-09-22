"""Duplicate people are merged on proof, never on a name alone.

Each case mirrors a cluster found in the real graph.
"""

from rip.ingest import ingest_profile
from rip.models import MergeCandidate, Person
from rip.normalize import EvidenceItem, NormalizedProfile, OrgAffiliation, PublicationData

from rip import dedupe


def profile(source, ext, name, *, pubs=(), topics=(), orgs=(), orcid=None):
    return NormalizedProfile(
        source=source, source_type="scholarly", external_id=ext,
        url=f"https://{source}.example/{ext}", raw={"id": ext}, name=name, orcid=orcid,
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics],
        organizations=[OrgAffiliation(name=o) for o in orgs],
        publications=[PublicationData(title=t, external_id=pid, raw_authors=list(authors))
                      for pid, t, authors in pubs],
    )


ML_COAUTHORS = ["Priya Raman", "Arjun Mehta", "Kavya Iyer", "Sanjay Kulkarni"]


def live(session):
    return session.query(Person).filter(Person.merged_into.is_(None)).all()


def test_one_person_across_sources_is_merged(session):
    """Dhruv Dixit of BITS Hyderabad: OpenAlex and Semantic Scholar, same papers."""
    papers = [("W1", "Face recognition", ["Dhruv Dixit", *ML_COAUTHORS]),
              ("W2", "Multimodal learning", ["Dhruv Dixit", *ML_COAUTHORS[:3]])]
    ingest_profile(session, profile("openalex", "A1", "Dhruv Dixit", pubs=papers,
                                    topics=["Face Recognition", "Robotics"], orgs=["BITS Hyderabad"]))
    ingest_profile(session, profile("semanticscholar", "S1", "Dhruv Dixit", pubs=papers))

    planned = dedupe.plan(session)
    assert len(planned.merges) == 1
    merged, _ = dedupe.apply(session, planned)
    assert merged == 1
    assert len(live(session)) == 1
    # applying again finds nothing more to do
    assert dedupe.plan(session).merges == []


def test_different_orcids_are_never_merged_whatever_they_share(session):
    """Two Karan Singhs shared eight co-authors — and are two people."""
    shared = [("P1", "Paper", ["Karan Singh", *ML_COAUTHORS]),
              ("P2", "Paper two", ["Karan Singh", *ML_COAUTHORS])]
    ingest_profile(session, profile("openalex", "A1", "Karan Singh", pubs=shared,
                                    orcid="0000-0001-0000-0001"))
    ingest_profile(session, profile("openalex", "A2", "Karan Singh", pubs=shared,
                                    orcid="0000-0002-0000-0002"))
    planned = dedupe.plan(session)
    assert planned.merges == [] and planned.reviews == []


def test_a_record_linked_to_two_different_people_does_not_chain_them(session):
    """A Semantic Scholar record sharing a paper with an ML researcher and an
    ecologist must not make them one person."""
    ml_paper = ("W1", "Face recognition", ["Dhruv Dixit", *ML_COAUTHORS])
    ml_paper2 = ("W2", "Vision", ["Dhruv Dixit", *ML_COAUTHORS])
    eco_paper = ("W9", "Echinoderm ecology", ["Dhruv Dixit", "Mira Salgado"])
    ingest_profile(session, profile("openalex", "A1", "Dhruv Dixit", pubs=[ml_paper, ml_paper2],
                                    topics=["Face Recognition", "Robotics", "Neural Networks"]))
    ingest_profile(session, profile("openalex", "A9", "Dhruv Dixit", pubs=[eco_paper],
                                    topics=["Echinoderm Biology", "Marine Plants", "Poverty"]))
    ingest_profile(session, profile("semanticscholar", "S1", "Dhruv Dixit",
                                    pubs=[ml_paper, ml_paper2, eco_paper]))
    dedupe.apply(session, dedupe.plan(session))

    by_topic = {}
    for person in live(session):
        topics = {e.value for e in person.evidence if e.attribute_type == "research_interest"}
        by_topic[person.id] = topics
    ml = [pid for pid, t in by_topic.items() if "Robotics" in t]
    eco = [pid for pid, t in by_topic.items() if "Poverty" in t]
    assert ml and eco and ml != eco           # still two people


def test_a_record_matching_two_provably_different_people_goes_to_review(session):
    """Vishesh Jain: one OpenAlex record overlapped two ORCID holders equally."""
    us_papers = [(f"U{i}", "US paper", ["Vishesh Jain", *ML_COAUTHORS]) for i in range(2)]
    in_papers = [(f"I{i}", "IN paper", ["Vishesh Jain", "Neha Kapoor", "Ravi Shankar",
                                        "Amit Bose", "Leela Menon", "Tara Das"]) for i in range(2)]
    ingest_profile(session, profile("openalex", "US", "Vishesh Jain", pubs=us_papers,
                                    orcid="0000-0001-1111-1111", orgs=["Stanford"]))
    ingest_profile(session, profile("openalex", "IN", "Vishesh Jain", pubs=in_papers,
                                    orcid="0000-0002-2222-2222", orgs=["IIT Delhi"]))
    torn = [("T1", "Mixed", ["Vishesh Jain", *ML_COAUTHORS, "Neha Kapoor", "Ravi Shankar",
                             "Amit Bose", "Leela Menon", "Tara Das"])]
    ingest_profile(session, profile("openalex", "TORN", "Vishesh Jain", pubs=torn,
                                    orgs=["Stanford", "IIT Delhi"]))
    planned = dedupe.plan(session)
    assert planned.merges == []
    assert planned.reviews


def test_initials_only_names_are_reviewed_never_merged(session):
    papers = [("W1", "A", ["K. Abhay", *ML_COAUTHORS]), ("W2", "B", ["K. Abhay", *ML_COAUTHORS])]
    a = ingest_profile(session, profile("openalex", "A1", "K. Abhay", pubs=papers))
    b = ingest_profile(session, profile("semanticscholar", "S1", "K. Abhay", pubs=papers))
    planned = dedupe.plan(session)
    assert planned.merges == []
    dedupe.apply(session, planned)
    # in the review queue — proposed at ingest or by the plan, exactly once
    pair = {a.id, b.id}
    queued = [m for m in session.query(MergeCandidate)
              if {m.person_id, m.candidate_person_id} == pair]
    assert len(queued) == 1 and queued[0].status == "pending"


def test_consortium_author_lists_are_not_evidence(session):
    """Hundred-author papers made unrelated people look like close collaborators."""
    crowd = [f"Member{i} Collaboration{i}" for i in range(200)]
    # same field too — as in the real graph — so only the author cap stops it
    physics = ["Particle Physics", "Detector Development"]
    ingest_profile(session, profile("openalex", "A1", "Rahul Gupta", topics=physics,
                                    pubs=[("C1", "Detector", ["Rahul Gupta", *crowd])]))
    ingest_profile(session, profile("openalex", "A2", "Rahul Gupta", topics=physics,
                                    pubs=[("C2", "Other detector", ["Rahul Gupta", *crowd])]))
    planned = dedupe.plan(session)
    assert planned.merges == []


def test_a_pair_rejected_in_review_is_never_merged_or_reproposed(session):
    papers = [("W1", "Face recognition", ["Dhruv Dixit", *ML_COAUTHORS]),
              ("W2", "Multimodal learning", ["Dhruv Dixit", *ML_COAUTHORS])]
    a = ingest_profile(session, profile("openalex", "A1", "Dhruv Dixit", pubs=papers))
    b = ingest_profile(session, profile("semanticscholar", "S1", "Dhruv Dixit", pubs=papers))
    session.query(MergeCandidate).delete()
    session.add(MergeCandidate(person_id=a.id, candidate_person_id=b.id, score=1.0,
                               signals={}, status="rejected"))
    session.commit()
    planned = dedupe.plan(session)
    assert planned.merges == [] and planned.reviews == []


def test_applying_queues_reviews_once(session):
    papers = [("W1", "A", ["K. Abhay", *ML_COAUTHORS]), ("W2", "B", ["K. Abhay", *ML_COAUTHORS])]
    ingest_profile(session, profile("openalex", "A1", "K. Abhay", pubs=papers))
    ingest_profile(session, profile("semanticscholar", "S1", "K. Abhay", pubs=papers))
    before = session.query(MergeCandidate).count()
    dedupe.apply(session, dedupe.plan(session))
    dedupe.apply(session, dedupe.plan(session))
    assert session.query(MergeCandidate).count() <= before + 1
    assert session.query(MergeCandidate).count() >= 1
