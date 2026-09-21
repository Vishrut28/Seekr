"""SQLAlchemy models.

Design principles:
- Source records are immutable-ish raw captures; they are never destroyed
  by entity resolution or merging.
- Every derived attribute is backed by an Evidence row pointing at the
  source record it came from.
- Conflicting values coexist as separate Evidence rows; nothing is
  silently overwritten.
- Person.id is a stable UUID the downstream ranking tool can reference.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Person(Base):
    __tablename__ = "person"
    __table_args__ = (
        # (merged_into, id) rather than separate single-column indexes:
        # every /v1/persons and /v1/query paging query filters
        # merged_into.is_(None) AND orders by Person.id for deterministic
        # pagination. Without a composite index covering both, SQLite (and
        # Postgres) can satisfy the filter from an index but still has to
        # materialize and sort the full matching set before LIMIT can apply
        # — confirmed via EXPLAIN QUERY PLAN ("USE TEMP B-TREE") and a real
        # benchmark showing roughly 2x latency on a 10k-person corpus. This
        # index lets the same scan satisfy the filter AND deliver rows
        # already in id order, eliminating that sort step entirely.
        Index("ix_person_merged_into_id", "merged_into", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    canonical_name: Mapped[str | None] = mapped_column(String(255), index=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    location: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    current_role: Mapped[str | None] = mapped_column(String(255))
    current_organization: Mapped[str | None] = mapped_column(String(255))
    profile_urls: Mapped[list] = mapped_column(JSON, default=list)
    # tombstone: set when this person was merged into another; the ID stays
    # resolvable so downstream references never break. Indexed via the
    # composite (merged_into, id) index above — a separate single-column
    # index here would be redundant (merged_into is its leading column) and
    # cost extra on every write for no read benefit.
    merged_into: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    identities: Mapped[list["IdentityLink"]] = relationship(back_populates="person")
    keys: Mapped[list["PersonKey"]] = relationship(back_populates="person")
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="person")
    affiliations: Mapped[list["Affiliation"]] = relationship(back_populates="person")
    authorships: Mapped[list["Authorship"]] = relationship(back_populates="person")
    contributions: Mapped[list["Contribution"]] = relationship(back_populates="person")


class SourceRecord(Base):
    """One raw capture of one external profile/page. Never deleted on merge."""

    __tablename__ = "source_record"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)  # e.g. "github", "openalex"
    source_type: Mapped[str] = mapped_column(String(64))  # e.g. "code_hosting", "scholarly"
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1024))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    first_observed: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_observed: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)

    identity: Mapped["IdentityLink | None"] = relationship(back_populates="source_record")


class IdentityLink(Base):
    """Resolution decision: this source record belongs to this person."""

    __tablename__ = "identity_link"
    __table_args__ = (UniqueConstraint("source_record_id", name="uq_identity_source_record"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"))
    match_method: Mapped[str] = mapped_column(String(64))  # "new" | "strong:<key>" | "fuzzy:name+org"
    match_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    matched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    review_state: Mapped[str] = mapped_column(String(32), default="unreviewed")
    # "unreviewed" | "approved" | "split"

    person: Mapped[Person] = relationship(back_populates="identities")
    source_record: Mapped[SourceRecord] = relationship(back_populates="identity")


class PersonKey(Base):
    """Strong identifiers used for deterministic entity resolution."""

    __tablename__ = "person_key"
    __table_args__ = (UniqueConstraint("key_type", "key_value", name="uq_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    key_type: Mapped[str] = mapped_column(String(32))  # "orcid" | "email" | "url" | "username"
    key_value: Mapped[str] = mapped_column(String(512))
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))

    person: Mapped[Person] = relationship(back_populates="keys")


class Evidence(Base):
    """Evidence backing one attribute claim about one person."""

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "attribute_type", "value", "source_record_id", name="uq_evidence"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    attribute_type: Mapped[str] = mapped_column(String(64), index=True)
    # e.g. "skill", "role", "education", "location", "research_interest", "specialization"
    value: Mapped[str] = mapped_column(String(512), index=True)
    extracted_info: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(1024))
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    verification_state: Mapped[str] = mapped_column(String(32), default="unverified")
    # "unverified" | "corroborated" | "conflicted" | "verified"

    person: Mapped[Person] = relationship(back_populates="evidence")


def normalize_org_name(name: str) -> str:
    """The key two spellings of the same company share.

    "Deccan.AI" and "Deccan AI" were two organizations with two separate sets
    of people, so a search for one could not see the other. Punctuation and
    legal suffixes carry no identity; the words do.
    """
    import re as _re

    text = _re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    words = [w for w in text.split() if w not in _ORG_SUFFIXES]
    return " ".join(words or text.split())


_ORG_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "pvt", "private", "corp", "corporation",
    "gmbh", "co", "plc", "sa", "bv", "ag", "llp", "lp",
}


class Organization(Base):
    __tablename__ = "organization"
    __table_args__ = (UniqueConstraint("name", name="uq_org_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    # identity, as opposed to spelling: "Deccan.AI" and "Deccan AI" share it
    norm_name: Mapped[str | None] = mapped_column(String(255), index=True)
    org_type: Mapped[str | None] = mapped_column(String(64))  # "company" | "university" | ...
    website: Mapped[str | None] = mapped_column(String(1024))
    location: Mapped[str | None] = mapped_column(String(255))

    affiliations: Mapped[list["Affiliation"]] = relationship(back_populates="organization")


class Affiliation(Base):
    """Person -> worked_at / studied_at -> Organization."""

    __tablename__ = "affiliation"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "organization_id", "role", "relation", name="uq_affiliation"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    relation: Mapped[str] = mapped_column(String(32), default="worked_at")  # or "studied_at"
    role: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[str | None] = mapped_column(String(32))
    end_date: Mapped[str | None] = mapped_column(String(32))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    url: Mapped[str | None] = mapped_column(String(1024))

    person: Mapped[Person] = relationship(back_populates="affiliations")
    organization: Mapped[Organization] = relationship(back_populates="affiliations")


class Publication(Base):
    __tablename__ = "publication"
    __table_args__ = (UniqueConstraint("external_id", name="uq_pub_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(255))  # DOI or source id
    title: Mapped[str] = mapped_column(String(1024), index=True)
    venue: Mapped[str | None] = mapped_column(String(512))
    published_date: Mapped[str | None] = mapped_column(String(32))
    url: Mapped[str | None] = mapped_column(String(1024))
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    citations: Mapped[int | None] = mapped_column(Integer)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    raw_authors: Mapped[list] = mapped_column(JSON, default=list)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))

    authorships: Mapped[list["Authorship"]] = relationship(back_populates="publication")


class Authorship(Base):
    """Person -> authored -> Publication."""

    __tablename__ = "authorship"
    __table_args__ = (UniqueConstraint("person_id", "publication_id", name="uq_authorship"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("publication.id"), index=True)
    author_position: Mapped[int | None] = mapped_column(Integer)

    person: Mapped[Person] = relationship(back_populates="authorships")
    publication: Mapped[Publication] = relationship(back_populates="authorships")


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("url", name="uq_project_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1024))
    technologies: Mapped[list] = mapped_column(JSON, default=list)
    organization: Mapped[str | None] = mapped_column(String(255))
    activity: Mapped[dict] = mapped_column(JSON, default=dict)  # stars, forks, commit counts...
    started_at: Mapped[str | None] = mapped_column(String(32))
    last_active_at: Mapped[str | None] = mapped_column(String(32))
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))

    contributions: Mapped[list["Contribution"]] = relationship(back_populates="project")


class Contribution(Base):
    """Person -> contributed_to -> Project."""

    __tablename__ = "contribution"
    __table_args__ = (UniqueConstraint("person_id", "project_id", name="uq_contribution"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), index=True)
    role: Mapped[str | None] = mapped_column(String(64))  # "owner" | "contributor"

    person: Mapped[Person] = relationship(back_populates="contributions")
    project: Mapped[Project] = relationship(back_populates="contributions")


class ChangeLog(Base):
    """Field-level change history for incremental sync by the ranking tool."""

    __tablename__ = "change_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PersonNameToken(Base):
    """Blocking index for entity resolution: one row per (person, name token).

    Lets fuzzy matching enumerate a small candidate set by shared name token
    instead of scanning every person.
    """

    __tablename__ = "person_name_token"
    __table_args__ = (UniqueConstraint("person_id", "token", name="uq_person_name_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    token: Mapped[str] = mapped_column(String(128), index=True)


class AttributeConflict(Base):
    """Two sources disagreeing about one attribute, stored as a first-class
    object with BOTH sides' provenance — a durable item a human can review,
    not just a log line."""

    __tablename__ = "attribute_conflict"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "attribute_name", "value_a", "value_b", name="uq_attr_conflict"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    attribute_name: Mapped[str] = mapped_column(String(64), index=True)
    value_a: Mapped[str] = mapped_column(String(512))
    source_a: Mapped[str | None] = mapped_column(String(64))
    source_record_a_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    value_b: Mapped[str] = mapped_column(String(512))
    source_b: Mapped[str | None] = mapped_column(String(64))
    source_record_b_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    # "active" | "resolved" | "superseded"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MergeCandidate(Base):
    """Near-miss resolution: two persons that might be the same, queued for
    human review instead of auto-merged. Approving merges them; rejecting
    records the decision so the pair is not re-proposed."""

    __tablename__ = "merge_candidate"
    __table_args__ = (
        UniqueConstraint("person_id", "candidate_person_id", name="uq_merge_candidate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    candidate_person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # "pending" | "merged" | "rejected" | "deferred"
    # "deferred" is not a decision: it means the pair had no evidence either
    # way, so there is nothing for a human to weigh. It leaves the review queue
    # and returns to it by itself if either person later gains evidence.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiscoveryLead(Base):
    """A person spotted in existing data but not yet ingested (co-author,
    repo contributor, org member, ...). Drained by `rip.cli ingest-leads`."""

    __tablename__ = "discovery_lead"
    __table_args__ = (UniqueConstraint("source", "identifier", name="uq_lead"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    identifier: Mapped[str] = mapped_column(String(255))
    discovered_via_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_record.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # "pending" | "claimed" | "ingested" | "error" | "skipped"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Which worker took this lead, and when. Several workers drain one queue,
    # so a lead is claimed before it is fetched — without it two workers read
    # the same pending rows and ingested the same person twice. A claim older
    # than discover.CLAIM_TTL_SECONDS belonged to a worker that died, and goes
    # back to pending.
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)


class SearchCache(Base):
    """One live-search call we already paid for.

    Before querying a metered provider we check here: if the same query ran
    recently, the answer is already in the graph and there is no reason to buy
    it again. Rows are cheap; a repeated search is not.
    """

    __tablename__ = "search_cache"
    __table_args__ = (UniqueConstraint("provider", "query_norm", name="uq_search_cache"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    query_norm: Mapped[str] = mapped_column(String(512), index=True)
    query_raw: Mapped[str | None] = mapped_column(Text)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    stored_count: Mapped[int] = mapped_column(Integer, default=0)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)  # times reused
    # The people this search produced. Without them a cache hit skips the
    # provider AND loses its answers: the corpus filter cannot express what
    # the search was actually looking for, so the person it found would not
    # come back. Remembering the ids makes a repeat query free AND complete.
    person_ids: Mapped[list | None] = mapped_column(JSON, default=list)


class SourceThrottle(Base):
    """Rate-limit backoff state for one external source, shared across every
    worker process via the database rather than kept as an in-process
    Python global.

    A module-level variable (the original design for GitHub's throttle) is
    invisible to sibling worker processes under gunicorn/uvicorn multi-
    worker deployment — each process has its own memory, so if worker A
    gets rate-limited, workers B/C/D have no way to know and each
    independently keeps spending requests until THEY also get rate-limited,
    multiplying wasted round-trips by however many workers are running.
    SearchCache already solves the identical class of problem (remembering
    something across requests/processes) by living in the database instead
    of process memory; this does the same for "should we even try this
    source right now."
    """

    __tablename__ = "source_throttle"
    __table_args__ = (UniqueConstraint("source", name="uq_source_throttle"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    blocked_until: Mapped[datetime] = mapped_column(DateTime)


class Shortlist(Base):
    """A named collection of people someone is keeping track of."""

    __tablename__ = "shortlist"
    __table_args__ = (UniqueConstraint("name", "owner", name="uq_shortlist_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    owner: Mapped[str] = mapped_column(String(128), default="anonymous", index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ShortlistMember(Base):
    """One person on one shortlist, with why they were added.

    The query they were found by is kept: months later "why is this person on
    my list" is the question, and the answer is the search that surfaced them.
    """

    __tablename__ = "shortlist_member"
    __table_args__ = (
        UniqueConstraint("shortlist_id", "person_id", name="uq_shortlist_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shortlist_id: Mapped[int] = mapped_column(ForeignKey("shortlist.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    found_by_query: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MatchFeedback(Base):
    """A human judgement that a person did or did not fit a query.

    Deliberately inert here. Seekr never reads this back to order results —
    ordering by judged fitness IS ranking, and ranking lives in the separate
    downstream tool. This table exists so that tool has labelled training data
    with the query it was judged against.
    """

    __tablename__ = "match_feedback"
    __table_args__ = (
        UniqueConstraint("person_id", "query_norm", "voter", name="uq_match_feedback"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    query_raw: Mapped[str] = mapped_column(Text)
    query_norm: Mapped[str] = mapped_column(String(512), index=True)
    # "good" or "bad" — a two-way judgement, not a score to average
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    voter: Mapped[str] = mapped_column(String(128), default="anonymous")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class WebhookSubscription(Base):
    """A downstream system asking to be pushed change events."""

    __tablename__ = "webhook_subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1024))
    event_types: Mapped[list] = mapped_column(JSON, default=list)
    signing_secret: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookDelivery(Base):
    """One queued push. Written in the ingest transaction, sent later by
    `rip.cli deliver-webhooks` — an outbox, so a subscriber never hears about
    a change that was rolled back."""

    __tablename__ = "webhook_delivery"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("webhook_subscription.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # "pending" | "delivered" | "failed"
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)


class IngestionRun(Base):
    """Source health / failure tracking."""

    __tablename__ = "ingestion_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    identifier: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="running")  # running|ok|error|skipped
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class PersonSplit(Base):
    """A record a person has pulled apart by hand, and what came out of it.

    One source record can describe two different researchers — a single
    OpenAlex author id holding a surgeon's operations and a combinatorialist's
    theorems. Splitting them cannot be re-derived from the source, because the
    source is what got it wrong: its author-level topic list describes the
    conflated fiction, and re-fetching would assert it again.

    So the split is recorded here, and ingest refuses to re-ingest the record
    it came from. That freezes the record rather than letting a refresh
    silently undo somebody's work — the honest trade, given the source cannot
    express the distinction.
    """

    __tablename__ = "person_split"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_record.id"), index=True)
    # who the papers were taken from, and who they were given to
    from_person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    to_person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True)
    publication_ids: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text)
    split_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConflationReview(Base):
    """A person record someone has looked at and judged, after it was flagged
    as possibly being several people.

    The flag is recomputed from the papers every time, so it cannot remember
    anything; this is what stops a record a human has already cleared from
    coming back forever. "several_people" is recorded rather than acted on:
    rip.split does divide a record, but only along its groups, so this is for
    a conflation nothing can separate -- papers belonging to different people
    that share a topic or a co-author. It marks the finding and closes the
    record.
    """

    __tablename__ = "conflation_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), index=True, unique=True)
    verdict: Mapped[str] = mapped_column(String(32))     # "one_person" | "several_people"
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# Registers the search index tables and the session hooks that keep them
# current. Imported last: it refers to the models above.
from . import search_index as _search_index  # noqa: E402,F401
