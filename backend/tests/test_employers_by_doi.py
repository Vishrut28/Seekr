"""Employers read from the author line a person has on their own papers."""

from rip.employers import employers_by_doi, own_author_line, without_employer
from rip.ingest import ingest_profile
from rip.models import Affiliation, Organization, Person
from rip.normalize import PublicationData
from tests.test_resolution import make_profile


def scholar(session, ext, name, dois):
    ingest_profile(session, make_profile(
        source="semanticscholar", source_type="scholarly", external_id=ext,
        url=f"https://example.org/{ext}", raw={"id": ext}, name=name, usernames=[],
        publications=[PublicationData(title=f"A study of agents number {i}", external_id=f"{ext}-{i}",
                                      doi=d, published_date="2023-01-01")
                      for i, d in enumerate(dois)]))
    session.commit()
    return session.query(Person).filter(Person.canonical_name == name).one()


def work(doi, *lines, date="2023-05-01"):
    return {"id": f"https://openalex.org/W{doi[-1]}", "doi": f"https://doi.org/{doi}",
            "publication_date": date,
            "authorships": [{"author": {"display_name": n}, "institutions": [{"display_name": i}]}
                            for n, i in lines]}


def employers(session, person):
    return sorted(o.name for o in session.query(Organization).join(
        Affiliation, Affiliation.organization_id == Organization.id).filter(
        Affiliation.person_id == person.id))


def test_their_own_author_line_names_the_employer(session):
    ada = scholar(session, "a", "Ada Lovelace", ["10.1/1", "10.1/2"])
    assert without_employer(session) == [ada.id]
    works = [work("10.1/1", ("Ada Lovelace", "University of Cambridge"), ("Bob Coauthor", "MIT")),
             work("10.1/2", ("A. Lovelace", "University of Cambridge"), ("Cy Other", "Oxford"))]
    got = employers_by_doi(session, [ada.id], lambda dois: works)
    assert got["people"] == 1 and employers(session, ada) == ["University of Cambridge"]
    assert ada.current_organization == "University of Cambridge"


def test_a_paper_with_two_authors_of_that_name_says_nothing():
    w = work("10.1/3", ("Wei Zhang", "Tsinghua University"), ("Wei Zhang", "Peking University"))
    assert own_author_line("Wei Zhang", w) is None
    assert own_author_line("Wei Zhang", work("10.1/4", ("Wei Zhang", "Tsinghua University"))) is not None
    # another person of the same surname is not this one
    assert own_author_line("Poonam Sharma", work("10.1/5", ("Aman Sharma", "GLA University"))) is None
