"""Ingestion pipeline core.

Source -> Connector -> NormalizedProfile -> [this module]:
    upsert SourceRecord (change detection via content hash)
    -> entity resolution -> apply attributes as Evidence
    -> organizations / publications / projects / relationship edges
    -> ChangeLog entries for person-field changes
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def _location_named_in_profile(profile: NormalizedProfile) -> str | None:
    """City a source wrote in the bio when it left the location field blank."""
    from .nlq import place_mentioned

    hint = place_mentioned(profile.summary)
    if hint:
        return hint
    for item in profile.evidence:
        if item.attribute_type in ("bio", "education"):
            hint = place_mentioned(item.value)
            if hint:
                return hint
    return None


def backfill_blank_locations(session: Session, limit: int = 2000) -> int:
    """Fill Person.location from bio/summary for records ingested before we did."""
    from sqlalchemy import or_

    from .nlq import place_mentioned

    people = session.execute(
        select(Person).where(
            Person.merged_into.is_(None),
            or_(Person.location.is_(None), Person.location == ""),
        ).limit(limit)
    ).scalars().all()
    filled = 0
    for person in people:
        hint = place_mentioned(person.summary)
        if not hint:
            for (val,) in session.execute(
                select(Evidence.value).where(
                    Evidence.person_id == person.id,
                    Evidence.attribute_type.in_(("bio", "education")),
                )
            ):
                hint = place_mentioned(val)
                if hint:
                    break
        if not hint:
            continue
        person.location = hint
        filled += 1
    if filled:
        session.commit()
    return filled


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
) -> None:
    existing = session.execute(
        select(Evidence).where(
            Evidence.person_id == person.id,
            Evidence.attribute_type == attribute_type,
            Evidence.value == value,
            Evidence.source_record_id == record.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.observed_at = _utcnow()
        return
    # same claim already made by a different source? mark both corroborated
    peers = (
        session.execute(
            select(Evidence).where(
                Evidence.person_id == person.id,
                Evidence.attribute_type == attribute_type,
                Evidence.value == value,
            )
        )
        .scalars()
        .all()
    )
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
    """Run one normalized profile through resolution + persistence. Idempotent."""
    from sqlalchemy import func as _func

    changelog_watermark = (
        session.execute(select(_func.max(ChangeLog.id))).scalar() or 0
    )
    record, _changed = _upsert_source_record(session, profile)

    # resolution (reuse existing link if this record was seen before)
    link = session.execute(
        select(IdentityLink).where(IdentityLink.source_record_id == record.id)
    ).scalar_one_or_none()
    if link is not None:
        person = session.get(Person, link.person_id)
    else:
        # one blocked candidate enumeration shared by both passes
        candidates, org_map = _fuzzy_candidates(session, profile)
        person, method, confidence, signals = resolve(session, profile, candidates, org_map)
        if person is None:
            near_misses = find_near_misses(session, profile, candidates, org_map)
            person = Person()  # fields filled below so creation lands in the change feed
            session.add(person)
            session.flush()
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
    stated_location = (profile.location or "").strip() or None
    location = stated_location or _location_named_in_profile(profile)
    _set_person_field(session, person, "location", location, record)
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
    urls.update(profile.linked_urls)
    person.profile_urls = sorted(u for u in urls if u)

    # location is also an evidence-backed claim
    if stated_location:
        _add_evidence(session, person, record, "location", stated_location, confidence=0.7)
    elif location:
        _add_evidence(
            session, person, record, "location", location,
            extracted_info="named in bio", confidence=0.5,
        )

    for item in profile.evidence:
        _add_evidence(
            session, person, record, item.attribute_type, item.value,
            extracted_info=item.extracted_info, url=item.url,
            published_at=item.published_at, confidence=item.confidence,
        )

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
    session: Session, connector, identifier: str, enrich_chain: bool = False
) -> Person | None:
    """Fetch + ingest with failure tracking (IngestionRun).

    With `enrich_chain`, identity signals in the fetched profile (ORCID,
    linked profiles, homepage) are followed into other sources; enrichment
    failures never fail this ingest.
    """
    run = IngestionRun(source=connector.source, identifier=identifier)
    session.add(run)
    session.commit()
    try:
        profile = connector.fetch(identifier)
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
