"""One paper, one row: copies of one work under a person are merged, and a
refresh does not bring them back (rip.papers)."""

from rip.ingest import ingest_profile
from rip.models import Authorship, Person, Publication
from rip.normalize import PublicationData
from rip.papers import merge_duplicate_papers, same_work
from tests.test_resolution import make_profile


def scholar(session, ext, name, papers, source="openalex"):
    person = ingest_profile(session, make_profile(
        source=source, source_type="scholarly", external_id=ext,
        url=f"https://example.org/{ext}", raw={"id": ext}, name=name, usernames=[],
        publications=papers))
    session.commit()
    return person


def held(session, person):
    return sorted(p.title for p in session.query(Publication).join(
        Authorship, Authorship.publication_id == Publication.id).filter(
        Authorship.person_id == person.id))


def test_a_copy_of_a_paper_already_held_is_not_stored_again(session):
    """A refresh listing the paper under another id -- an ORCID work entry,
    no DOI -- reuses the row rather than storing the work twice."""
    papers = [PublicationData(title="Rotornet: a scalable optical datacenter network",
                              external_id="W1", doi="10.1145/1", citations=40)]
    ada = scholar(session, "a", "Ada Lovelace", papers)
    scholar(session, "a", "Ada Lovelace", papers + [
        PublicationData(title="RotorNet: A Scalable Optical Datacenter Network",
                        external_id="orcid-work:1", citations=3)])
    assert held(session, ada) == ["Rotornet: a scalable optical datacenter network"]
    assert session.query(Publication).one().citations == 40


def test_copies_already_stored_are_merged_into_the_openalex_one(session):
    ada = scholar(session, "a", "Ada Lovelace", [])
    rows = [Publication(title="Inside the social network's datacenter network", external_id="orcid-work:1"),
            Publication(title="Inside the Social Network's (Datacenter) Network", external_id="orcid-work:2",
                        doi="10.1145/2", citations=900),
            Publication(title="Inside the social network's datacenter network", external_id="W9"),
            Publication(title="A different paper about networks", external_id="W10")]
    session.add_all(rows)
    session.flush()
    bob = Person(canonical_name="Bob Coauthor")
    session.add(bob)
    session.flush()
    session.add_all([Authorship(person_id=ada.id, publication_id=r.id) for r in rows])
    # a co-author holding one of the copies follows it to the one that stays
    session.add(Authorship(person_id=bob.id, publication_id=rows[0].id))
    session.commit()
    got = merge_duplicate_papers(session)
    session.commit()
    assert got == {"works": 1, "copies": 2}
    keep = session.query(Publication).filter(Publication.external_id == "W9").one()
    assert keep.doi == "10.1145/2" and keep.citations == 900
    assert len(held(session, ada)) == 2
    assert held(session, bob) == ["Inside the social network's datacenter network"]


def test_two_publications_of_one_title_stay_two():
    def pub(doi=None, date=None):
        return Publication(title="Fair capacitated clustering of large graphs", doi=doi,
                           published_date=date)
    # a conference paper and its journal version
    assert not same_work(pub("10.1145/1", "2019"), pub("10.1109/2", "2020"))
    # far apart in time
    assert not same_work(pub(None, "2005"), pub(None, "2012"))
    # a preprint and its published version are one work
    assert same_work(pub("10.48550/arXiv.1", "2023"), pub("10.1109/3", "2023"))
