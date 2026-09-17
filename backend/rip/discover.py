"""Bulk discovery: mine existing source records for new people.

Two kinds of extractors:
- raw-only: read stored raw payloads, zero extra API calls
  (OpenAlex co-authors)
- live: bounded follow-up API calls
  (GitHub contributors of a person's top repos)

Discovered people become DiscoveryLead rows (a queue), drained separately by
`ingest-leads` so discovery volume never outruns rate limits.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import get_connector
from .ingest import run_connector
from .models import DiscoveryLead, SourceRecord


def _add_lead(
    session: Session, source: str, identifier: str, via_record: SourceRecord, reason: str
) -> bool:
    """Insert if new and not already ingested. Returns True if a lead was added."""
    exists = session.execute(
        select(DiscoveryLead.id).where(
            DiscoveryLead.source == source, DiscoveryLead.identifier == identifier
        )
    ).first()
    if exists:
        return False
    already_ingested = session.execute(
        select(SourceRecord.id).where(
            SourceRecord.source == source, SourceRecord.external_id == identifier
        )
    ).first()
    if already_ingested:
        return False
    session.add(
        DiscoveryLead(
            source=source,
            identifier=identifier,
            discovered_via_record_id=via_record.id,
            reason=reason[:1000],
        )
    )
    return True


def discover_openalex_coauthors(session: Session) -> int:
    """Raw-only: co-authors on stored OpenAlex works."""
    added = 0
    records = (
        session.execute(select(SourceRecord).where(SourceRecord.source == "openalex"))
        .scalars()
        .all()
    )
    for record in records:
        own_name = (record.raw.get("author") or {}).get("display_name")
        for work in record.raw.get("works") or []:
            title = work.get("display_name") or "untitled"
            for authorship in work.get("authorships") or []:
                author = authorship.get("author") or {}
                author_id = (author.get("id") or "").rsplit("/", 1)[-1]
                if not author_id or author_id == record.external_id:
                    continue
                reason = f"co-author of {own_name or record.external_id} on '{title[:120]}'"
                if _add_lead(session, "openalex", author_id, record, reason):
                    added += 1
    session.commit()
    return added


def discover_dblp_coauthors(session: Session) -> int:
    """Raw-only: co-author PIDs stored in dblp source records."""
    added = 0
    records = (
        session.execute(select(SourceRecord).where(SourceRecord.source == "dblp"))
        .scalars()
        .all()
    )
    for record in records:
        own_name = record.raw.get("name") or record.external_id
        for coauthor in record.raw.get("coauthors") or []:
            pid = coauthor.get("pid")
            if not pid:
                continue
            reason = f"dblp co-author of {own_name} ({coauthor.get('name')})"
            if _add_lead(session, "dblp", pid, record, reason):
                added += 1
    session.commit()
    return added


def discover_github_contributors(
    session: Session, max_repos_per_person: int = 3, max_contributors_per_repo: int = 10
) -> int:
    """Live: contributors of each known person's top-starred repos. Bounded."""
    connector = get_connector("github")
    added = 0
    records = (
        session.execute(select(SourceRecord).where(SourceRecord.source == "github"))
        .scalars()
        .all()
    )
    for record in records:
        repos = [r for r in (record.raw.get("repos") or []) if not r.get("fork")]
        repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
        for repo in repos[:max_repos_per_person]:
            full_name = repo.get("full_name")
            if not full_name:
                continue
            try:
                contributors = connector.get_json(
                    f"https://api.github.com/repos/{full_name}/contributors",
                    params={"per_page": max_contributors_per_repo},
                )
            except Exception:
                continue  # private/blocked/rate issues: skip repo, keep going
            for contributor in contributors:
                login = contributor.get("login")
                if not login or login == record.external_id or contributor.get("type") == "Bot":
                    continue
                reason = (
                    f"contributor ({contributor.get('contributions', '?')} commits) "
                    f"to {full_name}"
                )
                if _add_lead(session, "github", login, record, reason):
                    added += 1
    session.commit()
    return added


# A claim older than this belonged to a worker that died mid-batch; its leads
# go back to the queue. Long enough that a slow batch (25 leads, each an
# enrichment chain across several rate-limited sources) is never mistaken for
# a dead one.
CLAIM_TTL_SECONDS = 1800
# How often one lead is retried when its write collides with another worker's.
WRITE_RETRIES = 3


def worker_id() -> str:
    """A name for this process that no other worker shares."""
    import os
    import socket
    import uuid

    return f"{socket.gethostname()[:24]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def claim_leads(
    session: Session, worker: str, limit: int, source: str | None = None
) -> list[DiscoveryLead]:
    """Atomically take up to LIMIT pending leads for WORKER.

    One conditional UPDATE does the taking, so two workers can never hold the
    same lead: on SQLite the statement runs under the database's single write
    lock; on Postgres the candidate rows are read FOR UPDATE SKIP LOCKED, so a
    second worker skips rows the first is taking instead of waiting for them
    and then finding them gone.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.execute(
        update(DiscoveryLead)
        .where(DiscoveryLead.status == "claimed",
               DiscoveryLead.claimed_at < now - timedelta(seconds=CLAIM_TTL_SECONDS))
        .values(status="pending", claimed_by=None, claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    pick = (
        select(DiscoveryLead.id)
        .where(DiscoveryLead.status == "pending")
        .order_by(DiscoveryLead.created_at, DiscoveryLead.id)
        .limit(limit)
    )
    if source:
        pick = pick.where(DiscoveryLead.source == source)
    if session.get_bind().dialect.name == "postgresql":
        pick = pick.with_for_update(skip_locked=True)
    session.execute(
        update(DiscoveryLead)
        .where(DiscoveryLead.id.in_(pick.scalar_subquery()),
               DiscoveryLead.status == "pending")
        .values(status="claimed", claimed_by=worker, claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    # populate_existing: the UPDATEs above bypassed the session, so lead objects
    # it already holds (a worker reuses its session across batches) would keep
    # showing whoever claimed them before
    return session.execute(
        select(DiscoveryLead)
        .where(DiscoveryLead.claimed_by == worker, DiscoveryLead.status == "claimed")
        .order_by(DiscoveryLead.created_at, DiscoveryLead.id)
        .execution_options(populate_existing=True)
    ).scalars().all()


def release_leads(session: Session, worker: str) -> int:
    """Give back every lead WORKER claimed and did not finish."""
    from sqlalchemy import update

    session.rollback()
    released = session.execute(
        update(DiscoveryLead)
        .where(DiscoveryLead.claimed_by == worker, DiscoveryLead.status == "claimed")
        .values(status="pending", claimed_by=None, claimed_at=None)
        .execution_options(synchronize_session=False)
    ).rowcount
    session.commit()
    return released


def _is_write_collision(exc: Exception) -> bool:
    """Did this fail because another worker wrote the same thing first?

    Two leads can be the same person: an OpenAlex author and their ORCID
    record, drained by two workers at once. Both find no existing person, both
    create one, and the second commit hits the unique ORCID key. Retrying
    resolves against the person the first worker just stored. SQLite's busy
    timeout running out is the same situation from the lock's side.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError

    if isinstance(exc, IntegrityError):
        return True
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


def drain_leads(
    session: Session, limit: int = 25, source: str | None = None,
    enrich_chain: bool = False, worker: str | None = None,
) -> tuple[int, int]:
    """Claim and ingest pending leads. Returns (ingested, failed).

    Safe to run in several processes against one database: each takes its own
    leads, and leads it does not get to — an interrupt, a crash — are handed
    back rather than left claimed.
    """
    import time

    worker = worker or worker_id()
    leads = claim_leads(session, worker, limit, source)
    connectors: dict = {}
    ok = failed = 0
    try:
        for lead in leads:
            connectors.setdefault(lead.source, get_connector(lead.source))
            for attempt in range(WRITE_RETRIES + 1):
                try:
                    run_connector(
                        session, connectors[lead.source], lead.identifier,
                        enrich_chain=enrich_chain,
                    )
                    lead.status = "ingested"
                    ok += 1
                    break
                except Exception as exc:
                    if _is_write_collision(exc) and attempt < WRITE_RETRIES:
                        session.rollback()
                        time.sleep(0.2 * (attempt + 1))
                        continue
                    session.rollback()
                    lead.status = "error"
                    failed += 1
                    print(f"lead failed {lead.source}:{lead.identifier}: {exc}")
                    break
            session.commit()
    finally:
        release_leads(session, worker)
    return ok, failed
