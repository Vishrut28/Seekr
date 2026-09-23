"""build_results() caches per-person evidence/affiliation lookups across
BOTH calls it can make within a single /v1/query request (once for the
initial corpus match, again after live discovery re-runs the search) — a
person who appears in both calls should only be queried once.

The one real risk this caching introduces: live discovery's ingest_profile()
runs identity resolution, which can attach a freshly-discovered profile's
evidence to someone who ALREADY existed in the corpus (a merge, not a new
person) — meaning that person's cached attrs from the FIRST call could be
stale by the time the SECOND call runs. These tests prove both properties:
the cache actually excludes already-covered people from the second query
(via real bound-parameter inspection, not just output correctness), and the
eviction step correctly invalidates exactly the people discovery touched.
"""
from rip.ingest import ingest_profile
from rip.normalize import EvidenceItem, NormalizedProfile
from tests.test_resolution import make_profile


def _capture_attr_query_params(session, fn):
    """Run fn() while recording the bound person-id list of every
    build_results() attrs query specifically — identifiable as the only
    evidence query that SELECTs evidence.source as an output column
    (confirmed by direct inspection: every other evidence query in this
    codebase either aggregates with SUM/COUNT/MAX or is EXISTS-wrapped
    rather than a bare SELECT). Returns (result, [set_of_ids_per_call])."""
    from sqlalchemy import event

    calls = []
    engine = session.get_bind()

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if "evidence.person_id, evidence.attribute_type, evidence.value, evidence.source" in statement:
            # SQLite driver gives a flat positional tuple; the IN(...)
            # placeholders come first, attribute_type values after — but we
            # only need to know WHICH person ids were queried, and every
            # bound string that looks like a UUID here is a person id
            # (attribute_type values are always "skill"/"research_interest").
            ids = {p for p in parameters if p not in ("skill", "research_interest")}
            calls.append(ids)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return result, calls


class _MergingSearcher:
    """A discovery source whose result resolves, via a strong username key,
    to a person ALREADY in the corpus — not a new person. Mirrors how a
    live-search hit for someone already known under a different source can
    merge into their existing record instead of creating a duplicate."""

    def __init__(self, name: str, username_key: str, new_skill_value: str):
        self._name = name
        self._username_key = username_key
        self._new_skill_value = new_skill_value

    def normalize(self, raw):
        return NormalizedProfile(
            source="exa", source_type="professional", external_id=self._username_key,
            url=f"https://example.test/{self._username_key}", raw=raw,
            name=self._name, usernames=[f"github:{self._username_key}"],
            evidence=[EvidenceItem(attribute_type="skill", value=self._new_skill_value,
                                   confidence=0.8, url="https://example.test")],
        )

    def search_people(self, query, limit=10):
        return [{
            "id": self._username_key, "name": self._name,
            "affiliation": None, "role": None, "location": None,
            "raw": {"id": self._username_key},
        }]


def test_second_build_results_call_excludes_already_cached_people(session, monkeypatch):
    """A person present in the corpus BEFORE discovery runs — and who still
    matches after the corpus grows — must NOT be re-queried by the second
    build_results() call. Only the genuinely new person's id should appear
    in that second call's query parameters."""
    from rip.api import nl_query

    existing = ingest_profile(
        session,
        make_profile(
            name="Already Matches", usernames=["github:already"],
            evidence=[EvidenceItem(attribute_type="skill", value="Kubernetes", confidence=0.8)],
        ),
    )
    session.commit()

    monkeypatch.setattr(
        "rip.connectors.get_connector",
        lambda s: _MergingSearcher("Newly Found", "newperson", "Kubernetes"),
    )
    monkeypatch.setattr(
        "rip.discovery.SUGGESTION_SEARCHERS",
        (("exa", __import__("rip.discovery", fromlist=["x"])._search_exa, True),),
    )
    monkeypatch.setenv("EXA_API_KEY", "k")

    resp, calls = _capture_attr_query_params(
        session, lambda: nl_query(q="kubernetes", discover="true", db=session)
    )

    names = {r["canonical_name"] for r in resp["results"]}
    assert "Already Matches" in names
    assert "Newly Found" in names
    # two build_results() calls happened (stored_from_live triggers a
    # re-run); the FIRST call queries "Already Matches" (the only person
    # who exists yet), the SECOND call must NOT include their id again —
    # only "Newly Found"'s id, which was not covered by the first call.
    assert resp["stored_from_live"] >= 1
    assert len(calls) == 2
    assert str(existing.id) in calls[0]
    assert str(existing.id) not in calls[1]


def test_eviction_prevents_stale_attrs_after_a_merge(session, monkeypatch):
    """The critical correctness case: a person already summarized once (and
    so already cached) gets NEW evidence attached via a live-discovery
    merge onto their EXISTING record. The final response must reflect the
    NEW evidence, not the stale cached snapshot from before the merge."""
    from rip.api import nl_query

    ingest_profile(
        session,
        make_profile(
            name="Existing Person", usernames=["github:mergetarget"],
            evidence=[EvidenceItem(attribute_type="skill", value="Kubernetes", confidence=0.8)],
        ),
    )
    session.commit()

    monkeypatch.setattr(
        "rip.connectors.get_connector",
        lambda s: _MergingSearcher("Existing Person", "mergetarget", "Distributed Systems"),
    )
    monkeypatch.setattr(
        "rip.discovery.SUGGESTION_SEARCHERS",
        (("exa", __import__("rip.discovery", fromlist=["x"])._search_exa, True),),
    )
    monkeypatch.setenv("EXA_API_KEY", "k")

    resp = nl_query(q="kubernetes", discover="true", db=session)

    person_row = next(r for r in resp["results"] if r["canonical_name"] == "Existing Person")
    attr_values = {a["value"] for a in person_row["attributes"]}
    # the ORIGINAL evidence must still be present...
    assert "Kubernetes" in attr_values
    # ...and the NEWLY merged-in evidence must ALSO be present — this is
    # exactly what a stale cache (no eviction) would miss, since "Existing
    # Person" was already cached from the FIRST build_results() call,
    # before the merge added "Distributed Systems"
    assert "Distributed Systems" in attr_values
