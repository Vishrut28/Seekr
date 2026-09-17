"""Semantic Scholar connector — official Academic Graph API (no key required).

Identifier: Semantic Scholar author ID (e.g. "1741101"); find one with
`search_authors`. Set SEMANTIC_SCHOLAR_API_KEY for higher rate limits
(unauthenticated pool is shared and slow — we keep a conservative interval).
Extracts: name, homepage (strong resolution link), affiliations, scholarly
metrics as evidence, top papers as publications (DOI/ArXiv ids preserved,
deduped against OpenAlex/dblp/ORCID by DOI).
"""

import os

from ..normalize import (
    EvidenceItem,
    NormalizedProfile,
    OrgAffiliation,
    PublicationData,
)
from .base import BaseConnector

API = "https://api.semanticscholar.org/graph/v1"
AUTHOR_FIELDS = "name,affiliations,homepage,externalIds,paperCount,citationCount,hIndex"
PAPER_FIELDS = "title,year,venue,externalIds,citationCount,authors"
MAX_PAPERS = 50
# On a paper with more authors than this, being one of them says little about
# working on its subject (see OpenAlexConnector.search_authors_by_topic).
MAX_TOPIC_TEAM = 15


class SemanticScholarConnector(BaseConnector):
    source = "semanticscholar"
    source_type = "scholarly"
    min_request_interval = 3.0  # shared unauthenticated pool; be conservative

    def default_headers(self) -> dict:
        headers = super().default_headers()
        key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if key:
            headers["x-api-key"] = key
        return headers

    def search_authors(self, name: str, limit: int = 10) -> list[dict]:
        data = self.get_json(
            f"{API}/author/search",
            params={"query": name, "fields": "name,affiliations,paperCount,citationCount", "limit": limit},
        )
        return [
            {
                "id": a["authorId"],
                "name": a.get("name"),
                "affiliations": a.get("affiliations") or [],
                "papers": a.get("paperCount"),
                "citations": a.get("citationCount"),
            }
            for a in data.get("data") or []
        ]

    def search_authors_by_topic(self, topic: str, limit: int = 10) -> list[dict]:
        """Authors of papers on TOPIC, most papers on it first.

        One request: paper search returns author ids and names inline. The
        shared unauthenticated pool answers this with 429s more often than
        not, so the live search only asks when SEMANTIC_SCHOLAR_API_KEY is set
        (a free key, requested at semanticscholar.org/product/api).
        """
        data = self.get_json(
            f"{API}/paper/search",
            params={"query": topic, "fields": "authors,citationCount",
                    "limit": min(100, max(10, limit * 5))},
        )
        found: dict[str, dict] = {}
        order: list[str] = []
        for paper in data.get("data") or []:
            authors = paper.get("authors") or []
            if len(authors) > MAX_TOPIC_TEAM:
                continue
            for a in authors:
                aid, name = a.get("authorId"), a.get("name")
                if not aid or not name:
                    continue
                if aid not in found:
                    found[aid] = {"id": str(aid), "name": name, "affiliations": [],
                                  "papers": None, "citations": None, "on_topic": 0}
                    order.append(aid)
                found[aid]["on_topic"] += 1
        # stable: equal counts keep search-relevance order
        ranked = sorted(order, key=lambda aid: -found[aid]["on_topic"])
        return [found[aid] for aid in ranked[:limit]]

    def fetch(self, identifier: str) -> NormalizedProfile:
        author_id = identifier.rstrip("/").rsplit("/", 1)[-1]
        author = self.get_json(f"{API}/author/{author_id}", params={"fields": AUTHOR_FIELDS})
        papers = self.get_json(
            f"{API}/author/{author_id}/papers",
            params={"fields": PAPER_FIELDS, "limit": MAX_PAPERS},
        )
        return self.normalize(author, papers.get("data") or [])

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(raw["author"], raw["papers"])

    def normalize(self, author: dict, papers: list[dict]) -> NormalizedProfile:
        author_id = str(author["authorId"])
        profile_url = f"https://www.semanticscholar.org/author/{author_id}"

        websites = [author["homepage"]] if author.get("homepage") else []

        organizations = [
            OrgAffiliation(name=aff, relation="worked_at", is_current=True)
            for aff in author.get("affiliations") or []
            if aff
        ]

        evidence = []
        if author.get("paperCount"):
            evidence.append(
                EvidenceItem(
                    attribute_type="scholarly_metrics",
                    value=f"{author['paperCount']} papers, {author.get('citationCount', 0)} citations, h-index {author.get('hIndex', 0)}",
                    extracted_info="Semantic Scholar author metrics",
                    url=profile_url,
                    confidence=0.8,
                )
            )

        publications = []
        for paper in papers:
            title = paper.get("title")
            if not title:
                continue
            ext = paper.get("externalIds") or {}
            doi = ext.get("DOI")
            authors = [a.get("name") for a in paper.get("authors") or [] if a.get("name")]
            position = None
            for i, a in enumerate(paper.get("authors") or []):
                if str(a.get("authorId")) == author_id:
                    position = i + 1
                    break
            publications.append(
                PublicationData(
                    title=title[:1024],
                    external_id=f"doi:{doi}" if doi else f"s2:{paper.get('paperId')}",
                    venue=paper.get("venue") or None,
                    published_date=str(paper["year"]) if paper.get("year") else None,
                    url=f"https://doi.org/{doi}" if doi
                    else f"https://www.semanticscholar.org/paper/{paper.get('paperId')}",
                    doi=doi,
                    citations=paper.get("citationCount"),
                    raw_authors=authors,
                    author_position=position,
                )
            )

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=author_id,
            url=profile_url,
            raw={"author": author, "papers": papers},
            name=author.get("name"),
            websites=websites,
            organizations=organizations,
            evidence=evidence,
            publications=publications,
        )
