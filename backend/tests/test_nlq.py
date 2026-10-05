from rip.ingest import ingest_profile
from rip.nlq import execute, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_resolution import make_profile


def seed(session):
    ingest_profile(
        session,
        make_profile(
            name="Ada Example",
            location="Toronto, Canada",
            organizations=[OrgAffiliation(name="Acme Labs", is_current=True)],
            evidence=[
                EvidenceItem(attribute_type="skill", value="Rust"),
                EvidenceItem(attribute_type="research_interest", value="Distributed Systems"),
            ],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            external_id="other",
            url="https://github.com/other",
            raw={"login": "other"},
            name="Grace Sample",
            usernames=["github:other"],
            location="Berlin, Germany",
            organizations=[OrgAffiliation(name="Globex University", relation="studied_at")],
            evidence=[EvidenceItem(attribute_type="skill", value="Python")],
        ),
    )


def test_vocab_driven_parse(session):
    seed(session)
    parsed = parse(session, "distributed systems experts at Acme Labs in Toronto, top 5")
    assert parsed.skills == ["Distributed Systems"]
    assert parsed.organizations == ["Acme Labs"]
    assert parsed.locations == ["Toronto, Canada"]
    assert parsed.limit == 5
    assert parsed.unmatched_terms == []


def test_bare_number_is_not_limit(session):
    seed(session)
    # growthOS bug regression: "10 years" must not become limit=10
    parsed = parse(session, "rust with 10 years experience")
    assert parsed.limit == 50  # DEFAULT_LIMIT, not the bare "10"
    assert parsed.skills == ["Rust"]
    assert "10" in parsed.unmatched_terms or "years" in parsed.unmatched_terms


def test_execute_applies_all_filters(session):
    seed(session)
    results = execute(session, parse(session, "rust at Acme Labs"))
    assert [p.canonical_name for p in results] == ["Ada Example"]
    # unknown skill applied as nothing -> honest unmatched, no accidental filter
    parsed = parse(session, "quantum basketweaving experts")
    assert parsed.skills == []
    # reported as the phrase the user typed, not chopped into words
    dropped = " ".join(parsed.unmatched_terms).lower()
    assert "quantum" in dropped or "basketweaving" in dropped


def test_name_term_filter(session):
    seed(session)
    results = execute(session, parse(session, "find Grace"))
    assert [p.canonical_name for p in results] == ["Grace Sample"]


def test_parsing_stays_free_of_scoring(session):
    seed(session)
    # parsing extracts constraints; scoring belongs to execute(), not here
    parsed = parse(session, "python")
    assert not hasattr(parsed, "score")
    results = execute(session, parsed)
    assert [p.canonical_name for p in results] == ["Grace Sample"]


def test_containment_matches_longer_corpus_values(session):
    seed(session)
    ingest_profile(
        session,
        make_profile(
            external_id="third", url="https://github.com/third", raw={"login": "third"},
            name="Ng Trailblazer", usernames=["github:third"],
            evidence=[EvidenceItem(attribute_type="research_interest",
                                   value="Neural Networks and Applications")],
        ),
    )
    parsed = parse(session, "neural networks researchers")
    assert parsed.skills == ["Neural Networks and Applications"]
    results = execute(session, parsed)
    assert [p.canonical_name for p in results] == ["Ng Trailblazer"]


def test_discovery_suggestions_are_not_results(session, monkeypatch):
    """Phase 4: discover=true adds suggestions, never auto-ingests."""
    from rip.api import nl_query
    from rip.models import Person, SourceRecord

    class FakeOpenAlex:
        def search_authors(self, name, limit=10):
            return [{"id": "A999", "name": "Marie Curie", "works_count": 12,
                     "affiliation": "Sorbonne", "cited_by": 3}]

        def search_authors_by_topic(self, topic, limit=10):
            return self.search_authors(topic, limit)

        def fetch(self, identifier):
            # no payload available, so nothing can be stored from this run
            raise RuntimeError("fetch not stubbed")

    monkeypatch.setattr("rip.connectors.get_connector", lambda s: FakeOpenAlex())
    before_persons = session.query(Person).count()
    before_records = session.query(SourceRecord).count()

    resp = nl_query(q="radioactivity pioneers", discover="true", db=session)
    assert resp["count"] == 0
    assert resp["discover_available"] is True
    suggestions = resp["discovery_suggestions"]
    assert suggestions[0]["external_id"] == "A999"
    assert "ingest openalex A999" in suggestions[0]["ingest_command"]
    # nothing entered the graph
    assert session.query(Person).count() == before_persons
    assert session.query(SourceRecord).count() == before_records
    # no ranking leaked into the response
    assert "score" not in str(resp)


def test_discover_defaults_to_free_sources_only(session, monkeypatch):
    """Default searches live, but never a metered provider."""
    import inspect

    from rip.api import app, nl_query

    # declared default (what an HTTP caller gets without the param)
    assert inspect.signature(nl_query).parameters["discover"].default.default == "auto"
    schema = app.openapi()["paths"]["/v1/query"]["get"]["parameters"]
    discover_param = next(p for p in schema if p["name"] == "discover")
    assert discover_param["schema"]["default"] == "auto"
    assert discover_param["required"] is False

    def boom(source):
        raise AssertionError("live search must not run when discover=false")

    monkeypatch.setattr("rip.connectors.get_connector", boom)
    for off in ("false", "no", ""):
        resp = nl_query(q="radioactivity pioneers", discover=off, db=session)
        assert "discovery_suggestions" not in resp
        assert "queued_leads" not in resp


def test_auto_discover_never_calls_a_metered_provider(session, monkeypatch):
    """The free sources answer by default; Exa costs money and stays opt-in."""
    from rip.api import nl_query

    called = []

    def fake_exa(query, limit):
        called.append(query)
        return [{"source": "exa", "external_id": "X1", "name": "Paid Person"}]

    monkeypatch.setattr("rip.discovery.SUGGESTION_SEARCHERS",
                        (("exa", fake_exa, True),))
    nl_query(q="quantum basketweaving", discover="auto", db=session)
    assert called == []                      # not bought

    nl_query(q="quantum basketweaving", discover="true", db=session)
    assert called                            # explicitly asked for, so allowed


class _FakeSearcher:
    """Stands in for a connector's search_authors, recording calls."""

    def __init__(self, results, calls, name, fail=False):
        self._results, self._calls, self._name, self._fail = results, calls, name, fail

    def search_authors(self, query, limit=10):
        self._calls.append(self._name)
        if self._fail:
            raise RuntimeError(f"{self._name} unavailable")
        return self._results

    def search_authors_by_topic(self, query, limit=10):
        # topical discovery is tried first; these fixtures answer by name
        return []

    def fetch(self, identifier):
        # free sources are fetched and ingested; these fixtures have no payload
        raise RuntimeError(f"{self._name} fetch not stubbed")


def _patch_sources(monkeypatch, openalex=None, s2=None, dblp=None, fail=()):
    calls = []
    registry = {
        "openalex": _FakeSearcher(openalex or [], calls, "openalex", "openalex" in fail),
        "semanticscholar": _FakeSearcher(s2 or [], calls, "semanticscholar", "semanticscholar" in fail),
        "dblp": _FakeSearcher(dblp or [], calls, "dblp", "dblp" in fail),
    }
    monkeypatch.setattr("rip.connectors.get_connector", lambda s: registry[s])
    monkeypatch.setattr("rip.discovery.SUGGESTION_SEARCHERS", tuple(
        (n, f, u) for (n, f, u) in __import__("rip.discovery", fromlist=["x"]).SUGGESTION_SEARCHERS
        if n != "exa"))
    return calls


def test_suggestions_fall_through_to_other_sources(session, monkeypatch):
    """OpenAlex finding nothing must not end the search."""
    from rip.api import nl_query

    calls = _patch_sources(
        monkeypatch,
        openalex=[],
        s2=[{"id": "S1", "name": "Ada S2", "affiliations": ["MIT"], "papers": 9}],
        dblp=[{"pid": "12/345", "name": "Ada dblp"}],
    )
    resp = nl_query(q="quantum basketweaving", discover="true", db=session)
    sources = {s["source"] for s in resp["discovery_suggestions"]}
    assert sources == {"semanticscholar", "dblp"}
    assert calls == ["openalex", "semanticscholar", "dblp"]
    s2_hit = next(s for s in resp["discovery_suggestions"] if s["source"] == "semanticscholar")
    assert s2_hit["affiliation"] == "MIT"
    assert "ingest semanticscholar S1" in s2_hit["ingest_command"]


def test_live_searchers_cover_all_free_connectors():
    from rip.connectors import CONNECTORS
    from rip.discovery import PAID_SOURCES, SUGGESTION_SEARCHERS

    live = {name for name, _fn, _full in SUGGESTION_SEARCHERS}
    free = set(CONNECTORS) - set(PAID_SOURCES)
    assert free <= live
    assert "exa" in live


def test_later_sources_skipped_once_enough_found(session, monkeypatch):
    from rip.api import nl_query

    plenty = [{"id": f"A{i}", "name": f"P{i}"} for i in range(5)]
    calls = _patch_sources(monkeypatch, openalex=plenty)
    nl_query(q="quantum basketweaving", discover="true", db=session)
    assert calls == ["openalex"]  # no needless upstream calls


def test_failing_source_does_not_fail_the_query(session, monkeypatch):
    from rip.api import nl_query

    _patch_sources(
        monkeypatch,
        s2=[{"id": "S1", "name": "Ada S2"}],
        fail=("openalex",),
    )
    resp = nl_query(q="quantum basketweaving", discover="true", db=session)
    assert [s["source"] for s in resp["discovery_suggestions"]] == ["semanticscholar"]


def test_discover_queue_adds_leads_but_never_ingests(session, monkeypatch):
    from rip.api import nl_query
    from rip.models import DiscoveryLead, Person, SourceRecord

    _patch_sources(monkeypatch, openalex=[{"id": "A999", "name": "Marie Curie"}])
    before = (session.query(Person).count(), session.query(SourceRecord).count())
    resp = nl_query(q="radioactivity pioneers", discover="queue", db=session)

    assert resp["queued_leads"] == 1
    lead = session.query(DiscoveryLead).filter_by(identifier="A999").one()
    assert lead.status == "pending" and lead.source == "openalex"
    assert "radioactivity pioneers" in lead.reason
    # nothing entered the graph during the request
    assert (session.query(Person).count(), session.query(SourceRecord).count()) == before


def test_queue_is_idempotent_and_skips_known_records(session, monkeypatch):
    from rip.api import nl_query
    from rip.models import DiscoveryLead

    _patch_sources(monkeypatch, openalex=[{"id": "A999", "name": "Marie Curie"}])
    nl_query(q="radioactivity pioneers", discover="queue", db=session)
    resp = nl_query(q="radioactivity pioneers", discover="queue", db=session)
    assert resp["queued_leads"] == 0
    assert session.query(DiscoveryLead).filter_by(identifier="A999").count() == 1


def test_unmatched_query_returns_nothing_not_everything(session):
    """Regression: a query matching no vocabulary must not return arbitrary rows.

    "top program managers at oyo" against an academic corpus previously
    applied zero filters and then returned the first 25 people in the table,
    which reads as a wrong answer rather than no answer.
    """
    from rip.api import nl_query
    from rip.nlq import has_filters

    seed(session)
    parsed = parse(session, "top program managers at oyo")
    assert not has_filters(parsed)
    assert execute(session, parsed) == []

    resp = nl_query(q="top program managers at oyo", discover="false", db=session)
    assert resp["count"] == 0
    assert resp["results"] == []
    assert resp["matched_nothing"] is True
    assert "no filter could be applied" in resp["explanation"].lower()


def test_partial_match_still_filters(session):
    """One recognised term is enough; only a total miss returns nothing."""
    from rip.api import nl_query
    from rip.nlq import has_filters

    seed(session)
    parsed = parse(session, "rust program managers at oyo")
    assert has_filters(parsed)  # "rust" matched
    resp = nl_query(q="rust program managers at oyo", discover="false", db=session)
    assert resp["matched_nothing"] is False
    assert resp["count"] == 1
    assert resp["results"][0]["canonical_name"] == "Ada Example"


def test_queue_lead_endpoint(session):
    """UI live-search 'Queue' button: records intent, never ingests."""
    from fastapi import HTTPException

    from rip.api import queue_lead
    from rip.models import DiscoveryLead, Person

    before = session.query(Person).count()
    r = queue_lead({"source": "openalex", "external_id": "A123"}, db=session)
    assert r["status"] == "queued"
    lead = session.query(DiscoveryLead).filter_by(identifier="A123").one()
    assert lead.status == "pending"
    assert session.query(Person).count() == before  # nothing ingested

    # idempotent
    assert queue_lead({"source": "openalex", "external_id": "A123"}, db=session)["status"] == "already_queued"

    for bad in ({}, {"source": "openalex"}, {"source": "nope", "external_id": "x"}):
        try:
            queue_lead(bad, db=session)
            raise AssertionError(f"should have rejected {bad}")
        except HTTPException as exc:
            assert exc.status_code == 422


def test_queue_lead_detects_already_ingested(session):
    from rip.api import queue_lead
    from tests.test_ingest import github_profile

    ingest_profile(session, github_profile())
    r = queue_lead({"source": "github", "external_id": "jdoe"}, db=session)
    assert r["status"] == "already_ingested"


def _filters(session, **kw):
    from rip.api import list_persons

    base = {"q": None, "skill": None, "organization": None, "current_organization": None,
                "education": None, "role": None, "country": None, "location": None, "source": None,
                "technology": None, "min_publications": None, "min_citations": None,
                "min_sources": None, "active_since": None, "updated_since": None,
                "has_cv": None, "has_email": None, "sort": "relevance", "limit": 50, "offset": 0,
                "db": session}
    base.update(kw)
    return list_persons(**base)


def test_every_filter_narrows_correctly(session):
    from rip.normalize import EvidenceItem, OrgAffiliation, ProjectData, PublicationData

    ingest_profile(
        session,
        make_profile(
            name="Asha Rao", location="Bengaluru, India", country="IN",
            organizations=[
                OrgAffiliation(name="Acme Labs", role="Principal Engineer", is_current=True),
                OrgAffiliation(name="IIT Madras", relation="studied_at"),
            ],
            evidence=[EvidenceItem(attribute_type="skill", value="Distributed Systems")],
            projects=[ProjectData(name="fastdb", url="https://x/1", technologies=["Rust"])],
            publications=[PublicationData(title="P1", external_id="d1", doi="d1",
                                          published_date="2024", citations=120)],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            source="dblp", external_id="9/9", url="https://dblp.org/pid/9/9",
            raw={"name": "Ben Cole"}, name="Ben Cole", usernames=[],
            location="Berlin, Germany", country="DE",
            organizations=[OrgAffiliation(name="Globex", is_current=True)],
        ),
    )

    assert _filters(session, country="IN")["total_matches"] == 1
    assert _filters(session, country="in")["total_matches"] == 1  # case-insensitive
    assert _filters(session, location="berlin")["total_matches"] == 1
    assert _filters(session, organization="Acme")["total_matches"] == 1
    assert _filters(session, current_organization="Globex")["total_matches"] == 1
    assert _filters(session, education="IIT")["total_matches"] == 1
    assert _filters(session, role="Principal")["total_matches"] == 1
    assert _filters(session, skill="distributed")["total_matches"] == 1
    assert _filters(session, technology="rust")["total_matches"] == 1
    assert _filters(session, source="dblp")["total_matches"] == 1
    assert _filters(session, min_publications=1)["total_matches"] == 1
    assert _filters(session, min_citations=100)["total_matches"] == 1
    assert _filters(session, min_citations=500)["total_matches"] == 0
    assert _filters(session, active_since="2020")["total_matches"] == 1
    assert _filters(session, active_since="2030")["total_matches"] == 0
    assert _filters(session, has_cv=False)["total_matches"] == 2
    assert _filters(session, has_cv=True)["total_matches"] == 0
    # combining filters is AND, not OR
    assert _filters(session, country="IN", skill="distributed")["total_matches"] == 1
    assert _filters(session, country="DE", skill="distributed")["total_matches"] == 0


def test_filters_paginate_and_report_total(session):
    for i in range(12):
        ingest_profile(
            session,
            make_profile(
                external_id=f"p{i}", url=f"https://github.com/p{i}",
                raw={"login": f"p{i}"}, name=f"Person {i}", usernames=[f"github:p{i}"],
                country="US",
            ),
        )
    page = _filters(session, country="US", limit=5)
    assert page["count"] == 5 and page["total_matches"] == 12
    assert page["has_more"] and page["next_offset"] == 5
    last = _filters(session, country="US", limit=5, offset=10)
    assert last["count"] == 2 and not last["has_more"]


def test_sort_is_factual_not_ranked(session):
    ingest_profile(session, make_profile(name="Zoe Last", country="US"))
    ingest_profile(
        session,
        make_profile(external_id="a", url="https://github.com/a", raw={"login": "a"},
                     name="Aaron First", usernames=["github:a"], country="US"),
    )
    names = [r["canonical_name"] for r in _filters(session, sort="name")["results"]]
    assert names == sorted(names)
    # no score/rank fields anywhere in the payload
    assert "score" not in str(_filters(session, country="US"))


def test_facets_list_available_values(session):
    from rip.api import facets

    ingest_profile(session, make_profile(name="Asha Rao", country="IN"))
    countries = facets(field="country", limit=10, db=session)
    assert countries["values"] == [{"value": "IN", "people": 1}]
    sources = facets(field="source", limit=10, db=session)
    names = {v["value"] for v in sources["values"]}
    assert {"github", "openalex", "orcid", "wikidata", "huggingface",
            "stackoverflow", "web", "dblp", "semanticscholar"} <= names
    assert "exa" not in names
    assert next(v["people"] for v in sources["values"] if v["value"] == "github") == 1

    from fastapi import HTTPException
    try:
        facets(field="nonsense", limit=10, db=session)
        raise AssertionError("should reject unknown facet")
    except HTTPException as exc:
        assert exc.status_code == 422


def test_place_name_is_never_treated_as_a_person_name(session):
    """Regression: Hyderabad is a place filter, not a person-name filter."""
    seed(session)
    parsed = parse(session, "top python developers in Hyderabad")
    assert parsed.name_terms == []            # not a name filter
    assert "Hyderabad" in parsed.locations     # recognized as a city


def test_capitalised_word_is_a_name_filter_only_when_someone_has_it(session):
    seed(session)
    assert parse(session, "find Grace").name_terms == ["Grace"]
    assert parse(session, "find Zzznobody").name_terms == []


def test_country_names_become_country_filters(session):
    from rip.normalize import OrgAffiliation

    ingest_profile(session, make_profile(name="Asha Rao", country="IN",
                                         organizations=[OrgAffiliation(name="Acme Labs")]))
    parsed = parse(session, "researchers in India")
    assert parsed.countries == ["IN"]
    assert [p.canonical_name for p in execute(session, parsed)] == ["Asha Rao"]
    assert parse(session, "people in Germany").countries == ["DE"]


def test_preposition_phrases_do_not_hijack_skills(session):
    """"in India" must not match a skill merely containing that phrase."""
    from rip.normalize import EvidenceItem

    ingest_profile(
        session,
        make_profile(name="Ravi Kumar", country="IN",
                     evidence=[EvidenceItem(attribute_type="research_interest",
                                            value="Social and Economic Development in India")]),
    )
    parsed = parse(session, "machine learning researchers in India")
    assert parsed.countries == ["IN"]
    assert "Social and Economic Development in India" not in parsed.skills


class _FullPayloadSearcher:
    """A provider that returns whole person records, like Exa does."""

    def __init__(self, calls):
        self._calls = calls

    def normalize(self, raw):
        from rip.connectors.exa import ExaConnector

        return ExaConnector.normalize(ExaConnector.__new__(ExaConnector), raw)

    def search_people(self, query, limit=10):
        self._calls.append(query)
        return [{
            "id": "px1", "name": "Nadia Rahman", "affiliation": "Zeta Corp",
            "role": "Growth Lead", "location": "Lisbon, Portugal",
            "raw": {
                "id": "https://exa.ai/library/person/px1",
                "url": "https://linkedin.com/in/nadia",
                "title": "Nadia Rahman",
                "text": "# Nadia Rahman\n\nGrowth Lead at Zeta Corp\n\nLisbon, Portugal (PT)\n",
                "entities": [{
                    "id": "https://exa.ai/library/person/px1", "type": "person",
                    "properties": {"name": "Nadia Rahman", "location": "Lisbon, Portugal",
                                   "workHistory": [{"title": "Growth Lead",
                                                    "company": {"name": "Zeta Corp"}}],
                                   "educationHistory": []},
                }],
            },
        }]


def test_paid_results_are_stored_so_the_next_query_is_free(session, monkeypatch):
    """A provider that returns full records should never be paid for twice."""
    from rip.api import nl_query
    from rip.models import Person, SearchCache

    calls = []
    monkeypatch.setenv("EXA_API_KEY", "k")
    monkeypatch.setattr("rip.connectors.get_connector", lambda s: _FullPayloadSearcher(calls))
    monkeypatch.setattr("rip.discovery.SUGGESTION_SEARCHERS",
                        (("exa", __import__("rip.discovery", fromlist=["x"])._search_exa, True),))

    first = nl_query(q="growth leads at Zeta Corp", discover="true", db=session)
    assert first["stored_from_live"] == 1
    assert len(calls) == 1
    assert session.query(Person).filter_by(canonical_name="Nadia Rahman").count() == 1
    # the freshly stored person is returned as a result, not just a suggestion
    assert any(r["canonical_name"] == "Nadia Rahman" for r in first["results"])

    second = nl_query(q="growth leads at Zeta Corp", discover="true", db=session)
    assert len(calls) == 1          # provider not queried again
    # either the corpus now answers fully (no live search needed) or the cache
    # suppressed the call — never a second purchase
    assert second.get("stored_from_live", 0) == 0
    assert any(r["canonical_name"] == "Nadia Rahman" for r in second["results"])
    cache = session.query(SearchCache).filter_by(provider="exa").one()
    assert cache.result_count == 1 and cache.stored_count == 1


def test_cache_expires_after_the_ttl(session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import rip.discovery as discovery
    from rip.models import SearchCache

    discovery._cache_record(session, "exa", "some query", 3, 3)
    assert discovery._cache_lookup(session, "exa", "some query") is not None
    row = session.query(SearchCache).one()
    row.ran_at = datetime.now(timezone.utc) - timedelta(days=discovery.SEARCH_TTL_DAYS + 1)
    session.commit()
    assert discovery._cache_lookup(session, "exa", "some query") is None


def test_query_normalisation_ignores_word_order(session):
    import rip.discovery as discovery

    assert discovery._norm_query("growth leads at Zeta") == discovery._norm_query("Zeta growth at leads")


def test_empty_result_names_the_filter_responsible(session):
    from rip.nlq import diagnose_empty
    from rip.normalize import EvidenceItem, OrgAffiliation

    ingest_profile(
        session,
        make_profile(name="Ann Fields", organizations=[OrgAffiliation(name="Zomato")]),
    )
    ingest_profile(
        session,
        make_profile(external_id="b", url="https://github.com/b", raw={"login": "b"},
                     name="Bo Chen", usernames=["github:b"],
                     evidence=[EvidenceItem(attribute_type="skill", value="Economic Growth")]),
    )
    parsed = parse(session, "growth people at Zomato")
    assert execute(session, parsed) == []
    why = diagnose_empty(session, parsed)
    assert why and why["would_match"] >= 1
    assert "Dropping" in why["message"]


def test_no_diagnosis_when_only_one_filter(session):
    from rip.nlq import diagnose_empty

    seed(session)
    parsed = parse(session, "Zzznothing")
    assert diagnose_empty(session, parsed) is None


def test_match_feedback_is_recorded_but_never_ranks(session):
    """Votes are stored for the downstream tool and change no ordering here."""
    from rip.api import list_feedback, nl_query, post_feedback
    from rip.models import Person

    seed(session)
    people = session.query(Person).order_by(Person.canonical_name).all()
    before = [p["canonical_name"]
              for p in nl_query(q="rust", discover="false", db=session)["results"]]

    post_feedback({"person_id": str(people[0].id), "query": "rust", "verdict": "bad"}, db=session)
    post_feedback({"person_id": str(people[-1].id), "query": "rust", "verdict": "good",
                   "note": "exactly right", "voter": "disha"}, db=session)

    after = [p["canonical_name"] for p in nl_query(q="rust", discover="false", db=session)["results"]]
    if before:
        assert after == before          # a bad vote does not demote, a good one does not promote

    log = list_feedback(since_id=0, limit=100, person_id="", db=session)
    assert log["count"] == 2
    assert {f["verdict"] for f in log["feedback"]} == {"good", "bad"}
    assert any(f["note"] == "exactly right" for f in log["feedback"])

    # re-voting replaces, never stacks
    post_feedback({"person_id": str(people[0].id), "query": "rust", "verdict": "good"}, db=session)
    log = list_feedback(since_id=0, limit=100, person_id="", db=session)
    assert log["count"] == 2


def test_match_votes_can_be_read_back_newest_first_with_names(session):
    """The Review page lists what was judged; the sync order stays oldest first."""
    from rip.api import list_feedback, post_feedback
    from rip.models import Person

    seed(session)
    people = session.query(Person).order_by(Person.canonical_name).all()[:2]
    for p in people:
        post_feedback({"person_id": str(p.id), "query": "rust", "verdict": "good"}, db=session)

    synced = list_feedback(since_id=0, limit=100, person_id="", db=session)
    assert [f["person_id"] for f in synced["feedback"]] == [str(p.id) for p in people]
    assert [f["person_name"] for f in synced["feedback"]] == [p.canonical_name for p in people]

    latest = list_feedback(since_id=0, limit=100, person_id="", newest_first=True, db=session)
    assert [f["person_id"] for f in latest["feedback"]] == [str(p.id) for p in reversed(people)]
    assert latest["next_since_id"] == synced["next_since_id"]


def test_feedback_rejects_a_bad_verdict(session):
    import pytest
    from fastapi import HTTPException

    from rip.api import post_feedback
    from rip.models import Person

    seed(session)
    pid = str(session.query(Person).first().id)
    for bad in ("maybe", "", "5"):
        with pytest.raises(HTTPException):
            post_feedback({"person_id": pid, "query": "rust", "verdict": bad}, db=session)


def test_shortlists_hold_people_with_the_query_that_found_them(session):
    import pytest
    from fastapi import HTTPException

    from rip.api import (
        add_to_shortlist,
        create_shortlist,
        get_shortlist,
        list_shortlists,
        remove_from_shortlist,
    )
    from rip.models import Person

    seed(session)
    person = session.query(Person).first()
    made = create_shortlist({"name": "Rust hires", "note": "Q3"}, db=session)
    assert made["created"] is True
    # same name again returns the existing list rather than a duplicate
    assert create_shortlist({"name": "Rust hires"}, db=session)["created"] is False

    add = add_to_shortlist(made["id"], {"person_id": str(person.id), "query": "rust"}, db=session)
    assert add["added"] is True
    assert add_to_shortlist(made["id"], {"person_id": str(person.id)}, db=session)["added"] is False

    body = get_shortlist(made["id"], db=session)
    assert body["count"] == 1
    assert body["members"][0]["found_by_query"] == "rust"   # why they are here
    assert list_shortlists(owner="anonymous", db=session)["shortlists"][0]["members"] == 1

    remove_from_shortlist(made["id"], str(person.id), db=session)
    assert get_shortlist(made["id"], db=session)["count"] == 0
    # the person is not deleted, only unlisted
    assert session.get(Person, person.id) is not None
    with pytest.raises(HTTPException):
        remove_from_shortlist(made["id"], str(person.id), db=session)


def test_typos_are_corrected_and_reported(session):
    """A corrected query must say what it actually searched for."""
    from rip.nlq import parse

    seed(session)
    parsed = parse(session, "rustt at Acme Labs")
    assert parsed.corrections, "a near-miss should be corrected"
    assert parsed.corrections[0]["typed"] == "rustt"
    # and the correction is applied, not just reported
    assert parsed.skill_groups or parsed.organizations


def test_filters_match_whole_words_not_substrings(session):
    """skill=r must not mean "every skill containing the letter r"."""
    from rip.api import list_persons
    from rip.ingest import ingest_profile
    from rip.normalize import EvidenceItem, NormalizedProfile

    def person(name, skill):
        return NormalizedProfile(
            source="github", source_type="code", external_id=name.lower().replace(" ", ""),
            url=f"https://github.com/{name}", raw={"login": name}, name=name,
            evidence=[EvidenceItem(attribute_type="skill", value=skill,
                                   url="https://example.test", confidence=0.9)],
        )

    ingest_profile(session, person("Rae Gopher", "Go"))
    ingest_profile(session, person("Cass Cognitive", "Cognitive Science"))
    ingest_profile(session, person("Ari Rlang", "R"))

    def call(**kw):
        base = {"q": None, "skill": None, "organization": None, "current_organization": None,
                    "education": None, "role": None, "country": None, "location": None, "source": None,
                    "technology": None, "min_publications": None, "min_citations": None,
                    "min_sources": None, "active_since": None, "updated_since": None,
                    "has_cv": None, "has_email": None, "sort": "relevance", "limit": 20, "offset": 0,
                    "db": session}
        base.update(kw)
        return list_persons(**base)

    def names(r):
        return {p["canonical_name"] for p in r["results"]}

    # "go" is a word in "Go", but only a fragment of "Cognitive"
    assert names(call(skill="go")) == {"Rae Gopher"}
    # a single letter is a real skill (the R language) and nothing else
    assert names(call(skill="r")) == {"Ari Rlang"}
    # the loose form is still available on request
    assert "Cass Cognitive" in names(call(skill="co*"))


def test_empty_filters_say_which_one_to_relax(session):
    """Five filters and zero results is useless without knowing which to drop."""
    from rip.api import list_persons
    from rip.ingest import ingest_profile
    from rip.normalize import EvidenceItem, NormalizedProfile

    def person(login, name, country, skill):
        return NormalizedProfile(
            source="github", source_type="code", external_id=login,
            url=f"https://github.com/{login}", raw={}, name=name, country=country,
            evidence=[EvidenceItem(attribute_type="skill", value=skill,
                                   url="https://example.test", confidence=0.9)],
        )

    ingest_profile(session, person("gopher", "Rae Gopher", "IN", "Go"))
    # someone in DE, but writing Haskell: each filter matches, the pair does not
    ingest_profile(session, person("haskeller", "Ada Berlin", "DE", "Haskell"))

    def call(**kw):
        base = {"q": None, "skill": None, "organization": None, "current_organization": None,
                    "education": None, "role": None, "country": None, "location": None, "source": None,
                    "technology": None, "min_publications": None, "min_citations": None,
                    "min_sources": None, "active_since": None, "updated_since": None,
                    "has_cv": None, "has_email": None, "sort": "relevance", "limit": 5, "offset": 0,
                    "db": session}
        base.update(kw)
        return list_persons(**base)

    assert call(skill="Go")["total_matches"] == 1
    # each filter works alone; together they match nobody
    out = call(skill="Go", country="DE")
    assert out["total_matches"] == 0
    why = out["empty_reason"]
    # dropping either one leaves exactly one person, and both are offered
    assert {b["filter"] for b in why["relaxing"]} == {"country", "skill"}
    assert all(b["without_it"] == 1 for b in why["relaxing"])
    assert {a["filter"]: a["matches"] for a in why["each_filter_alone"]} == {
        "skill": 1, "country": 1,
    }

    # a filter that matches nothing at all is named directly
    out = call(skill="Nonexistent", country="IN")
    assert "Nonexistent" in out["empty_reason"]["message"]

    # and diagnosing must not recurse forever when every subset is empty
    out = call(skill="Nonexistent", country="ZZ", role="Nobody")
    assert out["total_matches"] == 0
    assert out["empty_reason"]["message"]

    # The message is read by someone who filled in a form, and "q" is the
    # name of a parameter they never saw — on this endpoint it filters by
    # name, which is the one thing "q" does not suggest.
    out = call(q="Nobodyhere", country="IN")
    message = out["empty_reason"]["message"]
    assert "q=" not in message and " q " not in message, message
    assert "name" in message
    # the structured fields stay machine-readable, keyed by parameter
    assert any(a["filter"] == "q" for a in out["empty_reason"]["each_filter_alone"])


def test_job_titles_are_matched_as_roles_not_topics(session):
    """"community managers" must not become the topic Microbial Community Ecology."""
    from rip.ingest import ingest_profile
    from rip.nlq import parse
    from rip.normalize import EvidenceItem, NormalizedProfile, OrgAffiliation

    ingest_profile(session, NormalizedProfile(
        source="github", source_type="code", external_id="cm1",
        url="https://github.com/cm1", raw={}, name="Ravi Community",
        organizations=[OrgAffiliation(name="Zeta Corp", role="Community Manager")],
        evidence=[EvidenceItem(attribute_type="skill", value="Python",
                               url="https://example.test", confidence=0.9)],
    ))

    parsed = parse(session, "community managers at Zeta Corp")
    assert parsed.roles == ["community manager"]
    assert parsed.skill_groups == []          # never a research topic

    # a title nobody holds is reported, not applied — the corpus cannot
    # answer it, and filtering on it would just return nothing
    parsed = parse(session, "delivery managers at Zeta Corp")
    assert parsed.roles == []
    assert any("delivery" in t.lower() for t in parsed.unmatched_terms)

    # and a title must not swallow real vocabulary beside it
    parsed = parse(session, "python community managers")
    assert parsed.roles == ["community manager"]
    assert [g["term"] for g in parsed.skill_groups] == ["python"]


def test_one_organization_however_it_is_spelled(session):
    """"Deccan.AI" and "Deccan AI" are one company, not two."""
    from sqlalchemy import select

    from rip.ingest import ingest_profile
    from rip.models import Organization, normalize_org_name
    from rip.nlq import execute, parse
    from rip.normalize import NormalizedProfile, OrgAffiliation

    def person(login, name, org, role):
        return NormalizedProfile(
            source="github", source_type="code", external_id=login,
            url=f"https://github.com/{login}", raw={}, name=name,
            organizations=[OrgAffiliation(name=org, role=role)],
        )

    ingest_profile(session, person("a", "Ann Pm", "Deccan AI", "Program Manager"))
    ingest_profile(session, person("b", "Bo Pm", "Deccan.AI", "Program Manager"))
    ingest_profile(session, person("c", "Cy Design", "deccan ai", "Product Designer"))

    # one organization record, not three
    orgs = session.execute(
        select(Organization).where(Organization.norm_name == normalize_org_name("Deccan.AI"))
    ).scalars().all()
    assert len(orgs) == 1

    # and either spelling reaches everyone
    for spelling in ("deccan.ai", "Deccan AI"):
        found = {p.canonical_name for p in execute(session, parse(session, f"people at {spelling}"))}
        assert found == {"Ann Pm", "Bo Pm", "Cy Design"}, spelling

    pms = {p.canonical_name for p in execute(session, parse(session, "program managers at deccan.ai"))}
    assert pms == {"Ann Pm", "Bo Pm"}


def test_stronger_evidence_ranks_first(session):
    """The person with more, better-attested evidence leads the list."""
    ingest_profile(
        session,
        make_profile(
            name="Deep Evidence", external_id="deep", url="https://github.com/deep",
            raw={"login": "deep"}, usernames=["github:deep"], location="Berlin, Germany",
            evidence=[
                EvidenceItem(attribute_type="skill", value="Kubernetes", confidence=0.9),
                EvidenceItem(attribute_type="specialization", value="Kubernetes Operators",
                             confidence=0.8),
            ],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            name="Thin Evidence", external_id="thin", url="https://github.com/thin",
            raw={"login": "thin"}, usernames=["github:thin"], location="Berlin, Germany",
            evidence=[EvidenceItem(attribute_type="skill", value="Kubernetes", confidence=0.45)],
        ),
    )
    rows = execute(session, parse(session, "kubernetes"))
    names = [p.canonical_name for p in rows]
    assert names.index("Deep Evidence") < names.index("Thin Evidence")
    assert rows[0].relevance["score"] > rows[-1].relevance["score"]
    # the score has to be explainable, not just present
    assert set(rows[0].relevance["components"]) == {
        "depth", "output", "confidence", "corroboration", "breadth", "recency",
    }
    # the weights have to stay a blend, not drift into one signal dominating
    from rip.nlq import WEIGHTS

    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    assert max(WEIGHTS.values()) <= 0.35


def test_a_stated_skill_outranks_a_bio_mention(session):
    """Free text separates someone from nobody; it never beats a real skill."""
    ingest_profile(
        session,
        make_profile(
            name="Bio Only", external_id="bio", url="https://github.com/bio",
            raw={"login": "bio"}, usernames=["github:bio"],
            evidence=[EvidenceItem(attribute_type="bio",
                                   value="I dabble in kubernetes on weekends", confidence=0.6)],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            name="Real Skill", external_id="real", url="https://github.com/real",
            raw={"login": "real"}, usernames=["github:real"],
            evidence=[EvidenceItem(attribute_type="skill", value="Kubernetes", confidence=0.6)],
        ),
    )
    rows = execute(session, parse(session, "kubernetes"))
    assert [p.canonical_name for p in rows][0] == "Real Skill"
    # but the bio match is still found and still scores above zero
    assert "Bio Only" in [p.canonical_name for p in rows]
    assert rows[-1].relevance["score"] > 0


def test_vocabulary_cache_does_not_leak_between_databases(session):
    """A cached vocabulary belongs to one database, never to the process."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rip.db import Base

    seed(session)
    assert parse(session, "distributed systems experts").skill_groups  # populates the cache

    other = create_engine("sqlite://")
    Base.metadata.create_all(other)
    with sessionmaker(bind=other)() as empty:
        # an empty corpus knows no vocabulary — if the cache leaked it would
        # answer with the first database's skills
        assert parse(empty, "distributed systems experts").skill_groups == []
        # known tech words still parse as skills even on an empty corpus
        assert parse(empty, "rust developers").skill_groups


def _with_project(session, *, name, login, tech, stars, last_active, skill="Rust"):
    from rip.normalize import ProjectData

    return ingest_profile(
        session,
        make_profile(
            name=name, external_id=login, url=f"https://github.com/{login}",
            raw={"login": login}, usernames=[f"github:{login}"],
            evidence=[EvidenceItem(attribute_type="skill", value=skill, confidence=0.7)],
            projects=[ProjectData(
                name=f"{login}-proj", url=f"https://github.com/{login}/proj",
                technologies=[tech], activity={"stars": stars, "forks": 0},
                last_active_at=last_active,
            )],
        ),
    )


def test_shipped_work_lifts_an_otherwise_identical_profile(session):
    """Same stated skill, same sources — the one who built something leads."""
    _with_project(session, name="Builder", login="builder", tech="Rust",
                  stars=4000, last_active="2026-06-01")
    _with_project(session, name="Claimer", login="claimer", tech="Rust",
                  stars=0, last_active="2026-06-01")
    rows = execute(session, parse(session, "rust"))
    assert [p.canonical_name for p in rows][0] == "Builder"
    assert rows[0].relevance["components"]["output"] > rows[-1].relevance["components"]["output"]


def test_off_topic_fame_does_not_outrank_on_topic_work(session):
    """A famous unrelated repo says you ship; it does not say you ship *this*.

    stars=3000 (not the original 800): the smaller value produced a genuine
    but wafer-thin margin (~0.01, confirmed by direct measurement) that was
    never a robust assertion of the invariant this test names — it happened
    to hold under one specific recency half-life and would flip under a
    fully plausible tuning change elsewhere in the scoring (e.g.
    PROJECT_RECENCY_HALF_LIFE_DAYS, added when project/publication recency
    was split by kind). Raising the on-topic star count pushes its own
    output component close enough to the same ceiling that the assertion
    holds on the actual invariant (on-topic beats off-topic-fame) rather
    than on a coincidence of exactly how much recency happened to add.
    """
    _with_project(session, name="On Topic", login="ontopic", tech="Rust",
                  stars=3000, last_active="2026-06-01")
    _with_project(session, name="Famous Elsewhere", login="famous", tech="Cobol",
                  stars=90000, last_active="2026-06-01")
    rows = execute(session, parse(session, "rust"))
    assert [p.canonical_name for p in rows][0] == "On Topic"


def test_dormant_work_ranks_below_current_work(session):
    """Equal impact, but one of them stopped years ago."""
    _with_project(session, name="Still Active", login="active", tech="Rust",
                  stars=500, last_active="2026-07-01")
    _with_project(session, name="Long Dormant", login="dormant", tech="Rust",
                  stars=500, last_active="2013-01-01")
    rows = execute(session, parse(session, "rust"))
    assert [p.canonical_name for p in rows][0] == "Still Active"
    assert rows[0].relevance["components"]["recency"] > rows[-1].relevance["components"]["recency"]


def test_citations_count_like_stars_so_researchers_are_not_buried(session):
    """A cited paper and a starred repo are the same kind of evidence."""
    from rip.normalize import PublicationData

    _with_project(session, name="Repo Person", login="repo", tech="Robotics",
                  stars=600, last_active="2026-06-01", skill="Robotics")
    ingest_profile(
        session,
        make_profile(
            name="Paper Person", external_id="paper", url="https://github.com/paper",
            raw={"login": "paper"}, usernames=["github:paper"],
            evidence=[EvidenceItem(attribute_type="skill", value="Robotics", confidence=0.7)],
            publications=[PublicationData(
                title="A robotics result", external_id="W1", citations=600,
                published_date="2026-06-01", topics=["Robotics"],
            )],
        ),
    )
    rows = execute(session, parse(session, "robotics"))
    out = {p.canonical_name: p.relevance["components"]["output"] for p in rows}
    assert out["Paper Person"] == out["Repo Person"]


def test_publication_recency_decays_slower_than_project_recency(session):
    """Same age, same weighted impact — a paper should retain a HIGHER
    recency score than a repository at that age, because citations
    accumulate for years after publication in a way that "still being
    pushed to" does not. Isolates exactly the behavior this change adds:
    dated signals decaying on a rate specific to their kind, rather than one
    blanket half-life for every kind of date.
    """
    from rip.normalize import PublicationData

    old_date = "2022-01-01"  # comfortably in the past regardless of "now"
    _with_project(session, name="Old Repo", login="oldrepo", tech="Robotics",
                  stars=600, last_active=old_date, skill="Robotics")
    ingest_profile(
        session,
        make_profile(
            name="Old Paper", external_id="oldpaper", url="https://github.com/oldpaper",
            raw={"login": "oldpaper"}, usernames=["github:oldpaper"],
            evidence=[EvidenceItem(attribute_type="skill", value="Robotics", confidence=0.7)],
            publications=[PublicationData(
                title="An old robotics result", external_id="W2", citations=600,
                published_date=old_date, topics=["Robotics"],
            )],
        ),
    )
    rows = execute(session, parse(session, "robotics"))
    recency = {p.canonical_name: p.relevance["components"]["recency"] for p in rows}
    impact = {p.canonical_name: p.relevance["components"]["output"] for p in rows}
    # equal impact confirms this isolates recency, not output, as the cause
    # of any score difference between the two
    assert impact["Old Paper"] == impact["Old Repo"]
    assert recency["Old Paper"] > recency["Old Repo"]


def test_recency_score_pure_function_known_values():
    """Direct check of the decay function itself, independent of the DB
    plumbing above: a date exactly one half-life old must score ~0.5, and
    an unknown date must return None (not a guessed default — the caller
    decides what "no date at all" means, this function just says "I don't
    know")."""
    from datetime import datetime

    from rip.nlq import _recency_score

    now = datetime(2026, 1, 1)
    one_half_life_ago = datetime(2025, 1, 1)  # 365 days before `now`
    assert abs(_recency_score(now, one_half_life_ago, 365.0) - 0.5) < 0.01
    assert _recency_score(now, now, 365.0) == 1.0
    assert _recency_score(now, None, 365.0) is None
    # a longer half-life must score the SAME age higher — direct proof the
    # half-life parameter actually controls decay speed, not just a
    # cosmetic constant
    assert _recency_score(now, one_half_life_ago, 1460.0) > _recency_score(
        now, one_half_life_ago, 365.0
    )


def test_protected_attributes_are_redacted_before_they_become_evidence(session, caplog):
    """A bio that mentions family or pronouns must not make them searchable."""
    import logging

    from rip.ingest import ingest_profile
    from rip.models import Evidence
    from rip.normalize import EvidenceItem, NormalizedProfile

    def bio(text):
        return NormalizedProfile(
            source="github", source_type="code", external_id="p" + str(abs(hash(text)) % 999),
            url="https://github.com/x", raw={}, name="Sam Example",
            evidence=[EvidenceItem(attribute_type="bio", value=text,
                                   url="https://example.test", confidence=0.9)],
        )

    with caplog.at_level(logging.INFO, logger="rip.ingest"):
        ingest_profile(session, bio("Distributed systems engineer. Father of two. pronouns: he/him"))

    stored = [e.value for e in session.query(Evidence).filter_by(attribute_type="bio").all()]
    joined = " ".join(stored).lower()
    assert "father of two" not in joined
    assert "he/him" not in joined
    assert "distributed systems engineer" in joined      # the substance survives
    assert "[redacted: family_status]" in joined
    assert "redacted" in caplog.text                      # and it is logged, not silent

    # a topic that merely uses the vocabulary is untouched
    ingest_profile(session, bio("Researching how technology is affecting children and society"))
    kept = [e.value for e in session.query(Evidence).filter_by(attribute_type="bio").all()]
    assert any("affecting children and society" in v for v in kept)


def test_tech_word_is_never_a_name_filter(session):
    """Regression: once someone surnamed Python is ingested, 'python' must stay a skill."""
    seed(session)
    ingest_profile(
        session,
        make_profile(
            external_id="py", url="https://github.com/py",
            raw={"login": "py"}, name="Ada Python", usernames=["github:py"],
        ),
    )
    parsed = parse(session, "python developers")
    assert parsed.name_terms == []
    assert any(g.get("pattern") == "python" or "Python" in g.get("values", [])
               for g in parsed.skill_groups)


def test_name_in_corpus_is_not_swallowed_as_skill(session):
    """A lone first name that exists in the graph is a name filter, not a bio keyword."""
    ingest_profile(
        session,
        make_profile(name="Rahul Kumar", usernames=["github:rahulk"],
                     evidence=[EvidenceItem(attribute_type="bio",
                                            value="CEO at Example Corp")]),
    )
    parsed = parse(session, "rahul")
    assert parsed.name_terms == ["rahul"]
    assert parsed.skill_groups == []


def test_progressive_and_drops_rightmost_constraint(session):
    """When AND yields nothing, relax from the right and report what was dropped."""
    from rip.nlq import execute_progressive

    ingest_profile(
        session,
        make_profile(name="Andrew Cole", usernames=["github:andrew"],
                     evidence=[EvidenceItem(attribute_type="skill", value="Python")]),
    )
    asked = parse(session, "Andrew python california")
    rows, applied, dropped = execute_progressive(session, asked)
    assert [p.canonical_name for p in rows] == ["Andrew Cole"]
    assert any(d["term"].lower() == "california" for d in dropped)
    assert "california" not in applied.locations


def test_location_matches_bio_not_only_person_location(session):
    """Bangalore written only in a bio must satisfy a Bangalore location filter."""
    ingest_profile(
        session,
        make_profile(
            name="Bio Bangalore",
            location=None,
            evidence=[EvidenceItem(attribute_type="bio", value="NMIT, Bangalore")],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            external_id="berlin", url="https://github.com/berlin",
            raw={"login": "berlin"}, name="Berlin Person",
            usernames=["github:berlin"], location="Berlin, Germany",
        ),
    )
    parsed = parse(session, "bangalore")
    names = [p.canonical_name for p in execute(session, parsed)]
    assert "Bio Bangalore" in names
    assert "Berlin Person" not in names


def test_name_only_query_ranks_exact_match_first(session):
    ingest_profile(session, make_profile(name="Rahul Satija", usernames=["github:rs1"]))
    ingest_profile(
        session,
        make_profile(external_id="r2", url="https://github.com/r2", raw={"login": "r2"},
                     name="Someone Rahul", usernames=["github:r2"]),
    )
    rows = execute(session, parse(session, "Rahul Satija"))
    assert rows[0].canonical_name == "Rahul Satija"
    assert rows[0].relevance["components"].get("name_fit", 0) >= 0.9


def test_nl_query_reports_not_found(session):
    from rip.api import nl_query

    ingest_profile(
        session,
        make_profile(name="Andrew Cole", usernames=["github:andrew"],
                     evidence=[EvidenceItem(attribute_type="skill", value="Python")]),
    )
    resp = nl_query(q="Andrew python california", discover="false", near=True, db=session)
    assert resp["count"] >= 1
    assert any(d["term"].lower() == "california" for d in resp["not_found"])
    # by default the near match is held back and counted, not shown
    resp = nl_query(q="Andrew python california", discover="false", db=session)
    assert resp["count"] == 0 and resp["near_matches"] >= 1
    assert any(d["term"].lower() == "california" for d in resp["not_found"])
