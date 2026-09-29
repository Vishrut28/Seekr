"""The normalized intermediate representation every connector emits.

Connectors translate source-specific payloads into a NormalizedProfile;
everything downstream (resolution, persistence, API) only ever sees this
shape, so adding a new source never touches the core pipeline.
"""

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse


@dataclass
class EvidenceItem:
    attribute_type: str  # "skill" | "role" | "education" | "location" | "research_interest" ...
    value: str
    extracted_info: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    confidence: float = 0.5


@dataclass
class OrgAffiliation:
    name: str
    relation: str = "worked_at"  # or "studied_at"
    role: str | None = None
    org_type: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    url: str | None = None


@dataclass
class PublicationData:
    title: str
    external_id: str | None = None
    venue: str | None = None
    published_date: str | None = None
    url: str | None = None
    doi: str | None = None
    citations: int | None = None
    topics: list[str] = field(default_factory=list)
    # None where a source lists an author with no name, so positions still line up
    raw_authors: list[str | None] = field(default_factory=list)
    author_position: int | None = None


@dataclass
class ProjectData:
    name: str
    description: str | None = None
    url: str | None = None
    technologies: list[str] = field(default_factory=list)
    organization: str | None = None
    activity: dict = field(default_factory=dict)
    started_at: str | None = None
    last_active_at: str | None = None
    role: str | None = None


@dataclass
class NormalizedProfile:
    source: str
    source_type: str
    external_id: str
    url: str
    raw: dict
    name: str | None = None
    aliases: list[str] = field(default_factory=list)
    location: str | None = None
    country: str | None = None  # ISO-3166 alpha-2, when a source states it
    summary: str | None = None
    orcid: str | None = None
    emails: list[str] = field(default_factory=list)
    usernames: list[str] = field(default_factory=list)  # "github:login" style
    websites: list[str] = field(default_factory=list)
    linked_urls: list[str] = field(default_factory=list)  # cross-profile links for resolution
    organizations: list[OrgAffiliation] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    publications: list[PublicationData] = field(default_factory=list)
    projects: list[ProjectData] = field(default_factory=list)


def normalize_url(url: str) -> str | None:
    """Canonical form for URL matching: lowercase host, no scheme/www, no trailing slash."""
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = parsed.path.rstrip("/")
    key = f"{host}{path}"
    # query strings are identity-bearing on profile URLs
    # (scholar.google.com/citations?user=X, openreview.net/profile?id=Y)
    if parsed.query:
        key += f"?{parsed.query}"
    return key


def strong_keys(profile: NormalizedProfile) -> list[tuple[str, str]]:
    """Deterministic identity keys for entity resolution."""
    keys: list[tuple[str, str]] = []
    if profile.orcid:
        keys.append(("orcid", profile.orcid.rsplit("/", 1)[-1].upper()))
    for email in profile.emails:
        keys.append(("email", email.strip().lower()))
    for username in profile.usernames:
        keys.append(("username", username.strip().lower()))
    for url in [profile.url, *profile.websites, *profile.linked_urls]:
        norm = normalize_url(url) if url else None
        # bare domains of big platforms are useless as identity; require a path
        if norm and "/" in norm:
            keys.append(("url", norm))
        elif norm and norm not in {"github.com", "twitter.com", "linkedin.com", "x.com"}:
            keys.append(("url", norm))  # personal domain root is a valid identity signal
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out
