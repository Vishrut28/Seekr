"""Ingestion pipeline core.

Source -> Connector -> NormalizedProfile -> [this module]:
    upsert SourceRecord (change detection via content hash)
    -> entity resolution -> apply attributes as Evidence
    -> organizations / publications / projects / relationship edges
    -> ChangeLog entries for person-field changes
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .compliance import redact
from .models import (
    Affiliation,
    AttributeConflict,
    Authorship,
    ChangeLog,
    Contribution,
    Evidence,
    IdentityLink,
    IngestionRun,
    MergeCandidate,
    Organization,
    Person,
    PersonKey,
    Project,
    Publication,
    SourceRecord,
)
from .normalize import NormalizedProfile, strong_keys
from .resolution import _fuzzy_candidates, find_near_misses, resolve, sync_name_tokens


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


def _upsert_source_record(session: Session, profile: NormalizedProfile) -> tuple[SourceRecord, bool]:
    """Returns (record, content_changed)."""
    record = session.execute(
        select(SourceRecord).where(
            SourceRecord.source == profile.source,
            SourceRecord.external_id == profile.external_id,
        )
    ).scalar_one_or_none()
    content_hash = _hash(profile.raw)
    now = _utcnow()
    if record is None:
        record = SourceRecord(
            source=profile.source,
            source_type=profile.source_type,
            external_id=profile.external_id,
            url=profile.url,
            raw=profile.raw,
            content_hash=content_hash,
            extracted_at=now,
        )
        session.add(record)
        session.flush()
        return record, True
    changed = record.content_hash != content_hash
    record.last_observed = now
    if changed:
        record.raw = profile.raw
        record.content_hash = content_hash
        record.url = profile.url
        record.extracted_at = now
    return record, changed


def _set_person_field(
    session: Session, person: Person, field: str, value, record: SourceRecord
) -> None:
    """Fill blank fields; on conflict keep the existing value and log the disagreement."""
    if value in (None, "", []):
        return
    old = getattr(person, field)
    if old in (None, "", []):
        setattr(person, field, value)
        session.add(
            ChangeLog(
                person_id=person.id, field=field, old_value=None,
                new_value=str(value), source_record_id=record.id,
            )
        )
    elif old != value:
        # conflict: preserve both sides as evidence, never overwrite silently
        session.add(
            ChangeLog(
                person_id=person.id, field=f"conflict:{field}", old_value=str(old),
                new_value=str(value), source_record_id=record.id,
            )
        )
        _record_attribute_conflict(session, person, field, str(old), str(value), record)


def _record_attribute_conflict(
    session: Session, person: Person, field: str, old: str, new: str, record: SourceRecord
) -> None:
    """First-class conflict object carrying BOTH sides' provenance."""
    exists = session.execute(
        select(AttributeConflict).where(
            AttributeConflict.person_id == person.id,
            AttributeConflict.attribute_name == field,
            AttributeConflict.value_a == old,
            AttributeConflict.value_b == new,
        )
    ).scalar_one_or_none()
    if exists is not None:
        return
    # side A provenance: the change-log entry that originally set this value
    origin = session.execute(
        select(ChangeLog)
        .where(
            ChangeLog.person_id == person.id,
            ChangeLog.field == field,
            ChangeLog.new_value == old,
        )
        .order_by(ChangeLog.changed_at)
    ).scalars().first()
    origin_record = (
        session.get(SourceRecord, origin.source_record_id)
        if origin and origin.source_record_id
        else None
    )
    source_a = origin_record.source if origin_record else "unknown"
    session.add(
        AttributeConflict(
            person_id=person.id,
            attribute_name=field,
            value_a=old,
            source_a=origin_record.source if origin_record else None,
            source_record_a_id=origin_record.id if origin_record else None,
            value_b=new,
            source_b=record.source,
            source_record_b_id=record.id,
        )
    )
    # surface the conflict in the change feed so the ranking tool can react
    # without polling /conflicts
    session.add(
        ChangeLog(
            person_id=person.id,
            field=f"conflict_detected:{field}",
            old_value=None,
            new_value=f"{old} ({source_a}) vs {new} ({record.source})",
            source_record_id=record.id,
        )
    )


# Attributes a source writes as prose, where a protected attribute can turn up
# incidentally. Structured fields (skill, scholarly_metrics) are not screened:
# they are enumerations, not sentences.
logger = logging.getLogger("rip.ingest")

FREE_TEXT_ATTRS = frozenset({"bio", "summary", "role", "award", "location", "headline"})


# Keywords a person typed about themselves, where their slip stays on the
# record. Not "bio": a misspelling inside a sentence is not a subject heading.
SELF_TYPED_ATTRS = frozenset({"skill", "research_interest", "specialization"})

# How many of someone's papers must be about a subject before it is one of
# their subjects rather than a word that turned up in their work.
MIN_SUBJECT_TITLES = 2


def on_subject(profile, subject: str) -> bool:
    """Is SUBJECT something this person works on, or a word inside their work?

    A topical search answers with whoever's text contains the term, and for a
    clinical subject most of that text is symptoms. Asking Europe PMC for
    "headache" returned a thyroid oncologist and two Wuhan clinicians, whose
    COVID papers list headache among the things patients reported. Three of
    fourteen people ingested for the subject did not work on it.

    Two ways to pass, and they are the grader's, because the question is the
    same one it answers: the person SAYS it -- a stated skill, research
    interest or specialization naming the subject -- or enough of their
    published titles are about it. One title is not a research area (the
    pandemic put COVID-19 in front of crop geneticists and database
    researchers alike), which is why MIN_SUBJECT_TITLES is 2 here and
    MIN_TITLE_MENTIONS is 2 in evaluation/grader.py.

    Deliberately NOT the rule discovery uses. _matches_only_the_name is
    looser on purpose -- a live search should trust that the provider did the
    semantic work, and OpenAlex matching a Rust verification researcher whose
    topics say "formal methods" is a good answer. A bulk pass building
    coverage for a named subject is the case where that trust is misplaced,
    because the provider is matching a word and nobody is reading the result.
    """
    # Stems, not plain words: the corpus writes "Recommender Systems" and a
    # caller asks for "recommender system". Number deciding whether a
    # coverage pass finds anyone would be worse than no check at all. This is
    # the comparison _contained() in nlq.py makes, for the same reason.
    from .textnorm import contains_phrase, stems

    phrase = stems(subject or "")
    if not phrase:
        return False

    def names_it(text) -> bool:
        return bool(text) and contains_phrase(stems(str(text)), phrase)

    for item in profile.evidence or []:
        if item.attribute_type in SELF_TYPED_ATTRS and names_it(item.value):
            return True
    titles = sum(
        1 for pub in (profile.publications or [])
        if names_it(getattr(pub, "title", ""))
        or any(names_it(t) for t in (getattr(pub, "topics", None) or []))
    )
    return titles >= MIN_SUBJECT_TITLES


# Misspelled subject words, and what they were meant to be.
#
# A LIST, not a rule, and the corpus is why. Four ORCID profiles say
# "Reinforcment Learning". The tempting fix is a distance rule -- a word one
# edit from a far commoner word is a misspelling of it -- and it does not
# survive contact with the data: of the 75 pairs of words one edit apart in
# these keywords, 74 are not typos. Most are plurals (technique/techniques,
# disorder/disorders), some are spelling variants (modelling/modeling), and
# several are different words entirely -- generics/genetics,
# ischemia/ischemic, microscope/microscopy, and material/maternal, which
# missed being "corrected" by a single holder. Exactly one is a typo. Knowing
# that "reinforcment" is a misspelling and "techniques" is a plural takes a
# dictionary, not an edit distance, so the entries are named here and the
# rule stays out of it.
MISSPELLINGS = {"reinforcment": "reinforcement"}

_WORD = re.compile(r"[A-Za-z']+")


def correct_spelling(value: str) -> tuple[str, str | None]:
    """VALUE with its misspelled subject words repaired, and what was typed.

    One missing letter did two kinds of damage here. The four people who
    typed it were invisible to a search for reinforcement learning. And the
    misspelling WAS a vocabulary term, held by real people, so a query
    repeating the typo matched it exactly, the typo-correction pass never
    ran, and "reinforcment learning" answered with those four rather than the
    sixty who do it -- a corpus typo defeating the typo tolerance built to
    survive one.

    This runs at ingest because nowhere else holds. Correcting the stored
    rows works today and expires: _retract_evidence deletes any row its
    source record no longer asserts, so the next refresh of that ORCID
    profile drops the correction and puts the typo back. Here, every refresh
    re-applies it. Nothing is lost either way -- the source record still
    holds the payload as it arrived, and the row records what was typed.
    """
    def repair(match: re.Match[str]) -> str:
        word = match.group()
        fixed = MISSPELLINGS.get(word.casefold())
        if fixed is None:
            return word
        if word.isupper():
            return fixed.upper()
        return fixed.capitalize() if word[:1].isupper() else fixed

    corrected = _WORD.sub(repair, value)
    return (corrected, value) if corrected != value else (value, None)


class SplitRecordError(Exception):
    """Raised instead of re-merging people somebody separated by hand.

    A caller refreshing a whole source should catch this and move on: the
    record is deliberately frozen, not broken.
    """


def _add_evidence(
    session: Session,
    person: Person,
    record: SourceRecord,
    attribute_type: str,
    value: str,
    *,
    extracted_info: str | None = None,
    url: str | None = None,
    published_at: datetime | None = None,
    confidence: float = 0.5,
) -> tuple[str, str]:
    """Store one claim; returns the (attribute_type, value) actually stored."""
    # Free text arrives as whatever a source chose to write. Protected
    # attributes are redacted before the value becomes queryable evidence —
    # the raw payload still holds the original, so provenance is intact and
    # nothing is quietly rewritten out of existence.
    if attribute_type in FREE_TEXT_ATTRS:
        value, removed = redact(value)
        if removed:
            logger.info(
                "redacted %s from %s evidence for %s",
                ", ".join(removed), attribute_type, person.id,
            )
    if attribute_type in SELF_TYPED_ATTRS:
        value, mistyped = correct_spelling(value)
        if mistyped:
            said = f"spelled {mistyped!r} by the source"
            extracted_info = f"{extracted_info}; {said}" if extracted_info else said
            logger.info("read %r as %r for %s", mistyped, value, person.id)
    # Everything this person is already on record as claiming under this
    # attribute, matched WITHOUT regard to case. Sources disagree about it —
    # Europe PMC's MeSH terms arrive as both "Artificial Intelligence" and
    # "Artificial intelligence" — and matching exactly filed them as two
    # different interests, so a profile listed the same subject twice and
    # neither copy counted as corroborating the other. In a human-readable
    # label case is presentation, not meaning.
    peers = (
        session.execute(
            select(Evidence).where(
                Evidence.person_id == person.id,
                Evidence.attribute_type == attribute_type,
                func.lower(Evidence.value) == value.lower(),
            )
        )
        .scalars()
        .all()
    )
    # the spelling already on file wins, so a profile settles on one of them
    # rather than changing every time a source is refreshed
    if peers:
        value = peers[0].value
    existing = next((p for p in peers if p.source_record_id == record.id), None)
    if existing is not None:
        existing.observed_at = _utcnow()
        return attribute_type, value
    # same claim already made by a different source? mark both corroborated
    state = "unverified"
    if peers:
        state = "corroborated"
        for p in peers:
            if p.verification_state == "unverified":
                p.verification_state = "corroborated"
    session.add(
        Evidence(
            person_id=person.id,
            attribute_type=attribute_type,
            value=value,
            extracted_info=extracted_info,
            source=record.source,
            url=url or record.url,
            source_record_id=record.id,
            published_at=published_at,
            confidence=confidence,
            verification_state=state,
        )
    )
    return attribute_type, value


# Claims that stay true of the past when a source changes them: someone who
# moved from Berlin to Zurich did live in Berlin, and the change is a recorded
# conflict (see _set_person_field), not a retraction.
HISTORICAL_ATTRS = frozenset({"location", "role"})


def _retract_evidence(session: Session, person: Person, record: SourceRecord,
                      asserted: set[tuple[str, str]]) -> int:
    """Remove what this record used to claim and no longer does.

    Evidence used to be additive forever: a keyword a person deleted from
    ORCID, or one a parser learned to reject ("Rahul Kumar ceo" is a name, not
    an interest), stayed searchable after every refresh and reparse. The raw
    payload is the record's word; anything it no longer supports goes, and a
    claim left with a single source is no longer corroborated. History
    (HISTORICAL_ATTRS) is kept.
    """
    session.flush()
    stale = [
        e for e in session.execute(
            select(Evidence).where(Evidence.person_id == person.id,
                                   Evidence.source_record_id == record.id)
        ).scalars()
        if (e.attribute_type, e.value) not in asserted
        and e.attribute_type not in HISTORICAL_ATTRS
    ]
    for item in stale:
        session.delete(item)
    session.flush()
    for item in stale:
        peers = session.execute(
            select(Evidence).where(
                Evidence.person_id == person.id,
                Evidence.attribute_type == item.attribute_type,
                Evidence.value == item.value,
            )
        ).scalars().all()
        if len({p.source_record_id for p in peers}) < 2:
            for p in peers:
                if p.verification_state == "corroborated":
                    p.verification_state = "unverified"
    return len(stale)


def _get_or_create_org(session: Session, name: str, org_type: str | None, url: str | None) -> Organization:
    """One organization per company, however the source spelled it."""
    from .models import normalize_org_name

    norm = normalize_org_name(name)
    org = None
    if norm:
        org = session.execute(
            select(Organization).where(Organization.norm_name == norm)
        ).scalars().first()
    if org is None:
        org = session.execute(
            select(Organization).where(Organization.name == name)
        ).scalar_one_or_none()
    if org is None:
        org = Organization(name=name, norm_name=norm, org_type=org_type, website=url)
        session.add(org)
        session.flush()
    elif not org.norm_name:
        org.norm_name = norm
    return org


def ingest_profile(session: Session, profile: NormalizedProfile) -> Person:
    """Run one normalized profile through resolution + persistence. Idempotent.

    Raises personhood.NotAPerson, before anything is written, for a record
    that is not a person (a job post, a listicle, a community account).
    """
    from sqlalchemy import func as _func

    from .personhood import NotAPerson, assess

    # A page title is not a name: "Dhruv Dixit's Profile | YMGrad" is stored
    # as Dhruv Dixit. The raw payload keeps the original, so nothing is lost,
    # and the title is deliberately NOT kept as an alias — its words ("profile",
    # "ymgrad") would become searchable as if they were part of a name.
    verdict = assess(profile.name, profile.source)
    if not verdict.is_person:
        raise NotAPerson(f"{profile.source}:{profile.external_id} is not a person "
                         f"({verdict.reason}): {profile.name!r}")
    # A payload with no name, no claims and no papers describes nobody. Europe
    # PMC answers an ORCID search it holds nothing for with {"orcid": ...,
    # "articles": []}, and that became a living person with a null name --
    # assess() passes it because "no name given" means there is nothing to
    # JUDGE, not that somebody is there. One reached the corpus through a
    # topical ingest: no name, no evidence, no publications, and invisible to
    # every search because there was nothing to match.
    if not (profile.name or "").strip() and not profile.evidence and not profile.publications:
        raise NotAPerson(f"{profile.source}:{profile.external_id} has no name, no "
                         f"claims and no publications: there is nobody to record")
    if verdict.name and verdict.name != profile.name:
        logger.info("cleaned name %r -> %r", profile.name, verdict.name)
        profile.name = verdict.name
    for alias in verdict.aliases:
        if alias not in profile.aliases:
            profile.aliases.append(alias)

    changelog_watermark = (
        session.execute(select(_func.max(ChangeLog.id))).scalar() or 0
    )
    record, _changed = _upsert_source_record(session, profile)

    # A record somebody has pulled apart by hand describes more than one
    # person, and the source cannot express that — its author-level claims are
    # about the conflated whole. Re-ingesting would put those people back
    # together, so the record is refused instead. See rip.split.
    from .split import was_split

    if was_split(session, record.id):
        logger.info("skipping %s:%s — it was split by hand and would re-merge",
                    profile.source, profile.external_id)
        raise SplitRecordError(
            f"{profile.source}:{profile.external_id} was split into separate people; "
            f"re-ingesting it would merge them back")

    # resolution (reuse existing link if this record was seen before)
    link = session.execute(
        select(IdentityLink).where(IdentityLink.source_record_id == record.id)
    ).scalar_one_or_none()
    if link is not None:
        linked = session.get(Person, link.person_id)
        if linked is None:  # the foreign key rules this out; say so if it happens
            raise LookupError(f"identity link {link.id} points at missing person {link.person_id}")
        person = linked
    else:
        # one blocked candidate enumeration shared by both passes
        candidates, org_map = _fuzzy_candidates(session, profile)
        resolved, method, confidence, signals = resolve(session, profile, candidates, org_map)
        if resolved is None:
            near_misses = find_near_misses(session, profile, candidates, org_map)
            resolved = Person()  # fields filled below so creation lands in the change feed
            session.add(resolved)
            session.flush()
        person = resolved
        session.add(
            IdentityLink(
                person_id=person.id,
                source_record_id=record.id,
                match_method=method,
                match_confidence=confidence,
                signals=signals,
            )
        )
        if method == "new":
            for candidate, score, nm_signals in near_misses:
                exists = session.execute(
                    select(MergeCandidate).where(
                        MergeCandidate.person_id.in_([person.id, candidate.id]),
                        MergeCandidate.candidate_person_id.in_([person.id, candidate.id]),
                    )
                ).first()
                if exists is None:
                    session.add(
                        MergeCandidate(
                            person_id=candidate.id,
                            candidate_person_id=person.id,
                            score=score,
                            signals=nm_signals,
                        )
                    )

    # strong keys -> person_key (skip ones claimed by another person; log the collision)
    for key_type, key_value in strong_keys(profile):
        existing = session.execute(
            select(PersonKey).where(PersonKey.key_type == key_type, PersonKey.key_value == key_value)
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                PersonKey(
                    person_id=person.id, key_type=key_type,
                    key_value=key_value, source_record_id=record.id,
                )
            )
        elif existing.person_id != person.id:
            session.add(
                ChangeLog(
                    person_id=person.id, field=f"key_collision:{key_type}",
                    old_value=existing.person_id, new_value=key_value,
                    source_record_id=record.id,
                )
            )

    # person scalar fields (fill blanks, log conflicts)
    _set_person_field(session, person, "canonical_name", profile.name, record)
    _set_person_field(session, person, "location", profile.location, record)
    # Only what the source actually stated. A country inferred from the
    # location text is not a claim any source made, and storing it here would
    # invent provenance and manufacture conflicts between sources. The country
    # filter reads the location itself instead.
    _set_person_field(session, person, "country", profile.country, record)
    _set_person_field(session, person, "summary", profile.summary, record)

    # aliases and profile urls are additive
    aliases = set(person.aliases or [])
    for alias in [profile.name, *profile.aliases]:
        if alias and alias != person.canonical_name:
            aliases.add(alias)
    person.aliases = sorted(aliases)
    urls = set(person.profile_urls or [])
    urls.add(profile.url)
    urls.update(profile.websites)
    person.profile_urls = sorted(u for u in urls if u)

    # location is also an evidence-backed claim
    asserted: set[tuple[str, str]] = set()
    if profile.location:
        asserted.add(_add_evidence(session, person, record, "location", profile.location,
                                   confidence=0.7))

    for item in profile.evidence:
        asserted.add(_add_evidence(
            session, person, record, item.attribute_type, item.value,
            extracted_info=item.extracted_info, url=item.url,
            published_at=item.published_at, confidence=item.confidence,
        ))
    _retract_evidence(session, person, record, asserted)

    # organizations + affiliations
    for org_aff in profile.organizations:
        org = _get_or_create_org(session, org_aff.name, org_aff.org_type, org_aff.url)
        existing_aff = session.execute(
            select(Affiliation).where(
                Affiliation.person_id == person.id,
                Affiliation.organization_id == org.id,
                Affiliation.role == org_aff.role,
                Affiliation.relation == org_aff.relation,
            )
        ).scalar_one_or_none()
        if existing_aff is None:
            session.add(
                Affiliation(
                    person_id=person.id, organization_id=org.id, relation=org_aff.relation,
                    role=org_aff.role, start_date=org_aff.start_date, end_date=org_aff.end_date,
                    is_current=org_aff.is_current, source_record_id=record.id, url=org_aff.url,
                )
            )
        if org_aff.is_current:
            _set_person_field(session, person, "current_organization", org_aff.name, record)
            if org_aff.role:
                _set_person_field(session, person, "current_role", org_aff.role, record)

    # publications + authorship edges
    for pub_data in profile.publications:
        pub = None
        if pub_data.external_id:
            pub = session.execute(
                select(Publication).where(Publication.external_id == pub_data.external_id)
            ).scalar_one_or_none()
        if pub is None and pub_data.doi:
            pub = session.execute(
                select(Publication).where(Publication.doi == pub_data.doi)
            ).scalar_one_or_none()
        if pub is None:
            pub = Publication(
                external_id=pub_data.external_id, title=pub_data.title, venue=pub_data.venue,
                published_date=pub_data.published_date, url=pub_data.url, doi=pub_data.doi,
                citations=pub_data.citations, topics=pub_data.topics,
                raw_authors=pub_data.raw_authors, source_record_id=record.id,
            )
            session.add(pub)
            session.flush()
        elif pub_data.citations is not None:
            pub.citations = pub_data.citations
        existing_auth = session.execute(
            select(Authorship).where(
                Authorship.person_id == person.id, Authorship.publication_id == pub.id
            )
        ).scalar_one_or_none()
        if existing_auth is None:
            session.add(
                Authorship(
                    person_id=person.id, publication_id=pub.id,
                    author_position=pub_data.author_position,
                )
            )

    # projects + contribution edges
    for proj_data in profile.projects:
        proj = None
        if proj_data.url:
            proj = session.execute(
                select(Project).where(Project.url == proj_data.url)
            ).scalar_one_or_none()
        if proj is None:
            proj = Project(
                name=proj_data.name, description=proj_data.description, url=proj_data.url,
                technologies=proj_data.technologies, organization=proj_data.organization,
                activity=proj_data.activity, started_at=proj_data.started_at,
                last_active_at=proj_data.last_active_at, source_record_id=record.id,
            )
            session.add(proj)
            session.flush()
        else:
            proj.activity = proj_data.activity or proj.activity
            proj.last_active_at = proj_data.last_active_at or proj.last_active_at
        existing_contrib = session.execute(
            select(Contribution).where(
                Contribution.person_id == person.id, Contribution.project_id == proj.id
            )
        ).scalar_one_or_none()
        if existing_contrib is None:
            session.add(
                Contribution(person_id=person.id, project_id=proj.id, role=proj_data.role)
            )

    sync_name_tokens(session, person)
    person.updated_at = _utcnow()
    # outbox: queue webhook deliveries for the changes this ingest produced,
    # written in THIS transaction so a subscriber can never be told about a
    # change that gets rolled back
    session.flush()
    new_changes = session.execute(
        select(ChangeLog).where(ChangeLog.id > changelog_watermark)
    ).scalars().all()
    if new_changes:
        from .webhooks import enqueue_for_changes

        enqueue_for_changes(session, new_changes)
    session.commit()
    return person


def run_connector(
    session: Session, connector, identifier: str, enrich_chain: bool = False,
    keep=None,
) -> Person | None:
    """Fetch + ingest with failure tracking (IngestionRun).

    With `enrich_chain`, identity signals in the fetched profile (ORCID,
    linked profiles, homepage) are followed into other sources; enrichment
    failures never fail this ingest.

    KEEP is a predicate on the fetched profile, checked BEFORE anything is
    stored. Declining is not an error -- the fetch worked and the answer was
    no -- so the run is recorded as "skipped" and None comes back, the same
    thing a caller sees when there was nobody to ingest. Callers who want
    whatever the source returned pass nothing and get exactly the old
    behaviour.
    """
    run = IngestionRun(source=connector.source, identifier=identifier)
    session.add(run)
    session.commit()
    try:
        profile = connector.fetch(identifier)
        if keep is not None and not keep(profile):
            run.status = "skipped"
            return None
        person = ingest_profile(session, profile)
        run.status = "ok"
        if enrich_chain:
            from .enrich import enrich

            enriched = enrich(session, profile)
            for hop, error in enriched.failed:
                session.add(
                    IngestionRun(
                        source=hop.source, identifier=hop.identifier,
                        status="error", error=error, finished_at=_utcnow(),
                    )
                )
            session.commit()
        return person
    except Exception as exc:  # record failure, re-raise for the caller/CLI
        session.rollback()
        run.status = "error"
        run.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        run.finished_at = _utcnow()
        session.commit()
