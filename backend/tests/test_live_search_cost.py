"""What a live search is allowed to cost.

Two separate problems, measured against the running server.

A query naming nothing still went to eight providers. "!!! ??? ***" and
"the and of in" parse to no terms at all, and each cost a second and a half
to be told nothing, every time anybody asked.

And nothing bounded the search half. LIVE_BUDGET_SECONDS caps the fetches;
the searches were unbounded, so one provider having a bad day set the latency
of the whole query. Measured: wikidata failing after 5.2 seconds while
OpenAlex had answered at 2.3, and on another run OpenAlex itself taking 70
seconds while the sequential phase queued behind it.

Fakes throughout: these are about this code, not about how a provider feels
today.
"""

import time

import pytest

from rip import discovery, nlq
from rip.normalize import NormalizedProfile


@pytest.mark.parametrize("query, because", [
    ("!!! ??? ***", "punctuation parses to nothing"),
    ("the and of in", "stopwords parse to nothing"),
    ("12345 67890", "a bare number is not a name or a subject"),
])
def test_a_query_naming_nothing_does_not_reach_a_provider(session, monkeypatch,
                                                          query, because):
    asked = []
    monkeypatch.setattr(discovery, "SUGGESTION_SEARCHERS",
                        (("orcid", lambda q, limit: asked.append(q) or [], False),))
    out = discovery.discovery_suggestions(session, nlq.parse(session, query),
                                    allow_paid=False, persist=False)
    assert out == []
    assert asked == [], f"{because}, yet a provider was called"


def test_gibberish_is_still_searched_because_it_cannot_be_told_apart():
    """The one thing this must NOT do is guess. "zqxjv plormbat" and "photonic
    metasurface" are the same shape to a parser, and the second is exactly the
    query live discovery exists for."""
    assert discovery.worth_searching_live(["zqxjv plormbat"]) == ""
    assert discovery.worth_searching_live(["photonic metasurface"]) == ""
    assert discovery.worth_searching_live([])
    assert discovery.worth_searching_live(["12345"])
    assert discovery.worth_searching_live(["  "])


def test_a_source_that_will_not_answer_stops_holding_up_the_rest(session, monkeypatch):
    """The slow one is abandoned; the quick one's results still arrive."""
    monkeypatch.setattr(discovery, "LIVE_SEARCH_SECONDS", 0.4)

    def quick(_q, _limit):
        return [{"source": "orcid", "external_id": "quick-1", "name": "Ada Quick"}]

    def glacial(_q, _limit):
        time.sleep(5)
        return [{"source": "wikidata", "external_id": "slow-1", "name": "Ada Slow"}]

    class Fake:
        def __init__(self, *_a):
            pass

        def fetch(self, identifier):
            return NormalizedProfile(
                source="orcid", source_type="scholarly", external_id=identifier,
                url=f"https://orcid.org/{identifier}", raw={"id": identifier},
                name="Ada Quick", usernames=[f"orcid:{identifier}"])

    states = []
    monkeypatch.setattr("rip.connectors.get_connector", lambda s: Fake())
    monkeypatch.setattr(discovery, "SUGGESTION_SEARCHERS",
                        (("orcid", quick, False), ("wikidata", glacial, False)))

    started = time.perf_counter()
    out = discovery.discovery_suggestions(
        session, nlq.parse(session, "Ada Lovelace"), allow_paid=False, persist=False,
        on_source=lambda name, state, **f: states.append((name, state)))
    took = time.perf_counter() - started

    assert took < 4, f"waited {took:.1f}s for a source that was abandoned"
    assert ("wikidata", "timed out") in states
    assert any(s["external_id"] == "quick-1" for s in out), out


def test_the_sequential_phase_stops_once_the_budget_is_spent(session, monkeypatch):
    """Phase A runs one source after another, so a slow one spends the budget
    of everyone behind it. The call already running cannot be interrupted;
    nothing after it needs to start."""
    monkeypatch.setattr(discovery, "LIVE_SEARCH_SECONDS", 0.3)
    reached = []

    def slow_first(_q, _limit):
        reached.append("openalex")
        time.sleep(0.5)
        return []

    def should_not_run(_q, _limit):
        reached.append("dblp")
        return []

    states = []
    monkeypatch.setattr(discovery, "SUGGESTION_SEARCHERS",
                        (("openalex", slow_first, False), ("dblp", should_not_run, False)))
    discovery.discovery_suggestions(session, nlq.parse(session, "Ada Lovelace"),
                              allow_paid=False, persist=False,
                              on_source=lambda n, s, **f: states.append((n, s)))
    assert reached == ["openalex"], reached
    assert ("dblp", "skipped") in states
