"""Hard cases: search must not 500, over-match, or drop a place that is in the bio.

Written 2026-08-21 alongside work that was stashed and never committed; recovered
2026-09-30 and run against the code as it is now.
"""

from rip.api import nl_query
from rip.ingest import ingest_profile
from rip.nlq import execute, execute_progressive, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_nlq import _filters, seed
from tests.test_resolution import make_profile


def _person(session, ext, name, **kwargs):
    ingest_profile(
        session,
        make_profile(
            external_id=ext,
            url=f"https://github.com/{ext}",
            raw={"login": ext},
            name=name,
            usernames=[f"github:{ext}"],
            **kwargs,
        ),
    )


def test_parse_survives_empty_and_none(session):
    for q in (None, "", "   ", "\n\t"):
        parsed = parse(session, q)
        assert parsed.skill_groups == []
        assert execute(session, parsed) == []
        rows, used, dropped = execute_progressive(session, parsed)
        assert rows == []
        assert dropped == []


def test_parse_caps_a_pasted_essay(session):
    blob = " ".join(f"word{i}" for i in range(400))
    parsed = parse(session, blob)
    assert len(parsed.raw) > 1000
    # tokens are capped; this must return, not hang
    execute_progressive(session, parsed)


def test_like_wildcard_does_not_match_everyone(session):
    seed(session)
    # a literal percent must not become "match any location"
    assert _filters(session, location="%")["total_matches"] == 0
    assert _filters(session, location="_erlin")["total_matches"] == 0
    assert _filters(session, location="berlin")["total_matches"] == 1


def test_punctuation_and_unicode_queries_do_not_500(session):
    seed(session)
    for q in (
        "Ada rust Toronto",
        "%%%",
        "____",
        "'''\"\"\\",
        "Ada <script>alert(1)</script> rust",
        "नमस्ते python bangalore",
        "Neha c++ bangalore",
        "a" * 5000,
    ):
        resp = nl_query(q=q, discover="false", db=session)
        assert "results" in resp
        assert isinstance(resp["results"], list)
        assert "not_found" in resp


def test_live_discovery_failure_still_returns_corpus_matches(session, monkeypatch):
    seed(session)

    def boom(*_a, **_k):
        raise RuntimeError("upstream down")

    # nl_query imports it from rip.discovery at call time
    monkeypatch.setattr("rip.discovery.discovery_suggestions", boom)
    resp = nl_query(q="rust at Acme Labs", discover="true", db=session)
    assert [r["canonical_name"] for r in resp["results"]] == ["Ada Example"]
    assert resp.get("discovery_error")


def test_location_in_bio_and_org_and_education(session):
    _person(
        session, "bio-only", "Neha Chaudhary",
        location=None,
        summary=None,
        evidence=[
            EvidenceItem(attribute_type="skill", value="C++"),
            EvidenceItem(attribute_type="bio", value="NMIT, Bangalore"),
        ],
    )
    _person(
        session, "org-place", "Mina Orgplace",
        location=None,
        organizations=[OrgAffiliation(name="Bangalore Institute", is_current=True)],
        evidence=[EvidenceItem(attribute_type="skill", value="Python")],
    )
    _person(
        session, "edu-place", "Esha Eduplace",
        location=None,
        evidence=[
            EvidenceItem(attribute_type="skill", value="Java"),
            EvidenceItem(attribute_type="education", value="College of Engineering, Bengaluru"),
        ],
    )
    assert [p.canonical_name for p in execute(session, parse(session, "Neha c++ bangalore"))] == [
        "Neha Chaudhary"
    ]
    assert [p.canonical_name for p in execute(session, parse(session, "Mina python bangalore"))] == [
        "Mina Orgplace"
    ]
    assert [p.canonical_name for p in execute(session, parse(session, "Esha java bangalore"))] == [
        "Esha Eduplace"
    ]


def test_progressive_and_does_not_or_across_people(session):
    _person(
        session, "only-name", "Zelda Onlyname",
        location="Berlin, Germany",
        evidence=[EvidenceItem(attribute_type="skill", value="Rust")],
    )
    _person(
        session, "only-skill", "Sam Pythonista",
        location="Pune, India",
        evidence=[EvidenceItem(attribute_type="skill", value="Python")],
    )
    rows, used, dropped = execute_progressive(session, parse(session, "Zelda python pune"))
    # first word is Zelda; python and pune drop rather than returning Sam
    assert [p.canonical_name for p in rows] == ["Zelda Onlyname"]
    assert {d["term"].lower() for d in dropped} == {"python", "pune"}


def test_source_links_are_real_urls(session):
    ingest_profile(
        session,
        make_profile(
            external_id="linked",
            url="https://github.com/linked",
            raw={"login": "linked"},
            name="Link Person",
            usernames=["github:linked"],
            evidence=[EvidenceItem(attribute_type="skill", value="Python")],
        ),
    )
    resp = nl_query(q="Link python", discover="false", db=session)
    row = resp["results"][0]
    # the web UI links these (BrandLinks); the stashed `source_links` field it
    # replaced is gone
    assert any((u or "").startswith("https://github.com/") for u in (row.get("profile_urls") or []))


# ---------------------------------------------------------------------------
# From the stashed test_nlq.py: live-search rules that moved to rip.discovery
# ---------------------------------------------------------------------------

def _named(name, ident, source="openalex", **kw):
    from rip.normalize import NormalizedProfile

    return NormalizedProfile(source=source, source_type="scholarly", external_id=ident,
                             url=f"https://example.org/{ident}", raw={"id": ident},
                             name=name, **kw)


def test_a_person_named_the_query_is_kept(monkeypatch):
    """'Rahul' must keep an author whose name is Rahul, not drop them as a
    name-only coincidence: for a name query the name IS the connection."""
    from rip import discovery

    class Fake:
        def fetch(self, ident):
            return _named("Rahul", ident)

    monkeypatch.setattr("rip.connectors.get_connector", lambda _s: Fake())
    [(item, profile, exc)] = discovery._fetch_profiles(
        [{"source": "openalex", "external_id": "A1", "_term": "Rahul"}])
    assert exc is None and profile.name == "Rahul"


def test_a_name_match_does_not_need_the_name_repeated_in_the_bio(monkeypatch):
    from rip import discovery

    class Fake:
        def fetch(self, ident):
            return _named("Rahul Sharma", ident, source="github", summary="Backend engineer")

    monkeypatch.setattr("rip.connectors.get_connector", lambda _s: Fake())
    [(_item, _profile, exc)] = discovery._fetch_profiles(
        [{"source": "github", "external_id": "rsharma99", "_term": "Rahul"}])
    assert exc is None


def _one_hit_source(monkeypatch, fetch):
    """A free source that always finds Marie Curie; FETCH decides what storing
    her does. Returns the list of searches made."""
    from rip import discovery

    searched = []

    def search(query, _limit):
        searched.append(query)
        return [{"source": "orcid", "external_id": "0000-0001", "name": "Marie Curie"}]

    class Fake:
        def fetch(self, ident):
            return fetch(ident)

    monkeypatch.setattr("rip.connectors.get_connector", lambda _s: Fake())
    monkeypatch.setattr(discovery, "SUGGESTION_SEARCHERS", (("orcid", search, False),))
    return searched


def test_a_hit_that_could_not_be_stored_does_not_lock_the_name_out(session, monkeypatch):
    """Found, but the fetch failed: nothing was kept, so nothing can be
    replayed, and caching it meant the same question got no live answer until
    the entry expired."""
    from rip import discovery

    def offline(_ident):
        raise RuntimeError("offline")

    searched = _one_hit_source(monkeypatch, offline)
    for _ in range(2):
        discovery.discovery_suggestions(session, parse(session, "Marie Curie"), allow_paid=False)
    assert len(searched) == 2


def test_a_hit_that_was_stored_is_answered_from_the_cache_next_time(session, monkeypatch):
    """The other half: what was kept is replayed rather than bought again."""
    from rip import discovery

    searched = _one_hit_source(
        monkeypatch, lambda ident: _named("Marie Curie", ident, source="orcid",
                                          usernames=[f"orcid:{ident}"]))
    first = discovery.discovery_suggestions(session, parse(session, "Marie Curie"),
                                            allow_paid=False)
    assert any(s.get("stored") for s in first)
    discovery.discovery_suggestions(session, parse(session, "Marie Curie"), allow_paid=False)
    assert len(searched) == 1
