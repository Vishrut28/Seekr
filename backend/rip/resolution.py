"""Entity resolution.

Strategy (in order):
1. Deterministic: any strong key (orcid, email, source username, profile URL)
   already attached to a person -> same person, high confidence.
2. Fuzzy: name similarity >= threshold AND a shared organization -> same
   person, medium confidence.
3. Otherwise: new person.

Candidate enumeration is *blocked*: only persons sharing an organization or a
name token with the incoming profile are scored, so ingest cost does not grow
with corpus size. `_fuzzy_candidates` is shared by the merge path and the
near-miss review path so the corpus is walked once per ingest.

Source records are never merged or destroyed; resolution only creates an
IdentityLink recording the decision, its method, and its confidence, so
every merge is auditable and reversible.
"""

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Affiliation,
    IdentityLink,
    Organization,
    Person,
    PersonKey,
    PersonNameToken,
    SourceRecord,
)
from .normalize import NormalizedProfile, strong_keys
from .textnorm import words

FUZZY_NAME_THRESHOLD = 92.0
# near-miss band: not confident enough to merge, confident enough to queue
# for human review as a possible duplicate
NEAR_MISS_NAME_ORG_THRESHOLD = 85.0  # name score with a shared org
NEAR_MISS_NAME_ONLY_THRESHOLD = 96.0  # near-exact name, no shared org
# safety valve: a token shared by more persons than this (e.g. a very common
# surname) is too weak to block on by itself
MAX_CANDIDATES = 500


def _norm_name(name: str) -> str:
    return " ".join(name.lower().replace(".", " ").split())


def name_tokens(*names: str | None) -> set[str]:
    """Blocking keys for one or more name spellings.

    Two kinds are emitted:
      "hinton"      — a plain word, for single-word or unusual spellings
      "k:hinton|g"  — surname + other-part initial, the selective key

    The composite key is what keeps blocking selective at scale: the plain
    token "zhang" matches tens of thousands of people, while "k:zhang|w"
    matches far fewer. Both orderings are generated ("Hinton, Geoffrey" and
    "Geoffrey Hinton" produce the same key set), and it survives initials —
    "G. Hinton" and "Geoffrey Hinton" both yield "k:hinton|g".
    """
    tokens: set[str] = set()
    for name in names:
        if not name:
            continue
        parts = [
            p.strip("'’,")
            for p in _norm_name(name).replace("-", " ").split()
            if p.strip("'’,")
        ]
        # spelling variants block together: "aggarwal" is also "agarwal"
        from .names import spelling_key

        parts = [spelling_key(p) if p.isalpha() else p for p in parts]
        words = [p for p in parts if len(p) >= 2]
        tokens.update(words)
        if len(parts) >= 2:
            # treat each part in turn as the surname
            for i, surname in enumerate(parts):
                if len(surname) < 2:
                    continue
                for j, other in enumerate(parts):
                    if i != j and other:
                        tokens.add(f"k:{surname}|{other[0]}")
    return tokens


def blocking_keys(name: str | None) -> set[str]:
    """The subset of keys used to look candidates up (composite preferred)."""
    all_keys = name_tokens(name)
    composite = {k for k in all_keys if k.startswith("k:")}
    return composite or all_keys


def sync_name_tokens(session: Session, person: Person) -> None:
    """Keep a person's blocking tokens in step with their names."""
    wanted = name_tokens(person.canonical_name, *(person.aliases or []))
    existing = {
        t.token: t
        for t in session.execute(
            select(PersonNameToken).where(PersonNameToken.person_id == person.id)
        ).scalars()
    }
    for token in wanted - existing.keys():
        session.add(PersonNameToken(person_id=person.id, token=token))
    for token in existing.keys() - wanted:
        session.delete(existing[token])


def _org_names(profile: NormalizedProfile) -> set[str]:
    return {o.name.lower() for o in profile.organizations if o.name}


def _candidate_org_map(session: Session, person_ids: list[str]) -> dict[str, set[str]]:
    """Batch-fetch organization names for a candidate set (no N+1)."""
    if not person_ids:
        return {}
    rows = session.execute(
        select(Affiliation.person_id, Organization.name)
        .join(Organization, Organization.id == Affiliation.organization_id)
        .where(Affiliation.person_id.in_(person_ids))
    ).all()
    out: dict[str, set[str]] = {pid: set() for pid in person_ids}
    for person_id, org_name in rows:
        out[person_id].add(org_name.lower())
    return out


def _fuzzy_candidates(
    session: Session, profile: NormalizedProfile
) -> tuple[list[Person], dict[str, set[str]]]:
    """Blocked candidate set + their org names.

    Blocks on shared name tokens only. Both downstream paths (merge and
    near-miss) require a high name similarity, and any name pair scoring that
    high shares at least one token — so a name-token block loses no recall.

    Organization is deliberately NOT a blocking key: a large employer has
    thousands of people, so org-blocking returns the candidate cap on every
    lookup and makes ingest cost grow with corpus size. Org is still required
    as a *merge condition*, just not as a lookup key.
    """
    if not profile.name:
        return [], {}

    keys = blocking_keys(profile.name)
    if not keys:
        return [], {}
    ids = set(
        session.execute(
            select(PersonNameToken.person_id)
            .where(PersonNameToken.token.in_(keys))
            .limit(MAX_CANDIDATES)
        ).scalars()
    )
    if not ids:
        return [], {}
    candidates = list(
        session.execute(
            select(Person).where(
                Person.id.in_(ids),
                Person.canonical_name.isnot(None),
                Person.merged_into.is_(None),
            )
        ).scalars()
    )
    return candidates, _candidate_org_map(session, [p.id for p in candidates])


def _same_source_person_ids(
    session: Session, source: str, external_id: str, candidate_ids: list[str]
) -> set[str]:
    """Which of these candidates already hold a DIFFERENT record from this source.

    A source that issued two distinct IDs has already decided they are two
    people; name similarity must never overrule that. Without this guard,
    every "Wei Zhang" at one university collapses into a single record once
    the corpus is large enough to contain several of them.

    Scoped to the candidate set so the query stays small at any corpus size.
    """
    if not candidate_ids:
        return set()
    rows = session.execute(
        select(IdentityLink.person_id)
        .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
        .where(
            IdentityLink.person_id.in_(candidate_ids),
            SourceRecord.source == source,
            SourceRecord.external_id != external_id,
        )
    ).scalars()
    return set(rows)


def _spelled(name: str) -> str:
    """A name with each word in its transliteration group's form: "Rahul
    Aggarwal" and "Rahul Agarwal" compare as the same name. Look-alike names
    (Katherine / Kathryn) and nicknames are not folded — too weak to merge on
    (see names.py)."""
    from .names import spelling_key

    return " ".join(spelling_key(w) for w in _norm_name(name).split())


def _score(profile_name: str, person: Person) -> float:
    target = _spelled(profile_name)
    scores = [fuzz.token_sort_ratio(target, _spelled(person.canonical_name))] \
        if person.canonical_name else []
    for alias in person.aliases or []:
        # An alias that identifies nobody, compared with a name that identifies
        # nobody, proves nothing: "Karan Singh" carries the alias "K Singh", and
        # every "K. Singh" in the graph scored 100 against it. Enough for a
        # merge at a shared institute; wrong for most of them.
        if alias and (_identifiable(alias) or _identifiable(profile_name)):
            scores.append(fuzz.token_sort_ratio(target, _spelled(alias)))
    return max(scores, default=0.0)


def resolve(
    session: Session, profile: NormalizedProfile, candidates=None, org_map=None
) -> tuple[Person | None, str, float, dict]:
    """Return (person_or_None, match_method, confidence, signals).

    None means: create a new person. Pass `candidates`/`org_map` from
    `_fuzzy_candidates` to share one blocked enumeration with the near-miss
    pass; they are computed on demand otherwise.
    """
    # 1. deterministic strong-key match
    for key_type, key_value in strong_keys(profile):
        hit = session.execute(
            select(PersonKey).where(
                PersonKey.key_type == key_type, PersonKey.key_value == key_value
            )
        ).scalar_one_or_none()
        if hit is not None:
            person = session.get(Person, hit.person_id)
            return person, f"strong:{key_type}", 0.97, {
                "key_type": key_type,
                "key_value": key_value,
                "reason": f"Exact {key_type} match: {key_value}",
            }

    # 2. fuzzy name + shared org (only across sources, only when unambiguous)
    if profile.name and profile.organizations:
        if candidates is None:
            candidates, org_map = _fuzzy_candidates(session, profile)
        profile_orgs = _org_names(profile)
        disambiguated = _same_source_person_ids(
            session, profile.source, profile.external_id, [c.id for c in candidates]
        )
        matches: list[tuple[Person, float, str]] = []
        for person in candidates:
            if person.id in disambiguated:
                continue  # the source itself says this is someone else
            score = _score(profile.name, person)
            if score < FUZZY_NAME_THRESHOLD:
                continue
            shared = profile_orgs & (org_map or {}).get(person.id, set())
            if not shared:
                continue
            matches.append((person, score, next(iter(shared))))
        # several equally plausible people (common name at a big institution):
        # merging any of them would be a guess, so merge none and let the
        # near-miss pass queue them for review
        if len(matches) == 1:
            person, score, shared_org = matches[0]
            return (
                person,
                "fuzzy:name+org",
                0.75,
                {
                    "name_score": score,
                    "shared_org": shared_org,
                    "reason": f"Name similarity {score:.0f}/100 and shared organization '{shared_org}'",
                },
            )
        if len(matches) > 1:
            return None, "new", 1.0, {
                "ambiguous_candidates": len(matches),
                "reason": (
                    f"{len(matches)} people share this name and organization — "
                    "too ambiguous to merge automatically"
                ),
            }

    # 3. new person
    return None, "new", 1.0, {}


def _identifiable(name: str | None) -> bool:
    """Does this name name someone, on its own?

    "Karan Singh" does; "K. Singh" and a bare "Rahul" do not. A name-only
    near miss on a name like those is a question nobody can answer, and the
    real queue filled with them: one Karan Singh against thirty separate
    "K. Singh" records that share no paper, co-author or organization. With a
    shared organization the pair is still queued — that is evidence, and the
    branch above handles it.
    """
    return sum(1 for word in words(name) if len(word) > 1) >= 2


def find_near_misses(
    session: Session, profile: NormalizedProfile, candidates=None, org_map=None
) -> list[tuple[Person, float, dict]]:
    """Persons that MIGHT be this profile but did not clear the merge bar.

    Called after a profile resolves to a brand-new person; results feed the
    merge-review queue instead of auto-merging.
    """
    if not profile.name:
        return []
    if candidates is None:
        candidates, org_map = _fuzzy_candidates(session, profile)
    profile_orgs = _org_names(profile)
    disambiguated = _same_source_person_ids(
        session, profile.source, profile.external_id, [c.id for c in candidates]
    )
    out: list[tuple[Person, float, dict]] = []
    for person in candidates:
        if person.id in disambiguated:
            continue  # same source already ruled this out as a different person
        score = _score(profile.name, person)
        shared = (
            profile_orgs & (org_map or {}).get(person.id, set()) if profile_orgs else set()
        )
        if shared and NEAR_MISS_NAME_ORG_THRESHOLD <= score < FUZZY_NAME_THRESHOLD:
            org = next(iter(shared))
            out.append((person, score, {
                "name_score": score, "shared_org": org,
                "reason": f"Name similarity {score:.0f}/100 with shared organization '{org}' (below auto-merge bar)",
            }))
        elif not shared and score >= NEAR_MISS_NAME_ONLY_THRESHOLD \
                and _identifiable(profile.name) and _identifiable(person.canonical_name):
            out.append((person, score, {
                "name_score": score, "shared_org": None,
                "reason": f"Near-identical name ({score:.0f}/100) but no shared organization",
            }))
    return out
