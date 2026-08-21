"""Hard cases: search must not 500, over-match, or drop a place that is in the bio."""

from rip.api import nl_query
from rip.ingest import ingest_profile
from rip.nlq import execute, execute_progressive, parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_nlq import _filters, _person, seed
from tests.test_resolution import make_profile


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

    monkeypatch.setattr("rip.nlq.discovery_suggestions", boom)
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
    assert any((u or "").startswith("https://github.com/") for u in (row.get("profile_urls") or []))
    assert any(
        x.get("source") == "github" and (x.get("url") or "").startswith("https://")
        for x in (row.get("source_links") or [])
    )
