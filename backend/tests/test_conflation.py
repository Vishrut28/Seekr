"""Records that are really several people, and records that only look it.

The hard part is not spotting two bodies of work — it is not spotting them in
everybody. A productive researcher collaborates with different groups on
different subjects, and that must not read as two people.
"""

from rip import conflation
from rip.ingest import ingest_profile
from rip.normalize import PublicationData
from tests.test_resolution import make_profile


def researcher(session, tag, name, papers):
    """papers: list of (title, topics, coauthors)."""
    person = ingest_profile(session, make_profile(
        external_id=tag, url=f"https://openalex.org/{tag}", raw={"id": tag},
        name=name, usernames=[f"openalex:{tag}"],
        publications=[
            PublicationData(title=title, external_id=f"{tag}-{i}",
                            topics=list(topics), raw_authors=[name, *coauthors])
            for i, (title, topics, coauthors) in enumerate(papers)
        ]))
    session.commit()
    return person


def one_field(n, topic, coauthor):
    return [(f"Paper {i} on {topic}", [topic], [coauthor]) for i in range(n)]


def test_a_coherent_career_is_one_body_of_work(session):
    who = researcher(session, "clean", "Ann Solo", one_field(8, "Forensic Pathology", "Bob Helper"))
    split = conflation.split_of(session, who.id)
    assert split.score == 0.0, split.sizes
    assert len(split.groups) == 1


def test_a_side_interest_is_not_a_second_person(session):
    """The shape that defeated a simpler rule: most of a career in one place,
    a couple of papers somewhere else. That is range, not a conflation."""
    papers = one_field(10, "Forensic Pathology", "Bob Helper")
    papers += [("A note on teaching", ["Medical Education"], ["Cal Teacher"])] * 1
    who = researcher(session, "range", "Ann Range", papers)
    split = conflation.split_of(session, who.id)
    assert split.score < conflation.REPORT_ABOVE, split.sizes


def test_two_careers_under_one_name_are_found(session):
    """Half paediatric surgery, half combinatorics, sharing nobody and
    nothing — which is exactly one real record in this corpus."""
    papers = one_field(6, "Congenital Anomalies", "Surgeon Colleague")
    papers += one_field(6, "Markov Chains and Monte Carlo", "Maths Colleague")
    who = researcher(session, "two", "Ann Double", papers)
    split = conflation.split_of(session, who.id)
    assert split.score == 1.0, split.sizes
    assert split.sizes[:2] == [6, 6]


def test_a_shared_co_author_alone_holds_a_career_together(session):
    """Subjects can change completely over a career. One collaborator running
    through both halves is what says it is still one person."""
    papers = one_field(6, "Early Subject", "Lifelong Collaborator")
    papers += one_field(6, "Later Subject", "Lifelong Collaborator")
    who = researcher(session, "bridge", "Ann Bridge", papers)
    assert conflation.split_of(session, who.id).score == 0.0


def test_a_huge_author_list_does_not_bridge_two_separate_careers(session):
    """A 100-author paper makes every author look like every other author's
    colleague, so one such paper on each side would tie two unrelated careers
    together and hide the conflation. Author lists that long say nothing about
    who works with whom, and are not read as collaboration at all.

    Both halves here carry the SAME crowd, and nothing else in common: the
    only thing keeping them apart is the cutoff.
    """
    crowd = [f"Author Number{i}" for i in range(60)]
    papers = [(f"Physics paper {i}", ["Particle Physics"], crowd) for i in range(3)]
    papers += [(f"Biology paper {i}", ["Marine Ecology"], crowd) for i in range(3)]
    who = researcher(session, "crowd", "Ann Crowd", papers)
    split = conflation.split_of(session, who.id)
    assert split.score == 1.0, split.sizes
    assert split.sizes[:2] == [3, 3]


def test_too_few_papers_to_judge_is_not_reported(session):
    papers = one_field(2, "Subject A", "Colleague A") + one_field(2, "Subject B", "Colleague B")
    researcher(session, "thin", "Ann Thin", papers)
    assert conflation.candidates(session) == []


def test_the_report_carries_what_a_reader_needs_to_judge(session):
    papers = one_field(6, "Congenital Anomalies", "Surgeon Colleague")
    papers += one_field(6, "Markov Chains and Monte Carlo", "Maths Colleague")
    who = researcher(session, "desc", "Ann Double", papers)
    found = conflation.candidates(session)
    assert [s.person_id for s in found] == [who.id]
    groups = conflation.describe(session, found[0])
    assert len(groups) == 2
    assert {g["topics"][0] for g in groups} == {"Congenital Anomalies",
                                               "Markov Chains and Monte Carlo"}
    assert all(g["titles"] for g in groups)


def openalex_person(session, tag, name, works):
    """A person as OpenAlex hands them over: each work carrying the
    institutions THIS author put on it. works: [(title, topics, coauthors, institutions)]"""
    from rip.ingest import ingest_profile
    from rip.models import IdentityLink, SourceRecord
    from sqlalchemy import select

    person = researcher(session, tag, name,
                        [(t, tp, co) for t, tp, co, _inst in works])
    raw = {
        "author": {"id": f"https://openalex.org/{tag}", "display_name": name},
        "works": [
            {
                "id": f"https://openalex.org/W{tag}-{i}",
                "authorships": [
                    {
                        "author": {"id": f"https://openalex.org/{tag}"},
                        "institutions": [{"display_name": n} for n in inst],
                    },
                    # Somebody else on the same paper, somewhere else. Reading
                    # every authorship line instead of this author's own would
                    # let a co-author's employer stand in for theirs, and two
                    # strangers sharing one collaborator would look like one
                    # person.
                    {
                        "author": {"id": "https://openalex.org/A-somebody-else"},
                        "institutions": [{"display_name": "Shared Collaborator Institute"}],
                    },
                ],
            }
            for i, (_t, _tp, _co, inst) in enumerate(works)
        ],
    }
    record = session.execute(
        select(SourceRecord).join(IdentityLink,
                                  IdentityLink.source_record_id == SourceRecord.id)
        .where(IdentityLink.person_id == person.id)
    ).scalars().first()
    record.source = "openalex"
    record.raw = raw
    # the publication ids ingest wrote must match the work ids in the payload
    from rip.models import Authorship, Publication
    pubs = session.execute(
        select(Publication).join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person.id).order_by(Publication.id)
    ).scalars().all()
    for i, pub in enumerate(pubs):
        pub.external_id = f"https://openalex.org/W{tag}-{i}"
    session.commit()
    return person


def two_halves(inst_a, inst_b):
    a = [(f"Surgery paper {i}", ["Congenital Anomalies"], ["Surgeon Colleague"], inst_a)
         for i in range(4)]
    b = [(f"Maths paper {i}", ["Markov Chains"], ["Maths Colleague"], inst_b)
         for i in range(4)]
    return a + b


def test_one_employer_across_both_halves_says_one_person(session):
    """A career that changes subject keeps naming the same university across
    the turn. Two people filed under one name never do."""
    who = openalex_person(session, "same", "Ann Same",
                          two_halves(["Ghent University"], ["Ghent University"]))
    split = conflation.split_of(session, who.id)
    assert split.score == 1.0                      # the papers still look split
    assert conflation.shares_an_employer(session, split) is True
    assert conflation.candidates(session) == []    # ...and that is enough to drop it


def test_no_employer_in_common_leaves_the_record_flagged(session):
    who = openalex_person(session, "diff", "Ann Diff",
                          two_halves(["AIIMS Delhi"], ["University of Illinois Chicago"]))
    split = conflation.split_of(session, who.id)
    assert conflation.shares_an_employer(session, split) is False
    assert [s.person_id for s in conflation.candidates(session)] == [who.id]


def test_papers_with_no_institutions_answer_neither_way(session):
    """Most of the corpus is not from OpenAlex and carries no per-paper
    institutions. Silence must not read as either answer."""
    papers = one_field(4, "Congenital Anomalies", "Surgeon Colleague")
    papers += one_field(4, "Markov Chains", "Maths Colleague")
    who = researcher(session, "quiet", "Ann Quiet", papers)
    split = conflation.split_of(session, who.id)
    assert conflation.shares_an_employer(session, split) is None
    # unknown is not a reason to drop it
    assert [s.person_id for s in conflation.candidates(session)] == [who.id]
