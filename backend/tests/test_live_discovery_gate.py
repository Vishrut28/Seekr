"""When a search goes live, and what it sends.

A test request for "natual language procesing" with limit=1 went to the free
live sources although the corpus answered it 35 times over -- the one-result
page looked thin -- and sent them the misspelt text, which brought back a
Caribbean historian, a network-security researcher and a building designer.
Twelve unrelated people were stored.
"""

import pytest

from rip.ingest import ingest_profile
from rip.nlq import parse
from rip.normalize import EvidenceItem
from tests.test_resolution import make_profile


def person(session, ext, name, *topics):
    ingest_profile(session, make_profile(
        source="openalex", source_type="scholarly", external_id=ext,
        url=f"https://openalex.org/{ext}", raw={"id": ext}, name=name, usernames=[],
        evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics]))
    session.commit()


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a well-answered query must not go live")
    monkeypatch.setattr("rip.connectors.get_connector", refuse)


def test_a_small_page_does_not_make_a_full_answer_thin(session, no_network):
    from rip.api import nl_query

    for i in range(12):
        person(session, f"c{i}", f"Crypto Person{i}", "Cryptography")
    resp = nl_query(q="cryptography", limit=1, offset=0, discover="auto", db=session)
    assert resp["count"] == 1 and resp["total_matches"] == 12
    assert resp["discover_available"] is False


def test_a_repaired_phrase_is_what_the_sources_are_asked(session):
    from rip.discovery import repaired_query

    person(session, "n", "Nell Sample", "Natural Language Processing")
    parsed = parse(session, "natual language procesing researchers")
    assert repaired_query(parsed) == "natural language processing researchers"
    # nothing repaired: the question goes as typed
    assert repaired_query(parse(session, "Natural Language Processing")) == "Natural Language Processing"


def test_a_full_question_source_receives_the_repair(session, monkeypatch):
    from rip import discovery

    person(session, "n", "Nell Sample", "Natural Language Processing")
    asked = []

    def fake(search_for, limit):
        asked.append(search_for)
        return []

    monkeypatch.setattr(discovery, "enabled_searchers", lambda: (("openalex", fake, True),))
    parsed = parse(session, "natual language procesing")
    discovery.discovery_suggestions(session, parsed, allow_paid=False, persist=False)
    assert asked == ["natural language processing"]
