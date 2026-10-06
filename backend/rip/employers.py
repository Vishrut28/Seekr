"""Employers for people no source names one for, from their papers.

A Semantic Scholar or ORCID record often has papers and no employer, and a
person with no employer cannot answer "researchers at Stanford". The paper
itself says where its author was: OpenAlex keeps, for every author line of a
work, the institutions that author gave. So the papers' DOIs are looked up
in OpenAlex, the one author line carrying this person's name is read, and the
same rule as connectors.openalex.employers_from_papers decides what counts.

Each work is read only when exactly one of its author lines is this person's
name written some way (names.alias_fits, sharing a word): a paper with two
authors of that name says nothing about which one is ours. The rows are
added beside whatever a source states, never in place of it, and carry the
work they came from.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from .connectors.openalex import employers_from_papers
from .models import Affiliation, Authorship, ChangeLog, Person, Publication
from .names import _name_words, alias_fits

WORK_FIELDS = "id,doi,publication_date,authorships"
DOIS_PER_PERSON = 10
SELF = "SELF"


def without_employer(session: Session) -> list[str]:
    """People with papers that carry a DOI and no employer at all."""
    return list(session.execute(
        select(Person.id).where(
            Person.merged_into.is_(None),
            ~exists().where(Affiliation.person_id == Person.id),
            exists().where(Authorship.person_id == Person.id,
                           Authorship.publication_id == Publication.id,
                           Publication.doi.is_not(None)),
        )).scalars())


def _theirs(name: str, author_name: str | None) -> bool:
    shared = {w for w in _name_words(name) if len(w) > 1} & set(_name_words(author_name))
    return bool(shared) and alias_fits(name, author_name)


def own_author_line(name: str, work: dict) -> dict | None:
    """The one author line of WORK that is NAME, or None if there are none or two."""
    lines = [a for a in work.get("authorships") or []
             if _theirs(name, (a.get("author") or {}).get("display_name"))
             or _theirs(name, a.get("raw_author_name"))]
    return lines[0] if len(lines) == 1 else None


def employers_by_doi(session: Session, person_ids: list[str],
                     fetch: Callable[[list[str]], list[dict]]) -> dict:
    """Add employers read from each person's own author lines. FETCH takes
    DOIs and returns OpenAlex works with authorships (connector.works_by_doi).
    Returns {"people": n given an employer, "rows": n affiliations added,
    "looked_up": n DOIs asked about}."""
    from .ingest import _get_or_create_org

    wanted: dict[str, list[str]] = {}
    for pid in person_ids:
        stored = session.execute(
            select(Publication.doi).join(Authorship, Authorship.publication_id == Publication.id)
            .where(Authorship.person_id == pid, Publication.doi.is_not(None))
            .order_by(Publication.published_date.desc())
        ).scalars().all()
        dois = [d.lower() for d in dict.fromkeys(stored) if d][:DOIS_PER_PERSON]
        if dois:
            wanted[pid] = dois
    every = sorted({d for ds in wanted.values() for d in ds})
    works: dict[str, dict] = {}
    for work in fetch(every) if every else []:
        doi = (work.get("doi") or "").lower().replace("https://doi.org/", "")
        if doi:
            works[doi] = work

    people = rows = 0
    for pid, dois in wanted.items():
        person = session.get(Person, pid)
        if person is None or not person.canonical_name:
            continue
        mine: list[dict] = []
        source_of: dict[str, str] = {}
        for doi in dois:
            found = works.get(doi)
            if found is None:
                continue
            work = found
            line = own_author_line(person.canonical_name, work)
            if line is None:
                continue
            mine.append({"id": work.get("id"), "publication_date": work.get("publication_date"),
                         "authorships": [{"author": {"id": SELF},
                                          "institutions": line.get("institutions") or []}]})
            for inst in line.get("institutions") or []:
                source_of.setdefault(inst.get("display_name") or "", work.get("id") or "")
        orgs, _country, _seen = employers_from_papers(SELF, mine)
        if not orgs:
            continue
        for org_aff in orgs:
            org = _get_or_create_org(session, org_aff.name, org_aff.org_type, None)
            session.add(Affiliation(
                person_id=pid, organization_id=org.id, relation="worked_at",
                start_date=org_aff.start_date, end_date=org_aff.end_date,
                is_current=org_aff.is_current, url=source_of.get(org_aff.name)))
            rows += 1
        first = next((o.name for o in orgs if o.is_current), None)
        if first and not person.current_organization:
            person.current_organization = first
            session.add(ChangeLog(person_id=pid, field="current_organization", old_value=None,
                                  new_value=first))
        people += 1
    session.flush()
    return {"people": people, "rows": rows, "looked_up": len(every)}
