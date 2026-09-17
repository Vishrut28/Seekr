"""OpenAlex connector — open scholarly graph API (no key required).

Identifier: OpenAlex author ID (e.g. "A5023888391") or full OpenAlex URL.
Set OPENALEX_MAILTO to join their polite pool (faster, more reliable).
Extracts: name/aliases, ORCID (strong identity key), institutional affiliations,
research topics as evidence, top-cited works as publications.

Use `search_authors(name)` first to find candidate author IDs.
"""

import os
import re

from ..normalize import (
    EvidenceItem,
    NormalizedProfile,
    OrgAffiliation,
    PublicationData,
)
from .base import BaseConnector

API = "https://api.openalex.org"
# 0000-0002-1825-0097, and the 009x ranges whose final character may be X
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dXx]")
MAX_WORKS = 25
# Topical discovery reads authors only from papers with teams up to this size:
# beyond it, authorship says little about one person's own subject.
MAX_COAUTHOR_TEAM = 25

# Exactly the fields `normalize` reads. OpenAlex returns a full record by
# default — abstracts, reference lists, yearly counts, concept vectors — and
# `select` drops them server-side, so a request stays ONE request and carries a
# fraction of the bytes. Measured against the live API in alternating trials:
#   an author's works       1.54s / 292 KB  ->  1.08s / 136 KB
#   a topical works search  2.22s / 1.2 MB  ->  1.60s / 232 KB
#   a batch of ten authors  0.91s /  23 KB  ->  0.53s / 7.5 KB
# A field added to `normalize` must be added here too, or it arrives as None.
AUTHOR_FIELDS = (
    "id,display_name,display_name_alternatives,orcid,works_count,"
    "cited_by_count,last_known_institutions,topics"
)
WORK_FIELDS = (
    "id,title,display_name,doi,publication_date,cited_by_count,"
    "primary_location,topics,authorships"
)


class OpenAlexConnector(BaseConnector):
    source = "openalex"
    source_type = "scholarly"
    min_request_interval = 0.15  # OpenAlex allows 10 req/s

    def __init__(self) -> None:
        super().__init__()
        import threading

        self._prefetch_lock = threading.Lock()
        self._prefetched_authors: dict[str, dict] = {}

    def _params(self, extra: dict | None = None) -> dict:
        params = dict(extra or {})
        mailto = (os.environ.get("OPENALEX_MAILTO") or "").strip()
        # The polite pool asks for an address OpenAlex could actually reach, and
        # it travels in every request URL. A value copied straight out of
        # .env.example is neither, so it is not sent: the reserved example
        # domains (RFC 2606) are the placeholders people leave behind.
        if "@" in mailto and not mailto.lower().endswith(
            ("example.com", "example.org", "example.net")
        ):
            params["mailto"] = mailto
        return params

    def search_authors(self, name: str, limit: int = 10) -> list[dict]:
        """Candidate authors for a name — caller picks the right ID to ingest."""
        data = self.get_json(
            f"{API}/authors",
            params=self._params(
                {"search": name, "per-page": limit, "select": AUTHOR_FIELDS}
            ),
        )
        return [
            {
                "id": a["id"].rsplit("/", 1)[-1],
                "name": a.get("display_name"),
                "orcid": a.get("orcid"),
                "works_count": a.get("works_count"),
                "cited_by": a.get("cited_by_count"),
                "affiliation": (a.get("last_known_institutions") or [{}])[0].get("display_name")
                if a.get("last_known_institutions")
                else None,
            }
            for a in data.get("results", [])
        ]

    def search_authors_by_topic(self, topic: str, limit: int = 10) -> list[dict]:
        """Authors who published on a topic.

        Author-name search is the wrong instrument for "computer vision
        researchers" — it returns people surnamed Rust and index entities like
        "Computer Vision Foundation". Searching works and taking their authors
        finds people who actually work on the subject.
        """
        # Small-team papers only. On a 100-author collaboration paper, being
        # one of the authors says little about working on the topic, yet one
        # such paper filled every candidate slot with consortium members — and
        # its author list made this the heaviest request of a live search
        # (3.4s, 2.2 MB for "soil microbiome researchers").
        # Only the author lists are read here, so that is all the request asks
        # for: the same search came back 1.2 MB and 2.2s in full, 232 KB and
        # 1.6s trimmed.
        params = {"search": topic, "per-page": min(50, limit * 5),
                  "select": "id,authorships"}
        data = self.get_json(f"{API}/works", params=self._params(
            {**params, "filter": f"authors_count:<{MAX_COAUTHOR_TEAM + 1}"}))
        if not data.get("results"):
            # a field where nearly every paper is a large collaboration still
            # deserves an answer
            data = self.get_json(f"{API}/works", params=self._params(params))
        seen: dict[str, dict] = {}
        for work in data.get("results", []):
            for authorship in work.get("authorships", []):
                author = authorship.get("author") or {}
                aid = (author.get("id") or "").rsplit("/", 1)[-1]
                name = author.get("display_name")
                if not aid or not name or aid in seen:
                    continue
                insts = authorship.get("institutions") or [{}]
                seen[aid] = {
                    "id": aid,
                    "name": name,
                    "orcid": author.get("orcid"),
                    "works_count": None,
                    "cited_by": None,
                    "affiliation": insts[0].get("display_name"),
                }
                if len(seen) >= limit:
                    return list(seen.values())
        return list(seen.values())

    @staticmethod
    def _author_key(identifier: str) -> str:
        """The lookup key OpenAlex actually accepts for this identifier.

        An OpenAlex ID may arrive bare or as a URL, so the last path segment is
        the right key. An ORCID must not be reduced that way: the bare number
        404s and only the `orcid:` form resolves, which is why enrichment's
        ORCID hop — the one that ties a scholarly identity to everything else —
        silently failed on every author it was given.
        """
        ident = (identifier or "").strip()
        tail = ident.rsplit("/", 1)[-1]
        if ORCID_RE.fullmatch(tail):
            return f"orcid:{tail}"
        return tail

    def prefetch(self, identifiers: list[str]) -> None:
        """Load many author records in one request.

        Called by live search before fetching profiles concurrently, so each
        `fetch` needs only its works request. Measured: ten authors in one
        request took 1.1s, against ten requests competing for this connector's
        request spacing.
        """
        ids = [i for i in (self._author_key(x) for x in identifiers)
               if i and not i.startswith("orcid:")]
        if not ids:
            return
        with self._prefetch_lock:
            if len(self._prefetched_authors) > 1000:      # left behind by skipped fetches
                self._prefetched_authors.clear()
            for start in range(0, len(ids), 50):
                chunk = ids[start:start + 50]
                data = self.get_json(f"{API}/authors", params=self._params({
                    "filter": "openalex_id:" + "|".join(chunk), "per-page": len(chunk),
                    "select": AUTHOR_FIELDS}))
                for author in data.get("results", []):
                    self._prefetched_authors[author["id"].rsplit("/", 1)[-1]] = author

    def fetch(self, identifier: str) -> NormalizedProfile:
        key = self._author_key(identifier)
        with self._prefetch_lock:
            author = self._prefetched_authors.pop(key, None)
        if author is None:
            author = self.get_json(
                f"{API}/authors/{key}", params=self._params({"select": AUTHOR_FIELDS})
            )
        # Works are filtered by the canonical OpenAlex ID from the author we
        # just resolved — an `orcid:` key is valid for lookup but not as a
        # value for author.id, so reusing the request key would return nothing.
        #
        # ONE works request, on purpose. Splitting it into a slim request plus a
        # small-team author-list request was tried and measured: it cut
        # consortium payloads from 5 MB to 0.1 MB, but OpenAlex's response time
        # is roughly flat per request, so two requests made a typical author
        # slower (0.85s -> ~2.7s). `select` gets the same saving inside one
        # request instead, which is why WORK_FIELDS exists.
        author_id = author["id"].rsplit("/", 1)[-1]
        if author.get("works_count") == 0:
            return self.normalize(author, [])       # nothing to ask for
        works = self.get_json(
            f"{API}/works",
            params=self._params({
                "filter": f"author.id:{author_id}",
                "sort": "cited_by_count:desc",
                "per-page": MAX_WORKS,
                "select": WORK_FIELDS,
            }),
        )
        return self.normalize(author, works.get("results", []))

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(raw["author"], raw["works"])

    def normalize(self, author: dict, works: list[dict]) -> NormalizedProfile:
        author_id = author["id"].rsplit("/", 1)[-1]
        name = author.get("display_name")

        country = next(
            (
                inst.get("country_code")
                for inst in (author.get("last_known_institutions") or [])
                if inst.get("country_code")
            ),
            None,
        )
        organizations = [
            OrgAffiliation(
                name=inst["display_name"],
                relation="worked_at",
                org_type=inst.get("type"),
                is_current=True,
                url=None,
            )
            for inst in (author.get("last_known_institutions") or [])
            if inst.get("display_name")
        ]

        topics = [t for t in (author.get("topics") or [])[:15] if t.get("display_name")]
        evidence = [
            EvidenceItem(
                attribute_type="research_interest",
                value=topic["display_name"],
                extracted_info=f"OpenAlex topic (count={topic.get('count')})",
                url=author.get("id"),
                confidence=0.6,
            )
            for topic in topics
        ]
        # The subfield and field OpenAlex files those topics under — what a
        # broad question asks about. "Glioma Diagnosis and Treatment" is filed
        # under Oncology and Medicine, and nobody states "oncology" as a topic.
        # Weaker than the topics themselves (search counts them for half).
        filed: dict[str, tuple[str, float]] = {}
        for topic in topics:
            for level, confidence in (("subfield", 0.45), ("field", 0.35)):
                label = (topic.get(level) or {}).get("display_name")
                if label and label not in filed:
                    filed[label] = (f"OpenAlex {level} of '{topic['display_name']}'", confidence)
        evidence += [
            EvidenceItem(attribute_type="research_field", value=label, extracted_info=info,
                         url=author.get("id"), confidence=confidence)
            for label, (info, confidence) in filed.items()
        ]

        publications = []
        for work in works:
            title = work.get("display_name") or work.get("title")
            if not title:
                continue
            authorships = work.get("authorships") or []
            authors = [
                (a.get("author") or {}).get("display_name")
                for a in authorships
            ]
            position = None
            for i, a in enumerate(authorships):
                if ((a.get("author") or {}).get("id") or "").endswith(author_id):
                    position = i + 1
                    break
            venue = None
            loc = work.get("primary_location") or {}
            if loc.get("source"):
                venue = loc["source"].get("display_name")
            publications.append(
                PublicationData(
                    title=title[:1024],
                    external_id=work["id"].rsplit("/", 1)[-1],
                    venue=venue,
                    published_date=work.get("publication_date"),
                    url=work.get("doi") or work.get("id"),
                    doi=(work.get("doi") or "").replace("https://doi.org/", "") or None,
                    citations=work.get("cited_by_count"),
                    topics=[
                        t["display_name"] for t in (work.get("topics") or [])[:5]
                        if t.get("display_name")
                    ],
                    raw_authors=[a for a in authors if a],
                    author_position=position,
                )
            )

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=author_id,
            url=author.get("id") or f"{API}/authors/{author_id}",
            raw={"author": author, "works": works},
            name=name,
            aliases=list(author.get("display_name_alternatives") or []),
            country=country.upper() if country else None,
            orcid=author.get("orcid"),
            organizations=organizations,
            evidence=evidence,
            publications=publications,
        )
