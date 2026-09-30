"""Wikidata connector: identity hub linking sources we cannot crawl."""

import pytest

from rip.connectors.wikidata import WikidataConnector
from rip.ingest import ingest_profile
from rip.models import Evidence, Person

ENTITY = {
    "labels": {"en": {"value": "Geoffrey Hinton"}},
    "aliases": {"en": [{"value": "Geoff Hinton"}, {"value": "G. E. Hinton"}]},
    "descriptions": {"en": {"value": "British-Canadian computer scientist"}},
    "claims": {
        "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
        "P496": [{"mainsnak": {"datavalue": {"value": "0000-0002-1111-2222"}}}],
        "P2037": [{"mainsnak": {"datavalue": {"value": "geoffhinton"}}}],
        "P1960": [{"mainsnak": {"datavalue": {"value": "JicYPdAAAAAJ"}}}],
        "P2456": [{"mainsnak": {"datavalue": {"value": "10/3248"}}}],
        "P108": [{"mainsnak": {"datavalue": {"value": {"id": "Q180865"}}}}],
        "P69": [{"mainsnak": {"datavalue": {"value": {"id": "Q160302"}}}}],
        "P101": [{"mainsnak": {"datavalue": {"value": {"id": "Q197536"}}}}],
        "P166": [{"mainsnak": {"datavalue": {"value": {"id": "Q185667"}}}}],
    },
}
LABELS = {"Q180865": "University of Toronto", "Q160302": "University of Edinburgh",
          "Q197536": "deep learning", "Q185667": "Turing Award"}


def profile():
    return WikidataConnector.normalize(
        WikidataConnector.__new__(WikidataConnector), "Q92894", ENTITY, LABELS
    )


def test_extracts_identity_links_and_facts():
    p = profile()
    assert p.name == "Geoffrey Hinton"
    assert p.orcid == "0000-0002-1111-2222"
    assert "github:geoffhinton" in p.usernames
    assert any("scholar.google.com" in u for u in p.linked_urls)
    assert any("dblp.org/pid/10/3248" in u for u in p.linked_urls)
    assert ("University of Toronto", "worked_at") in [(o.name, o.relation) for o in p.organizations]
    assert ("University of Edinburgh", "studied_at") in [(o.name, o.relation) for o in p.organizations]


def test_non_humans_rejected():
    connector = WikidataConnector.__new__(WikidataConnector)
    entity = dict(ENTITY, claims={"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q43229"}}}}]})
    connector._get = lambda params: {"entities": {"Q1": entity}}
    with pytest.raises(ValueError, match="not a person"):
        connector.fetch("Q1")


def test_links_dblp_record_that_name_alone_would_miss(session):
    """The point of Wikidata: a strong key joins records with unlike names."""
    from rip.normalize import NormalizedProfile

    dblp = NormalizedProfile(
        source="dblp", source_type="scholarly", external_id="10/3248",
        url="https://dblp.org/pid/10/3248", raw={"name": "G. E. Hinton"},
        name="G. E. Hinton", websites=["https://dblp.org/pid/10/3248"],
    )
    p1 = ingest_profile(session, dblp)
    p2 = ingest_profile(session, profile())
    assert p1.id == p2.id
    assert session.query(Person).filter(Person.merged_into.is_(None)).count() == 1
    awards = {e.value for e in session.query(Evidence).filter_by(attribute_type="award")}
    assert "Turing Award" in awards


def test_renormalize_from_raw(session):
    p = ingest_profile(session, profile())
    record = p.identities[0].source_record
    again = WikidataConnector.renormalize(
        WikidataConnector.__new__(WikidataConnector), record.external_id, record.raw
    )
    assert again.name == "Geoffrey Hinton"
