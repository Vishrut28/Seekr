"""Placing unplaced papers by DOI, so the conflation detector can see their people.

ORCID, Europe PMC and Semantic Scholar bring papers without OpenAlex's
taxonomy, and a person made only of such papers could never be reported: no
paper had a subject the detector could compare. These pin down that a DOI
lookup fixes that, that it never overrides a paper's own placed topics, and
that the lookup itself asks each DOI once. No network: the connector is faked.
"""

from rip.connectors.openalex import OpenAlexConnector
from rip.ingest import ingest_profile
from rip.models import Publication, WorkTopics
from rip.normalize import PublicationData
from scripts.place_topics_by_doi import placed, store, unplaced_dois
from tests.test_foreign_work import PLACE
from tests.test_resolution import make_profile

from rip import conflation


def _orcid_person(session, papers):
    """Papers as ORCID brings them: a DOI, co-authors, and no topics at all."""
    person = ingest_profile(session, make_profile(
        source="orcid", source_type="scholarly", external_id="0000-0001-2345-6789",
        url="https://orcid.org/0000-0001-2345-6789", raw={}, name="Ada Olesen",
        usernames=["orcid:0000-0001-2345-6789"],
        publications=[PublicationData(title=title, external_id=f"doi:{doi}", doi=doi,
                                      topics=[], raw_authors=["Ada Olesen", coauthor],
                                      published_date=str(2005 + i))
                      for i, (title, doi, coauthor) in enumerate(papers)]))
    session.commit()
    return person


CAREER = [(f"Superconductors {i}", f"10.1000/sc{i}", "Bob Physicist") for i in range(8)]
INTRUDER = ("Phylogeny of the shorebirds", "10.1000/birds", "Cal Birder")


def _openalex_says(session, doi, topic):
    session.add(WorkTopics(doi=doi, openalex_id=f"W-{doi}", topics=[
        {"name": topic, "subfield": PLACE[topic][0], "field": PLACE[topic][1],
         "domain": PLACE[topic][2]}]))
    session.commit()


def test_a_person_with_no_placed_paper_is_invisible_until_looked_up(session):
    who = _orcid_person(session, [*CAREER, INTRUDER])
    assert conflation.split_of(session, who.id).foreign == {}          # blind

    for _title, doi, _co in CAREER:
        _openalex_says(session, doi, "Superconductivity")
    _openalex_says(session, INTRUDER[1], "Bird Phylogeny")

    split = conflation.split_of(session, who.id)
    intruder = session.query(Publication).filter_by(doi=INTRUDER[1]).one()
    assert split.foreign == {intruder.id: "domain"}
    assert who.id in [s.person_id for s in conflation.candidates(session)]
    # and the review page shows the subject it was judged on
    [shown] = conflation.foreign_evidence(session, split)
    assert shown["topics"] == ["Bird Phylogeny"]


def test_a_paper_whose_own_topics_place_keeps_them():
    """The lookup fills a gap; it never overrides what the paper's source said."""
    rows = [(i, f"doi:10.1/{i}", ["Superconductivity"], ["Ada", "Bob"], "2010", f"10.1/{i}")
            for i in range(8)]
    rows.append((99, "doi:10.1/x", ["Superconductivity"], ["Ada", "Cal"], "2010", "10.1/x"))
    by_doi = {"10.1/x": ["Bird Phylogeny"]}
    split = conflation._build("p", "Ada", rows, {}, dict(PLACE), by_doi)
    assert split.foreign == {}


def test_a_doi_is_asked_about_once_and_a_miss_is_remembered(session):
    _orcid_person(session, [CAREER[0], INTRUDER])
    assert set(unplaced_dois(session)) == {"10.1000/sc0", "10.1000/birds"}

    known = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/SC0",
             "topics": [{"display_name": "Superconductivity",
                         "subfield": {"display_name": PLACE["Superconductivity"][0]},
                         "field": {"display_name": PLACE["Superconductivity"][1]},
                         "domain": {"display_name": PLACE["Superconductivity"][2]}}]}
    assert store(session, ["10.1000/sc0", "10.1000/birds"], [known]) == (1, 1)
    session.commit()

    hit, miss = session.get(WorkTopics, "10.1000/sc0"), session.get(WorkTopics, "10.1000/birds")
    assert hit.openalex_id == "W1" and hit.topics == placed(known)
    assert miss.openalex_id is None and miss.topics == []
    assert unplaced_dois(session) == []


def test_the_lookup_asks_fifty_at_a_time_and_never_sends_filter_syntax(monkeypatch):
    asked = []

    def fake_get_json(self, url, params=None):
        asked.append(params["filter"].removeprefix("doi:").split("|"))
        return {"results": []}

    monkeypatch.setattr(OpenAlexConnector, "get_json", fake_get_json)
    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
    dois = [f"10.1/{i}" for i in range(120)] + ["10.1/a|b", "10.1/a,b", "10.1/0"]
    OpenAlexConnector().works_by_doi(dois)
    assert [len(chunk) for chunk in asked] == [50, 50, 20]
    assert not any("|" in d or "," in d for chunk in asked for d in chunk)
