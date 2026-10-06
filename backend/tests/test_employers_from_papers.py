"""Employers from the institutions a person puts on their own papers.

OpenAlex's single "last known institution" filed Koray Kavukcuoglu at John
Brown University and Alex Graves at Merck Serono -- on none of their papers,
while twelve and nine of them said Google DeepMind. A search for Google
DeepMind researchers found one person.
"""

from rip.connectors.openalex import OpenAlexConnector, employers_from_papers
from rip.ingest import ingest_profile
from rip.models import Affiliation, Organization, Person


def work(i, year, *institutions, author="A1"):
    return {"id": f"https://openalex.org/W{i}", "display_name": f"Paper number {i} on agents",
            "title": f"Paper number {i} on agents", "publication_date": f"{year}-01-01",
            "authorships": [
                {"author": {"id": f"https://openalex.org/{author}"},
                 "institutions": [{"display_name": n, "country_code": "GB", "type": "company"}
                                  for n in institutions]},
                # a co-author's institution is not this person's
                {"author": {"id": "https://openalex.org/A999"},
                 "institutions": [{"display_name": "Somebody Else's University"}]}]}


def koray(last_known="John Brown University"):
    works = ([work(i, 2014 + i % 10, "Google DeepMind (United Kingdom)") for i in range(12)]
             + [work(100 + i, 2009, "New York University") for i in range(3)]
             + [work(200 + i, 2012, "Shandong University of Political Science and Law") for i in range(2)]
             + [work(300 + i, 2013) for i in range(8)])
    author = {"id": "https://openalex.org/A1", "display_name": "Koray Kavukcuoglu",
              "last_known_institutions": [{"display_name": last_known, "country_code": "US"}]}
    return author, works


def test_employers_come_from_their_own_papers():
    author, works = koray()
    orgs, country, seen = employers_from_papers("A1", works)
    assert [(o.name, o.is_current) for o in orgs] == [
        ("Google DeepMind (United Kingdom)", True), ("New York University", False)]
    assert "Somebody Else's University" not in seen and country == "GB"


def test_a_last_known_institution_on_none_of_their_papers_is_dropped():
    profile = OpenAlexConnector().normalize(*koray())
    assert [o.name for o in profile.organizations] == [
        "Google DeepMind (United Kingdom)", "New York University"]
    assert profile.country == "GB"


def test_a_last_known_institution_on_one_paper_is_a_new_job():
    author, works = koray(last_known="Isomorphic Labs")
    works.append(work(400, 2024, "Isomorphic Labs"))
    names = [o.name for o in OpenAlexConnector().normalize(author, works).organizations]
    assert names[0] == "Isomorphic Labs" and "Google DeepMind (United Kingdom)" in names


def test_rereading_a_record_retracts_the_employer_it_no_longer_names(session):
    connector = OpenAlexConnector()
    author, works = koray()
    old = connector.normalize(author, [])          # last-known only, as stored before
    ingest_profile(session, old)
    person = session.query(Person).one()
    assert person.current_organization == "John Brown University"
    ingest_profile(session, connector.normalize(author, works))
    session.refresh(person)
    names = {o.name for o in session.query(Organization).join(
        Affiliation, Affiliation.organization_id == Organization.id).filter(
        Affiliation.person_id == person.id)}
    assert names == {"Google DeepMind (United Kingdom)", "New York University"}
    assert person.current_organization == "Google DeepMind (United Kingdom)"
