"""SYNONYMS: bridging queries and vocabulary that mean the same thing but
share no words — "LLM inference" and "Model Serving Infrastructure" — where
plain containment (used everywhere else in parse()) can never connect them.

Every test here ingests a person with a controlled, known skill value and
checks the END-TO-END outcome (does execute() actually find them), not just
that parse() produced some internal structure — the thing that matters is
whether the person is found, and a synonym gap is invisible until you check
that specifically.
"""
from rip.ingest import ingest_profile
from rip.nlq import SYNONYMS, execute, parse
from rip.normalize import EvidenceItem
from tests.test_resolution import make_profile


def test_llm_inference_finds_model_serving(session):
    """The confirmed real gap this feature exists to close: zero string
    overlap between the query and the stored skill value."""
    ingest_profile(
        session,
        make_profile(
            name="Serving Specialist",
            evidence=[EvidenceItem(attribute_type="skill",
                                   value="Model Serving Infrastructure", confidence=0.8)],
        ),
    )
    parsed = parse(session, "LLM inference")
    assert parsed.skill_groups, "the synonym should have produced a skill group"
    assert parsed.unmatched_terms == []
    rows = execute(session, parsed)
    assert [p.canonical_name for p in rows] == ["Serving Specialist"]


def test_client_side_development_finds_frontend_engineering(session):
    ingest_profile(
        session,
        make_profile(
            name="Frontend Person",
            evidence=[EvidenceItem(attribute_type="skill",
                                   value="Frontend Engineering", confidence=0.8)],
        ),
    )
    parsed = parse(session, "client-side development")
    rows = execute(session, parsed)
    assert [p.canonical_name for p in rows] == ["Frontend Person"]


def test_synonym_only_fires_when_canonical_phrase_is_a_real_word_boundary(session):
    """"large-scale systems" -> "distributed systems" must match a skill
    reading "Distributed Systems Engineering" (word-boundary containment)."""
    ingest_profile(
        session,
        make_profile(
            name="Systems Person",
            evidence=[EvidenceItem(attribute_type="skill",
                                   value="Distributed Systems Engineering", confidence=0.8)],
        ),
    )
    parsed = parse(session, "large-scale systems")
    rows = execute(session, parsed)
    assert [p.canonical_name for p in rows] == ["Systems Person"]


def test_exact_vocabulary_hit_is_preferred_over_containment(session):
    """When the canonical phrase exists as an exact skill VALUE (not just
    inside a longer one), the synonym should resolve to that value —
    proving the two-step ACRONYMS-style fallback (exact hit, then
    containment) actually prefers the tighter match."""
    ingest_profile(
        session,
        make_profile(
            name="Exact Match Person",
            evidence=[EvidenceItem(attribute_type="skill",
                                   value="Model Serving", confidence=0.8)],
        ),
    )
    parsed = parse(session, "model deployment")  # -> "model serving" in SYNONYMS
    assert any(g.get("values") == ["Model Serving"] for g in parsed.skill_groups)
    rows = execute(session, parsed)
    assert [p.canonical_name for p in rows] == ["Exact Match Person"]


def test_synonym_does_not_fire_when_term_already_matches_directly(session):
    """A term that already has its OWN exact vocabulary hit must resolve to
    that hit, not get redirected through a synonym mapping — synonyms are a
    fallback for terms that would otherwise find nothing, not an override
    for terms that are already unambiguous. (None of the current SYNONYMS
    keys collide with a real vocabulary value, so this mainly guards against
    a future entry accidentally shadowing a real skill.)"""
    ingest_profile(
        session,
        make_profile(
            name="Devops Literal",
            evidence=[EvidenceItem(attribute_type="skill", value="Devops", confidence=0.8)],
        ),
    )
    parsed = parse(session, "devops")
    # "devops" is both a real vocabulary value here AND a SYNONYMS key
    # (-> "site reliability") — the exact vocabulary hit must win, since it
    # is checked before SYNONYMS in the parse loop.
    assert any(g.get("values") == ["Devops"] for g in parsed.skill_groups)


def test_unrelated_bio_text_is_not_falsely_matched(session):
    """A synonym must not turn into a blind wildcard: a bio that shares no
    real connection to the canonical phrase must not match."""
    ingest_profile(
        session,
        make_profile(
            name="Unrelated Person",
            evidence=[EvidenceItem(attribute_type="bio",
                                   value="Enjoys hiking and photography")],
        ),
    )
    parsed = parse(session, "LLM inference")
    rows = execute(session, parsed)
    assert [p.canonical_name for p in rows] == []


def test_synonyms_keys_are_lowercase_and_non_empty():
    """A basic data-integrity check on the dict itself: parse() lowercases
    every n-gram before checking membership, so an uppercase or malformed
    key here would silently never match anything."""
    for key, value in SYNONYMS.items():
        assert key == key.lower(), f"{key!r} must be lowercase"
        assert value == value.lower(), f"{value!r} must be lowercase"
        assert key.strip() and value.strip()
        assert key != value, f"{key!r} maps to itself"
