"""Phase 1: blocked candidate enumeration for entity resolution."""

from rip.ingest import ingest_profile
from rip.models import Person, PersonNameToken
from rip.normalize import NormalizedProfile, OrgAffiliation
from rip.resolution import _fuzzy_candidates, name_tokens, sync_name_tokens
from tests.test_resolution import make_profile


def test_name_tokens_extraction():
    tokens = name_tokens("Geoffrey E. Hinton")
    # plain words (single-letter initials excluded — too weak alone)
    assert {"geoffrey", "hinton"} <= tokens and "e" not in tokens
    # composite surname|initial keys, both orderings
    assert {"k:hinton|g", "k:geoffrey|h"} <= tokens
    assert name_tokens(None, "") == set()


def test_blocking_keys_survive_name_variants():
    """Variants of one name must share a key; different people must not."""
    from rip.resolution import blocking_keys

    full = blocking_keys("Geoffrey Hinton")
    for variant in ["G. Hinton", "Hinton, Geoffrey", "geoffrey hinton"]:
        assert blocking_keys(variant) & full, variant
    # a shared common surname alone is not enough to collide
    assert not (blocking_keys("Wei Zhang") & blocking_keys("Yan Zhang"))
    assert not (blocking_keys("John Smith") & blocking_keys("Alice Smith"))


def test_tokens_synced_on_ingest(session):
    person = ingest_profile(session, make_profile(name="Ada Lovelace"))
    tokens = {
        t.token for t in session.query(PersonNameToken).filter_by(person_id=person.id)
    }
    assert {"ada", "lovelace"} <= tokens


def test_candidate_set_is_blocked_not_full_scan(session):
    """1000 unrelated persons must not all be scored for one incoming profile.

    The crowd is built with Person + sync_name_tokens rather than 1000 full
    ingests. Blocking reads exactly two things -- a person row and its name
    tokens -- and the other 998 ingests were paying for source records,
    evidence, resolution and search indexing that this assertion never looks
    at. 26 seconds of a 90-second suite, for one test.

    The shortcut is pinned to the real thing instead of assumed equal to it:
    ONE of the thousand goes through ingest_profile, a twin of it is built
    the short way, and their token sets must match. If ingest ever stops
    writing name tokens -- the failure this test exists to catch -- the twin
    disagrees and this fails, rather than passing against a crowd the
    shortcut invented.
    """
    real = ingest_profile(
        session,
        make_profile(
            source="github",
            external_id="filler0",
            url="https://github.com/filler0",
            raw={"login": "filler0"},
            name="Filler Person0",
            usernames=["github:filler0"],
        ),
    )

    def tokens_of(person_id):
        return {t.token for t in
                session.query(PersonNameToken).filter_by(person_id=person_id)}

    twin = Person(id="twin", canonical_name="Filler Person0")
    session.add(twin)
    session.flush()
    sync_name_tokens(session, twin)
    assert tokens_of(real.id), "ingest wrote no name tokens at all"
    assert tokens_of(twin.id) == tokens_of(real.id),         "the shortcut no longer writes what ingest writes"

    for i in range(1, 999):
        filler = Person(id=f"filler{i}", canonical_name=f"Filler Person{i}")
        session.add(filler)
        session.flush()
        sync_name_tokens(session, filler)
    session.commit()
    assert session.query(Person).count() == 1000

    incoming = make_profile(
        source="dblp",
        external_id="x/1",
        url="https://dblp.org/pid/x/1",
        raw={"name": "Ada Lovelace"},
        name="Ada Lovelace",
        usernames=[],
        organizations=[OrgAffiliation(name="Analytical Engine Co")],
    )
    candidates, org_map = _fuzzy_candidates(session, incoming)
    assert len(candidates) < 50  # blocked, not 1000
    assert set(org_map) == {c.id for c in candidates}


def test_shared_org_still_merges_in_fuzzy_band(session):
    """Blocking must not change merge behavior for genuine matches."""
    p1 = ingest_profile(
        session,
        make_profile(
            name="Katherine Johnson",
            organizations=[OrgAffiliation(name="Acme Labs", is_current=True)],
        ),
    )
    p2 = ingest_profile(
        session,
        make_profile(
            source="dblp",
            external_id="k/1",
            url="https://dblp.org/pid/k/1",
            raw={"name": "Katherine Johnson"},
            name="Katherine Johnson",
            usernames=[],
            organizations=[OrgAffiliation(name="Acme Labs")],
        ),
    )
    assert p1.id == p2.id  # fuzzy:name+org still fires through the block


def test_org_is_not_a_blocking_key(session):
    """Sharing only an employer must not pull someone into the candidate set.

    A large organization has thousands of people; blocking on it would return
    the candidate cap on every lookup. Such a candidate could never merge
    anyway, because merging also requires a high name score.
    """
    ingest_profile(
        session,
        make_profile(
            name="Zzz Unrelated",
            organizations=[OrgAffiliation(name="Shared Institute")],
        ),
    )
    incoming = make_profile(
        source="dblp", external_id="q/9", url="https://dblp.org/pid/q/9",
        raw={"name": "Qqq Different"}, name="Qqq Different", usernames=[],
        organizations=[OrgAffiliation(name="Shared Institute")],
    )
    candidates, _ = _fuzzy_candidates(session, incoming)
    assert candidates == []


def test_name_token_block_keeps_real_matches(session):
    """Name variants that should merge must still be enumerated."""
    ingest_profile(
        session,
        make_profile(
            name="Geoffrey Hinton",
            organizations=[OrgAffiliation(name="University of Toronto")],
        ),
    )
    for variant in ["G. Hinton", "Hinton, Geoffrey", "Geoffrey E. Hinton"]:
        incoming = make_profile(
            source="dblp", external_id=f"v/{variant}", url="https://dblp.org/pid/v/1",
            raw={"name": variant}, name=variant, usernames=[],
            organizations=[OrgAffiliation(name="University of Toronto")],
        )
        candidates, _ = _fuzzy_candidates(session, incoming)
        assert [c.canonical_name for c in candidates] == ["Geoffrey Hinton"], variant


def test_tokens_resynced_when_aliases_grow(session):
    person = ingest_profile(session, make_profile(name="Jane Doe"))
    person.aliases = ["Jane Alexandra Doe"]
    sync_name_tokens(session, person)
    session.commit()
    tokens = {t.token for t in session.query(PersonNameToken).filter_by(person_id=person.id)}
    assert {"jane", "doe", "alexandra"} <= tokens


def test_profile_without_name_yields_no_candidates(session):
    empty = NormalizedProfile(
        source="github", source_type="code_hosting", external_id="anon",
        url="https://github.com/anon", raw={}, name=None,
    )
    assert _fuzzy_candidates(session, empty) == ([], {})
