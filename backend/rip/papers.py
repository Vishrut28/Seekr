"""One paper, one row: copies of the same work under one person are merged.

Ingest knows a paper it has seen by its source id or its DOI. The same work
arrives under several of each: ORCID lists one paper three times from three
import feeds, OpenAlex keeps a preprint and the published version as two
works, and an ORCID entry with no DOI never meets the OpenAlex record of the
paper it describes. 540 extra copies sat on 147 people. They are not
cosmetic: a subject counts for a person once two of their papers name it
(search_index.WORK_MENTIONS_TO_MATCH), so one paper held twice passed the
rule alone, and every citation of it counted twice.

WHAT COUNTS AS THE SAME WORK

  The same title, under the same person, in words of four or more -- short
  titles ("Editorial", "Trachoma", "Mobile computing") are reused by
  different works. Two copies dated more than two years apart are kept
  apart, and so are two copies with different DOIs neither of which is a
  preprint's: a conference paper and its journal version, or a paper and its
  erratum, are publications of their own.

WHICH COPY STAYS

  OpenAlex's, where there is one: the conflation check reads per-paper
  institutions and topics from the OpenAlex work it names. Then one with a
  DOI, then the most cited. The others' DOI, citations, topics and author
  list fill in what it lacks, every authorship moves to it, and the copies
  are deleted.

Ingest then reuses a person's existing paper of the same title instead of
creating a copy, so a refresh does not bring the duplicates back.
"""

from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Authorship, PersonSplit, Publication

MIN_TITLE_WORDS = 4
MAX_YEAR_GAP = 2
# DOI prefixes of preprint servers: arXiv, bioRxiv/medRxiv, SSRN, Research
# Square, Preprints.org, TechRxiv. A preprint and its published version are
# one work under two DOIs.
PREPRINT_DOI_PREFIXES = ("10.48550/", "10.1101/", "10.2139/", "10.21203/", "10.20944/",
                         "10.36227/")


def title_key(title: str | None) -> str | None:
    """The title as copies of one work share it, or None when it is too
    short to tell two works apart."""
    words = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split()
    return " ".join(words) if len(words) >= MIN_TITLE_WORDS else None


def _year(pub: Publication) -> int | None:
    match = re.search(r"(1[89]\d\d|20\d\d)", pub.published_date or "")
    return int(match.group(1)) if match else None


def _published_doi(pub: Publication) -> str | None:
    doi = (pub.doi or "").lower()
    if not doi or doi.startswith(PREPRINT_DOI_PREFIXES):
        return None
    return doi


def same_work(a: Publication, b: Publication) -> bool:
    """Two rows of one title: one work, or two publications that share it?"""
    ya, yb = _year(a), _year(b)
    if ya and yb and abs(ya - yb) > MAX_YEAR_GAP:
        return False
    da, db = _published_doi(a), _published_doi(b)
    return not (da and db and da != db)


def _keeper(pubs: list[Publication]) -> Publication:
    return min(pubs, key=lambda p: (not (p.external_id or "").startswith("W"),
                                    not p.doi, -(p.citations or 0), p.id))


def _absorb(keep: Publication, gone: Publication) -> None:
    if not keep.doi and gone.doi:
        keep.doi = gone.doi
    keep.citations = max(keep.citations or 0, gone.citations or 0) or keep.citations
    keep.topics = list(dict.fromkeys([*(keep.topics or []), *(gone.topics or [])]))
    if len(gone.raw_authors or []) > len(keep.raw_authors or []):
        keep.raw_authors = list(gone.raw_authors or [])
    keep.venue = keep.venue or gone.venue
    keep.published_date = keep.published_date or gone.published_date
    keep.url = keep.url or gone.url


def _groups(pubs: list[Publication]) -> list[list[Publication]]:
    """Rows of one person grouped into works: same title key, and every pair
    in a group the same work (complete-link, so one row cannot chain a
    journal version and a conference version together)."""
    by_title: dict[str, list[Publication]] = defaultdict(list)
    for pub in pubs:
        key = title_key(pub.title)
        if key:
            by_title[key].append(pub)
    out: list[list[Publication]] = []
    for rows in by_title.values():
        if len(rows) < 2:
            continue
        clusters: list[list[Publication]] = []
        for pub in sorted(rows, key=lambda p: p.id):
            home = next((c for c in clusters if all(same_work(pub, other) for other in c)), None)
            if home is None:
                clusters.append([pub])
            else:
                home.append(pub)
        out.extend(c for c in clusters if len(c) > 1)
    return out


def merge_duplicate_papers(session: Session, person_ids=None) -> dict:
    """Merge copies of one work under each person (all people when PERSON_IDS
    is None). Returns {"works": n merged, "copies": n rows removed}."""
    query = select(Authorship.person_id, Publication).join(
        Publication, Publication.id == Authorship.publication_id)
    if person_ids is not None:
        query = query.where(Authorship.person_id.in_(list(person_ids)))
    by_person: dict[str, list[Publication]] = defaultdict(list)
    for person_id, pub in session.execute(query).all():
        by_person[person_id].append(pub)

    renamed: dict[int, int] = {}
    works = copies = 0
    for pubs in by_person.values():
        live = [p for p in pubs if p.id not in renamed]
        for group in _groups(live):
            keep = _keeper(group)
            for gone in group:
                if gone is keep or gone.id in renamed:
                    continue
                _absorb(keep, gone)
                have = set(session.execute(select(Authorship.person_id).where(
                    Authorship.publication_id == keep.id)).scalars())
                for authorship in session.execute(select(Authorship).where(
                        Authorship.publication_id == gone.id)).scalars().all():
                    if authorship.person_id in have:
                        session.delete(authorship)
                    else:
                        authorship.publication_id = keep.id
                        have.add(authorship.person_id)
                session.flush()
                renamed[gone.id] = keep.id
                session.delete(gone)
                copies += 1
            works += 1
    if renamed:
        # a split remembers which papers it moved, by id
        for split in session.execute(select(PersonSplit)).scalars():
            ids = [renamed.get(i, i) for i in split.publication_ids or []]
            if ids != list(split.publication_ids or []):
                split.publication_ids = sorted(set(ids))
    session.flush()
    return {"works": works, "copies": copies}


def existing_copy(session: Session, person_id: str, title: str | None, doi: str | None,
                  published_date: str | None) -> Publication | None:
    """This person's paper of the same work, if they already hold one -- what
    ingest reuses instead of storing the work a second time."""
    key = title_key(title)
    if not key or not person_id:
        return None
    probe = Publication(title=title, doi=doi, published_date=published_date)
    for pub in session.execute(
        select(Publication).join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).scalars():
        if title_key(pub.title) == key and same_work(pub, probe):
            return pub
    return None
