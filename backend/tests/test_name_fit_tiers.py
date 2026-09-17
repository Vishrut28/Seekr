"""Precise, tier-by-tier tests for _name_fit_scores.

The only prior coverage (test_name_only_query_ranks_exact_match_first in
test_nlq.py) only asserted ">= 0.9" — enough to catch a gross regression,
not enough to pin which named tier actually fired or to test the two
independent floor boosts (alias, handle) at all. These tests exercise each
tier directly by name, plus the floor-interaction ordering traced through
by hand while extracting the magic numbers into NAME_FIT_* constants.
"""
from rip.ingest import ingest_profile
from rip.nlq import (
    NAME_FIT_ALIAS_FLOOR,
    NAME_FIT_ALL_WORDS,
    NAME_FIT_ANY_WORD,
    NAME_FIT_EXACT,
    NAME_FIT_HANDLE_FLOOR,
    NAME_FIT_PREFIX_SUFFIX,
    NLQuery,
    _name_fit_scores,
    parse,
)
from tests.test_resolution import make_profile


def test_exact_match_scores_full_tier(session):
    person = ingest_profile(session, make_profile(name="Geoffrey Hinton"))
    parsed = parse(session, "Geoffrey Hinton")
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] == NAME_FIT_EXACT


def test_prefix_suffix_tier(session):
    """The query is a prefix or suffix of the full name — "Geoffrey" alone
    against "Geoffrey Hinton", or "Hinton" alone against the same name."""
    person = ingest_profile(session, make_profile(name="Geoffrey Hinton"))
    parsed = parse(session, "Geoffrey")
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] == NAME_FIT_PREFIX_SUFFIX


def test_all_words_tier(session):
    """Both name words appear in the query, in EITHER order — a match, but
    not a clean prefix/suffix, so it lands one tier below."""
    person = ingest_profile(session, make_profile(name="Geoffrey Hinton"))
    parsed = parse(session, "Hinton Geoffrey")
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] == NAME_FIT_ALL_WORDS


def test_any_word_tier(session):
    """Only ONE of several query words matches this person's name — a
    weaker, partial signal."""
    person = ingest_profile(session, make_profile(name="Geoffrey Hinton"))
    parsed = NLQuery(raw="Geoffrey Someone", name_terms=["Geoffrey", "Someone"])
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] == NAME_FIT_ANY_WORD


def test_no_match_scores_zero(session):
    person = ingest_profile(session, make_profile(name="Geoffrey Hinton"))
    parsed = NLQuery(raw="Zzznobody", name_terms=["Zzznobody"])
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] == 0.0


def test_alias_floor_lifts_a_weak_base_tier(session):
    """A person whose NAME doesn't match well, but who is known by the
    queried name as an ALIAS, must be lifted to at least the alias floor —
    even though their base tier (from canonical_name alone) would score
    lower."""
    person = ingest_profile(
        session,
        make_profile(name="Totally Different Name", aliases=["Geoffrey Hinton"]),
    )
    parsed = NLQuery(raw="Geoffrey Hinton", name_terms=["Geoffrey Hinton"])
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] >= NAME_FIT_ALIAS_FLOOR


def test_alias_floor_does_not_lower_an_already_higher_score(session):
    """The floor is a MINIMUM, not an override — an exact canonical-name
    match must stay at NAME_FIT_EXACT even if an alias also happens to
    match (floors only ever raise a score that's currently below them)."""
    person = ingest_profile(
        session,
        make_profile(name="Geoffrey Hinton", aliases=["Geoffrey Hinton"]),
    )
    parsed = parse(session, "Geoffrey Hinton")
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] == NAME_FIT_EXACT


def test_handle_floor_lifts_a_weak_or_zero_base_tier(session):
    """A person whose canonical name shares nothing with the query, but
    whose account handle contains the queried term, gets lifted to at
    least the handle floor."""
    person = ingest_profile(
        session,
        make_profile(
            name="No Name Overlap At All", external_id="geoffreyh",
            url="https://github.com/geoffreyh", raw={"login": "geoffreyh"},
            usernames=["github:geoffreyh"],
        ),
    )
    parsed = NLQuery(raw="geoffreyh", name_terms=["geoffreyh"])
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] >= NAME_FIT_HANDLE_FLOOR


def test_alias_floor_is_checked_before_handle_floor(session):
    """Ordering matters: the alias check runs first and can push the score
    to NAME_FIT_ALIAS_FLOOR (0.8), which is ABOVE NAME_FIT_HANDLE_FLOOR
    (0.7) — so when both an alias and a handle would match, the handle
    check's own "if score < HANDLE_FLOOR" guard is already false by the
    time it runs, and the alias floor wins. This pins that exact ordering
    dependency rather than leaving it implicit in the code's line order."""
    person = ingest_profile(
        session,
        make_profile(
            name="No Overlap Whatsoever", external_id="geoffreyh2",
            url="https://github.com/geoffreyh2", raw={"login": "geoffreyh2"},
            usernames=["github:geoffreyh2"], aliases=["Geoffrey Hinton"],
        ),
    )
    parsed = NLQuery(raw="Geoffrey Hinton", name_terms=["Geoffrey Hinton"])
    scores = _name_fit_scores(session, parsed, [person.id])
    assert scores[person.id] == NAME_FIT_ALIAS_FLOOR
