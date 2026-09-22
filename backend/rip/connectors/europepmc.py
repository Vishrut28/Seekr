"""Europe PMC connector — EBI's life-science literature API (no key, no email).

Identifier: an ORCID. Europe PMC publishes no author IDs of its own, and a
name is not an identity here, so only authors who publish with an ORCID can be
ingested. That is a feature as much as a limit: an ORCID is a strong key, so
every record this produces resolves onto the person who already holds it
instead of creating another near-duplicate to review.

Extracts: name (built from firstName/lastName, because Europe PMC's own
`fullName` is initials-style — "Tan C" — and an initials-only name is
second-class everywhere downstream), institutional affiliation, article
keywords and MeSH terms as evidence, and articles as publications with DOIs
and citation counts.

It covers medicine and biology, where OpenAlex carries thinner affiliation
data, and it gives a live search a second topical source, so one slow provider
no longer sets the pace on its own.

Emails appear in Europe PMC affiliation strings (corresponding-author
contacts). They are stripped rather than collected: the project takes an email
only where someone published it on their own profile, and a paper's
affiliation line is not that.
"""

import re
from collections import Counter

from rapidfuzz import fuzz

from .. import geo
from ..normalize import (
    EvidenceItem,
    NormalizedProfile,
    OrgAffiliation,
    PublicationData,
)
from ..textnorm import fold
from .base import BaseConnector

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dXx]")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Articles read per request. Europe PMC's response time rises steeply with
# page size on a broad query (0.6s for 10, 6s+ for 25 on "soil microbiome"),
# and ten articles already yield tens of identifiable authors.
TOPIC_PAGE = 10
MAX_ARTICLES = 50
# An author of a 40-name clinical consortium paper is not thereby working on
# the subject — the same rule the OpenAlex connector applies.
MAX_COAUTHOR_TEAM = 25
# How close an author name must be to the name searched for.
NAME_MATCH = 85
# Words that mark the part of an affiliation string naming an institution
# rather than a department, a city or a postcode.
INSTITUTION_WORDS = (
    "university", "universite", "universidad", "universita", "universitat",
    "hospital", "institute", "institut", "college", "school", "centre",
    "center", "laboratory", "laboratoire", "clinic", "academy", "foundation",
    "trust", "faculty", "cnrs", "inserm", "nih", "csic", "csir",
)


class EuropePmcConnector(BaseConnector):
    source = "europepmc"
    source_type = "scholarly"
    min_request_interval = 0.25  # EBI asks for a reasonable rate, not a quota
    # Europe PMC's median search is about 1.4s and its tail is not: the same
    # request for "soil microbiome" has come back in 0.55s and in 14.4s. A
    # live search would rather go without this source than wait, and the
    # per-source failure is reported on the result.
    request_timeout = 8.0

    def _search(self, query: str, page_size: int, sort: str | None = None) -> list[dict]:
        params = {"query": query, "resultType": "core", "pageSize": page_size,
                  "format": "json"}
        if sort:
            params["sort"] = sort
        data = self.get_json(f"{API}/search", params=params)
        if not isinstance(data, dict):
            return []
        return (data.get("resultList") or {}).get("result") or []

    # ---- discovery -------------------------------------------------------

    def search_authors(self, name: str, limit: int = 10) -> list[dict]:
        """Candidates for a name — only those publishing with an ORCID.

        Often none: the four Stéphane Laurents in Europe PMC's hearing and
        medicinal-chemistry papers carry no ORCID on any of them, while 62
        of their co-authors do. Returning nothing is the honest answer —
        those records cannot be told apart from each other.
        """
        articles = self._search(f'AUTH:"{name}"', min(25, max(10, limit * 2)))
        return self._identifiable_authors(articles, limit, name_like=name)

    def search_authors_by_topic(self, topic: str, limit: int = 10) -> list[dict]:
        """Authors who publish on a subject, from small-team papers.

        Sorted by citations rather than by relevance: it picks the authors an
        established literature agrees on, and it is the only shape whose
        latency stayed inside two seconds across every topic measured —
        relevance ranking over a few million hits is what goes slow.
        """
        articles = self._search(topic, TOPIC_PAGE, sort="CITED desc")
        return self._identifiable_authors(articles, limit)

    def _identifiable_authors(
        self, articles: list[dict], limit: int, name_like: str | None = None
    ) -> list[dict]:
        wanted = fold(name_like) if name_like else ""
        seen: dict[str, dict] = {}
        for article in articles:
            authors = _authors(article)
            if len(authors) > MAX_COAUTHOR_TEAM:
                continue
            for author in authors:
                orcid = _orcid_of(author)
                if not orcid or orcid in seen:
                    continue
                name = _display_name(author)
                if not name:
                    continue
                # A shared surname is not the person asked for: searching
                # "Stephane Laurent" must not return their co-author Laurent
                # Fabre, whose ORCID happens to be on the paper.
                if wanted and fuzz.token_sort_ratio(wanted, fold(name)) < NAME_MATCH:
                    continue
                seen[orcid] = {
                    "id": orcid,
                    "name": name,
                    "orcid": orcid,
                    "works_count": None,
                    "cited_by": None,
                    "affiliation": _institution(_affiliation(author)),
                }
                if len(seen) >= limit:
                    return list(seen.values())
        return list(seen.values())

    # ---- fetch -----------------------------------------------------------

    def fetch(self, identifier: str) -> NormalizedProfile:
        orcid = _plain_orcid(identifier)
        if not orcid:
            raise ValueError(
                f"Europe PMC identifies people by ORCID; {identifier!r} is not one"
            )
        articles = self._search(f'AUTHORID:"{orcid}"', MAX_ARTICLES, sort="CITED desc")
        return self.normalize(orcid, articles)

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(raw["orcid"], raw["articles"])

    def normalize(self, orcid: str, articles: list[dict]) -> NormalizedProfile:
        # An ORCID in the literature is not always one person's: the ORCID
        # documentation example 0000-0002-1825-0097 is attached to papers by a
        # dermatologist in Nanjing and a computer scientist in Halifax, each
        # having pasted it into a submission form. Only the articles whose
        # entry for this ORCID agrees with the commonest name are kept, so a
        # mistyped identifier cannot fuse two strangers into one person.
        mine: list[tuple[dict, dict]] = []
        for article in articles:
            for author in _authors(article):
                if _orcid_of(author) == orcid and _display_name(author):
                    mine.append((article, author))
                    break
        keys = Counter(_name_key(author) for _, author in mine)
        agreed = keys.most_common(1)[0][0] if keys else None
        mine = [(a, au) for a, au in mine if _name_key(au) == agreed]

        names = Counter(_display_name(author) for _, author in mine)
        name = names.most_common(1)[0][0] if names else None
        aliases = sorted(n for n in names if n != name)

        affiliations = [_affiliation(author) for _, author in mine]
        institutions: list[str] = []
        for text in affiliations:
            org = _institution(text)
            if org and org not in institutions:
                institutions.append(org)
        country = next(
            (geo.country_in_text(text) for text in affiliations if geo.country_in_text(text)),
            None,
        )

        topics: Counter = Counter()
        for article, _ in mine:
            for keyword in (article.get("keywordList") or {}).get("keyword") or []:
                if keyword:
                    topics[keyword.strip()] += 1
            for heading in (article.get("meshHeadingList") or {}).get("meshHeading") or []:
                descriptor = heading.get("descriptorName")
                if descriptor and heading.get("majorTopic_YN") != "N":
                    topics[descriptor] += 1

        url = f"https://europepmc.org/search?query=AUTHORID%3A%22{orcid}%22"
        evidence = [
            EvidenceItem(
                attribute_type="research_interest",
                value=topic,
                extracted_info=f"Europe PMC: subject of {n} of this author's articles",
                url=url,
                confidence=0.6 if n > 1 else 0.5,
            )
            for topic, n in topics.most_common(15)
        ]

        publications = []
        for article, author in mine:
            title = (article.get("title") or "").strip().rstrip(".")
            if not title:
                continue
            authors = [_display_name(a) or a.get("fullName") for a in _authors(article)]
            authors = [a for a in authors if a]
            position = next(
                (i + 1 for i, a in enumerate(_authors(article)) if _orcid_of(a) == orcid),
                None,
            )
            doi = article.get("doi")
            article_id = article.get("id") or ""
            publications.append(
                PublicationData(
                    title=title[:1024],
                    external_id=f"doi:{doi}" if doi
                    else f"epmc:{article.get('source', '')}:{article_id}",
                    venue=((article.get("journalInfo") or {}).get("journal") or {}).get("title"),
                    published_date=article.get("firstPublicationDate") or article.get("pubYear"),
                    url=f"https://doi.org/{doi}" if doi
                    else f"https://europepmc.org/article/{article.get('source', 'MED')}/{article_id}",
                    doi=doi,
                    citations=article.get("citedByCount"),
                    topics=[
                        k.strip() for k in
                        ((article.get("keywordList") or {}).get("keyword") or [])[:5] if k
                    ],
                    raw_authors=authors,
                    author_position=position,
                )
            )

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=orcid,
            url=url,
            raw={"orcid": orcid, "articles": articles},
            name=name,
            aliases=aliases,
            country=country,
            orcid=orcid,
            # the same person's ORCID record, for resolution to link them
            linked_urls=[f"https://orcid.org/{orcid}"],
            organizations=[
                OrgAffiliation(name=org, relation="worked_at", is_current=i == 0, url=None)
                for i, org in enumerate(institutions[:5])
            ],
            evidence=evidence,
            publications=publications,
        )


def _plain_orcid(identifier: str) -> str | None:
    match = ORCID_RE.search(identifier or "")
    return match.group(0).upper() if match else None


def _authors(article: dict) -> list[dict]:
    return (article.get("authorList") or {}).get("author") or []


def _orcid_of(author: dict) -> str | None:
    ident = author.get("authorId") or {}
    if ident.get("type") == "ORCID":
        return _plain_orcid(ident.get("value") or "")
    return None


def _display_name(author: dict) -> str | None:
    """A real name where Europe PMC gives one.

    `fullName` is "Tan C"; firstName + lastName is "Cheng Tan", which is what
    resolution, ranking and the merge rules all need.
    """
    first, last = (author.get("firstName") or "").strip(), (author.get("lastName") or "").strip()
    if first and last:
        return f"{first} {last}"
    return (author.get("fullName") or "").strip() or None


def _name_key(author: dict) -> str:
    """Surname plus first initial — the coarsest form two spellings share."""
    name = (_display_name(author) or "").lower()
    words = [w for w in re.findall(r"\w+", name) if w]
    if not words:
        return ""
    return f"{words[-1]}|{words[0][:1]}"


def _affiliation(author: dict) -> str:
    details = (author.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or []
    text = details[0].get("affiliation") if details else author.get("affiliation")
    return EMAIL_RE.sub("", text or "").strip()


def _institution(affiliation: str | None) -> str | None:
    """The part of an affiliation string that names an institution.

    "Department of Dermatology, Affiliated Hospital of Nanjing University of
    Chinese Medicine, Nanjing, China." is one string holding a department, an
    institution, a city and a country. Storing it whole would make every
    affiliation unique and unsearchable.
    """
    parts = [p.strip(" .;") for p in (affiliation or "").split(",")]
    named = [p for p in parts
             if len(p) > 4 and any(w in p.lower() for w in INSTITUTION_WORDS)]
    if not named:
        return None
    # The shortest qualifying part is the institution itself: "Kailuan
    # Hospital" rather than "Department of Cardiology Kailuan Hospital North
    # China University of Science and Technology Tangshan China".
    return min(named, key=len)[:255]
