"""Tests for features adopted from the finderr comparison scan:
reparse-from-raw, integer change cursor, near-miss review band + tombstone
merge, evidence-aggregated profiles."""

from rip.ingest import ingest_profile
from rip.models import Evidence, MergeCandidate, Person, SourceRecord
from rip.normalize import OrgAffiliation
from rip.review import list_suspicious, resolve_duplicate
from tests.test_ingest import github_profile
from tests.test_resolution import make_profile

from rip.connectors import get_connector


def test_reparse_from_stored_raw(session):
    ingest_profile(session, github_profile())
    record = session.query(SourceRecord).one()
    connector = get_connector("github")
    profile = connector.renormalize(record.external_id, record.raw)
    assert profile.name == "Jane Doe"
    before = session.query(Evidence).count()
    ingest_profile(session, profile)  # idempotent round-trip
    assert session.query(Evidence).count() == before
    assert session.query(Person).count() == 1


def test_dblp_renormalize_from_full_xml(session):
    from tests.test_dblp_hf import dblp_profile

    ingest_profile(session, dblp_profile())
    record = session.query(SourceRecord).one()
    assert "xml" in record.raw  # full XML retained
    reparsed = get_connector("dblp").renormalize(record.external_id, record.raw)
    assert reparsed.name == "Jane Doe"
    assert len(reparsed.publications) == 2


def test_near_miss_creates_merge_candidate(session):
    # same org, name similar but below the 92 merge bar -> candidate, no merge
    ingest_profile(
        session,
        make_profile(
            name="Katherine Johnson",
            organizations=[OrgAffiliation(name="Acme Labs")],
        ),
    )
    ingest_profile(
        session,
        make_profile(
            source="dblp",
            external_id="5/55",
            url="https://dblp.org/pid/5/55",
            raw={"name": "Kathryn Johnson"},
            name="Kathryn Johnson",
            usernames=[],
            organizations=[OrgAffiliation(name="Acme Labs")],
        ),
    )
    assert session.query(Person).count() == 2
    mc = session.query(MergeCandidate).one()
    assert mc.status == "pending"
    assert mc.signals["shared_org"] == "acme labs"
    assert 85 <= mc.score < 92
    assert len(list_suspicious(session)["possible_duplicates"]) == 1


def test_duplicate_merge_tombstones(session):
    ingest_profile(
        session,
        make_profile(name="Katherine Johnson", organizations=[OrgAffiliation(name="Acme Labs")]),
    )
    ingest_profile(
        session,
        make_profile(
            source="dblp", external_id="5/55", url="https://dblp.org/pid/5/55",
            raw={"name": "x"}, name="Kathryn Johnson", usernames=[],
            organizations=[OrgAffiliation(name="Acme Labs")],
        ),
    )
    mc = session.query(MergeCandidate).one()
    result = resolve_duplicate(session, mc.id, "merge")
    assert result["status"] == "merged"
    keep = session.get(Person, result["kept_person_id"])
    merged = (
        session.query(Person).filter(Person.merged_into.isnot(None)).one()
    )
    assert merged.merged_into == keep.id  # tombstone, not deleted
    assert "Kathryn Johnson" in keep.aliases
    # all identity links now on the keeper
    assert all(l.person_id == keep.id for l in keep.identities)
    # queue cleared
    assert list_suspicious(session)["possible_duplicates"] == []


def test_duplicate_reject(session):
    ingest_profile(
        session,
        make_profile(name="Katherine Johnson", organizations=[OrgAffiliation(name="Acme Labs")]),
    )
    ingest_profile(
        session,
        make_profile(
            source="dblp", external_id="5/55", url="https://dblp.org/pid/5/55",
            raw={"name": "x"}, name="Kathryn Johnson", usernames=[],
            organizations=[OrgAffiliation(name="Acme Labs")],
        ),
    )
    mc = session.query(MergeCandidate).one()
    assert resolve_duplicate(session, mc.id, "reject")["status"] == "rejected"
    assert session.query(Person).count() == 2
    assert list_suspicious(session)["possible_duplicates"] == []


def test_merge_dedupes_shared_publications(session):
    from rip.models import Authorship
    from rip.normalize import PublicationData
    from rip.review import merge_persons

    shared_pub = PublicationData(title="Shared Paper", external_id="doi:10.1/x", doi="10.1/x")
    p1 = ingest_profile(
        session,
        make_profile(name="Katherine Johnson", publications=[shared_pub],
                     organizations=[OrgAffiliation(name="Acme Labs", role=None)]),
    )
    p2 = ingest_profile(
        session,
        make_profile(
            source="dblp", external_id="5/55", url="https://dblp.org/pid/5/55",
            raw={"name": "x"}, name="Kathryn Johnson", usernames=[],
            publications=[PublicationData(title="Shared Paper", external_id="doi:10.1/x", doi="10.1/x")],
            organizations=[OrgAffiliation(name="Acme Labs", role=None)],
        ),
    )
    assert p1.id != p2.id
    keep = merge_persons(session, p1.id, p2.id)
    auths = session.query(Authorship).filter_by(person_id=keep.id).all()
    assert len(auths) == 1  # duplicate authorship dropped, not crashed


S2_AUTHOR = {
    "authorId": "999",
    "name": "Jane Doe",
    "affiliations": ["Acme AI"],
    "homepage": "https://janedoe.ai",
    "paperCount": 42,
    "citationCount": 1200,
    "hIndex": 18,
}
S2_PAPERS = [
    {
        "paperId": "abc",
        "title": "Efficient Serving of Large Models",
        "year": 2023,
        "venue": "MLSys",
        "citationCount": 87,
        "externalIds": {"DOI": "10.1234/abcd"},
        "authors": [{"authorId": "999", "name": "Jane Doe"}, {"authorId": "1", "name": "Bob Ray"}],
    }
]


def test_semanticscholar_normalize_and_merge(session):
    from rip.connectors.semanticscholar import SemanticScholarConnector
    from rip.models import Publication
    from tests.test_ingest import github_profile

    p1 = ingest_profile(session, github_profile())  # blog: janedoe.ai
    profile = SemanticScholarConnector.normalize(
        SemanticScholarConnector.__new__(SemanticScholarConnector), S2_AUTHOR, S2_PAPERS
    )
    p2 = ingest_profile(session, profile)
    assert p1.id == p2.id  # merged on shared homepage
    pub = session.query(Publication).filter_by(doi="10.1234/abcd").one()
    assert pub.citations == 87
    # renormalize round-trip
    from rip.models import SourceRecord
    rec = session.query(SourceRecord).filter_by(source="semanticscholar").one()
    again = SemanticScholarConnector.renormalize(
        SemanticScholarConnector.__new__(SemanticScholarConnector), rec.external_id, rec.raw
    )
    assert again.name == "Jane Doe"


def test_attribute_conflict_two_sided_provenance(session):
    from rip.models import AttributeConflict
    from tests.test_ingest import github_profile

    ingest_profile(session, github_profile())  # location: Berlin, from github
    moved = github_profile()
    moved.source = "orcid"
    moved.external_id = "0000-1"
    moved.url = "https://orcid.org/0000-1"
    moved.usernames = []
    moved.location = "Zurich, Switzerland"
    moved.raw = {"who": "orcid-version"}
    ingest_profile(session, moved)
    conflict = session.query(AttributeConflict).one()
    assert conflict.attribute_name == "location"
    assert conflict.value_a == "Berlin, Germany"
    assert conflict.source_a == "github"  # side A provenance is real, not "canonical"
    assert conflict.value_b == "Zurich, Switzerland"
    assert conflict.source_b == "orcid"
    assert conflict.status == "active"
    # idempotent: re-ingest does not duplicate the conflict
    ingest_profile(session, moved)
    assert session.query(AttributeConflict).count() == 1


def test_resolution_reasons_human_readable(session):
    from rip.models import IdentityLink

    ingest_profile(session, make_profile(orcid="0000-0001-2345-6789"))
    ingest_profile(
        session,
        make_profile(
            source="openalex", external_id="A1", url="https://openalex.org/A1",
            raw={"id": "A1"}, usernames=[], orcid="0000-0001-2345-6789",
        ),
    )
    link = session.query(IdentityLink).filter(IdentityLink.match_method != "new").one()
    assert link.signals["reason"] == "Exact orcid match: 0000-0001-2345-6789"


def test_person_queries_avoid_distinct_on_json(session):
    """Postgres cannot SELECT DISTINCT rows containing a JSON column.

    Regression: list_persons and nlq.execute must de-duplicate on the id, not
    on whole rows, or they raise UndefinedFunction on Postgres while passing
    silently on SQLite.
    """
    from rip.api import list_persons
    from rip.nlq import execute, parse

    ingest_profile(session, github_profile())

    # every filter combination must run without a row-level DISTINCT
    def call(**kw):
        base = dict(q=None, skill=None, organization=None, current_organization=None,
                    education=None, role=None, country=None, location=None, source=None,
                    technology=None, min_publications=None, min_citations=None,
                    min_sources=None, active_since=None, updated_since=None,
                    has_cv=None, has_email=None, sort="relevance", limit=5, offset=0,
                    db=session)
        base.update(kw)
        return list_persons(**base)

    assert call(q="jane")["count"] == 1
    assert call(skill="Rust")["count"] == 1
    # the fixture org is "AcmeAI", one word: "Acme" is a prefix, not a word,
    # so it needs the explicit loose form
    assert call(organization="AcmeAI")["count"] == 1
    assert call(organization="Acme*")["count"] == 1
    assert len(execute(session, parse(session, "rust"))) == 1

    # and the SQL they emit de-duplicates on the id column, never whole rows
    import inspect

    import rip.api as api_module
    from rip.api import Person as _P  # noqa: F401  (module-level import check)

    source = inspect.getsource(api_module.list_persons)
    assert "with_only_columns(Person.id).distinct()" in source
