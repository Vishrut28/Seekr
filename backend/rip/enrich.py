"""Auto-enrichment: follow identity signals from one source into others.

After a profile is ingested, its own content usually names other places the
same person exists — an ORCID in a GitHub bio, an OpenAlex ID on an ORCID
record, a homepage link on a dblp page. This module follows those hops so a
single `ingest github torvalds` yields a multi-source profile.

Rules:
- Bounded: MAX_DEPTH hops from the root, visited-set prevents cycles.
- Cheap: a hop whose source record already exists is skipped, not re-fetched.
- Isolated: a failing hop is logged to IngestionRun and never fails the root
  ingest.
- Honest: every hop goes through the normal ingest_profile() pipeline, so
  resolution, evidence, and the change feed behave identically.
"""

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors.base import RateLimitedError
from .models import SourceRecord
from .normalize import NormalizedProfile

logger = logging.getLogger("rip.enrich")

MAX_DEPTH = 3
MAX_WEB_PAGES = 2
ORCID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b")
# hosts covered by a dedicated connector, so never fetched as a generic page
SKIP_WEB_HOSTS = {
    "orcid.org", "github.com", "dblp.org", "openalex.org", "huggingface.co",
    "stackoverflow.com", "semanticscholar.org", "scholar.google.com",
    "twitter.com", "x.com", "linkedin.com", "doi.org", "wikipedia.org",
}


@dataclass
class Hop:
    source: str
    identifier: str
    reason: str


@dataclass
class EnrichResult:
    ingested: list[Hop] = field(default_factory=list)
    skipped: list[Hop] = field(default_factory=list)
    failed: list[tuple[Hop, str]] = field(default_factory=list)
    rate_limited: list[tuple[Hop, str]] = field(default_factory=list)


def _has_record(session: Session, source: str, external_id: str) -> bool:
    return session.execute(
        select(SourceRecord.id).where(
            SourceRecord.source == source, SourceRecord.external_id == external_id
        )
    ).first() is not None


def _orcid_from(profile: NormalizedProfile) -> str | None:
    if profile.orcid:
        return profile.orcid.rsplit("/", 1)[-1].upper()
    haystack = " ".join(
        filter(None, [profile.summary, *profile.linked_urls, *profile.websites])
    )
    match = ORCID_RE.search(haystack)
    return match.group(1) if match else None


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _hops_from(profile: NormalizedProfile) -> list[Hop]:
    """What other sources does this profile point at?"""
    hops: list[Hop] = []

    orcid = _orcid_from(profile)
    if orcid and profile.source != "orcid":
        hops.append(Hop("orcid", orcid, f"ORCID {orcid} found in {profile.source} profile"))
    if orcid and profile.source != "openalex":
        # OpenAlex resolves authors by ORCID URL
        hops.append(
            Hop("openalex", f"https://orcid.org/{orcid}", f"OpenAlex author for ORCID {orcid}")
        )

    for url in [*profile.linked_urls, *profile.websites]:
        if not url:
            continue
        host = _host(url)
        path = urlparse(url).path.strip("/")
        if host == "github.com" and path and "/" not in path and profile.source != "github":
            hops.append(Hop("github", path, f"GitHub link on {profile.source} profile"))
        elif host == "dblp.org" and "/pid/" in url and profile.source != "dblp":
            hops.append(Hop("dblp", url.split("/pid/", 1)[-1].removesuffix(".html"),
                            f"dblp link on {profile.source} profile"))
        elif host.endswith("huggingface.co") and path and "/" not in path and profile.source != "huggingface":
            hops.append(Hop("huggingface", path, f"Hugging Face link on {profile.source} profile"))

    # personal sites: only pages no dedicated connector covers
    web_pages = 0
    for url in profile.websites:
        if not url or profile.source == "web":
            continue
        host = _host(url)
        if not host or any(host == h or host.endswith("." + h) for h in SKIP_WEB_HOSTS):
            continue
        if web_pages >= MAX_WEB_PAGES:
            break
        web_pages += 1
        hops.append(Hop("web", url, f"homepage listed on {profile.source} profile"))

    seen = set()
    unique = []
    for hop in hops:
        key = (hop.source, hop.identifier.lower())
        if key not in seen:
            seen.add(key)
            unique.append(hop)
    return unique


def enrich(
    session: Session,
    profile: NormalizedProfile,
    *,
    depth: int = MAX_DEPTH,
    visited: set[tuple[str, str]] | None = None,
    result: EnrichResult | None = None,
) -> EnrichResult:
    """Follow identity signals out of `profile` into other sources."""
    from .connectors import get_connector

    result = result if result is not None else EnrichResult()
    visited = visited if visited is not None else set()
    visited.add((profile.source, profile.external_id.lower()))
    if depth <= 0:
        return result

    for hop in _hops_from(profile):
        key = (hop.source, hop.identifier.lower())
        if key in visited:
            continue
        visited.add(key)
        if _has_record(session, hop.source, hop.identifier):
            result.skipped.append(hop)
            continue
        try:
            connector = get_connector(hop.source)
            fetched = connector.fetch(hop.identifier)
        except RateLimitedError as exc:
            # a throttled source is a temporary condition, not a data problem:
            # skip the hop, stop chaining into that source for this ingest,
            # and leave the root profile intact
            logger.warning("enrich hop rate-limited %s:%s — %s", hop.source, hop.identifier, exc)
            result.rate_limited.append((hop, f"RateLimitedError: {exc}"))
            result.failed.append((hop, f"RateLimitedError: {exc}"))
            continue
        except Exception as exc:
            logger.warning("enrich hop failed %s:%s — %s", hop.source, hop.identifier, exc)
            result.failed.append((hop, f"{type(exc).__name__}: {exc}"))
            continue
        # already ingested under its canonical external_id? don't refetch deeper
        if _has_record(session, fetched.source, fetched.external_id):
            result.skipped.append(hop)
            visited.add((fetched.source, fetched.external_id.lower()))
            continue
        try:
            from .ingest import ingest_profile

            ingest_profile(session, fetched)
            result.ingested.append(hop)
        except Exception as exc:
            session.rollback()
            logger.warning("enrich ingest failed %s:%s — %s", hop.source, hop.identifier, exc)
            result.failed.append((hop, f"{type(exc).__name__}: {exc}"))
            continue
        enrich(session, fetched, depth=depth - 1, visited=visited, result=result)

    return result
