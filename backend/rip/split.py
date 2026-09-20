"""Pull one person record apart into the people it actually describes.

rip.review.split_link already detaches a whole SOURCE RECORD into its own
person, which answers "these two sources are different people". It cannot
answer this one: here a single record is the problem. One OpenAlex author id
in this corpus carries a paediatric surgeon's operations and a
combinatorialist's theorems, and every row on both sides shares its
source_record_id, so there is nothing for that function to divide on.

What divides cleanly is the papers. rip.conflation already groups them by what
they share, so a split takes a set of publications and gives them to a new
person.

WHAT MOVES, AND WHY EACH

  authorships   the papers themselves — the thing being divided
  affiliations  by the institutions THIS author put on the moved papers, the
                same per-paper data the employer check reads
  evidence      NOT divided: recomputed. OpenAlex's topic evidence comes from
                its author-level topic list, which describes the conflated
                record as a whole and has no per-paper provenance to divide
                on. Both sides get subjects derived from their own papers
                instead, which is the only honest attribution available.

WHAT DOES NOT MOVE

  strong keys   an ORCID belongs to whoever the source thought the author was,
                and nothing here can say which half earned it. It stays, and
                the new person has none. Guessing would assert an identity.
  the source
  record        it stays with the original, because ingest looks its link up
                with one-or-none and a second link would make the next
                refresh raise instead of run.

WHY THE RECORD IS THEN FROZEN

A split cannot be re-derived from the source, because the source is what got
it wrong. So it is written down (models.PersonSplit) and ingest refuses that
record afterwards. The alternative is a refresh silently putting the two
people back together, which is worse than a record that stops updating — and
the record is known to be wrong, so freezing it costs little.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (Affiliation, Authorship, Evidence, IdentityLink,
                     Organization, Person, PersonSplit, Publication,
                     SourceRecord)

# What a paper's own topics are worth as a claim about the person: the same
# confidences the OpenAlex connector uses for the author-level list, because
# it is the same kind of claim from a narrower base.
TOPIC_CONFIDENCE = 0.6
FIELD_CONFIDENCE = 0.35
# The same ceiling the OpenAlex connector puts on an author's topic list.
# Without it a split person ends up with every topic their papers mention —
# 39 where the source had summarised 13 — and reads as noisier than
# everybody who was never split, for no reason a reader could guess.
MAX_SUBJECTS = 15
DERIVED_ATTRS = ("research_interest", "research_field")


@dataclass
class SplitResult:
    from_person_id: str
    to_person_id: str
    papers_moved: int
    evidence_rewritten: int
    affiliations_moved: int
    frozen_record_id: int | None


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError):
        return []


def _paper_topics(session: Session, publication_ids) -> list[str]:
    """The subjects a set of papers is actually about, commonest first."""
    counted: dict[str, int] = {}
    if not publication_ids:
        return []
    for (raw,) in session.execute(
        select(Publication.topics).where(Publication.id.in_(list(publication_ids)))
    ):
        for topic in _as_list(raw):
            counted[topic] = counted.get(topic, 0) + 1
    ranked = sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, _n in ranked[:MAX_SUBJECTS]]


def _rewrite_subjects(session: Session, person: Person, record: SourceRecord | None) -> int:
    """Replace a person's derived subjects with what their own papers say.

    Only the attributes a source derived from the whole conflated record are
    touched. A skill somebody typed about themselves on ORCID or Hugging Face
    is their own word and is left alone — it may belong to either half, and
    nothing here can tell.
    """
    papers = session.execute(
        select(Authorship.publication_id).where(Authorship.person_id == person.id)
    ).scalars().all()
    topics = _paper_topics(session, papers)
    if not topics:
        # Nothing to replace them with, so nothing is taken away. Papers from
        # Semantic Scholar and Europe PMC carry no topics, and rewriting on
        # that basis deleted every subject a person had and wrote back none —
        # it left a 47-paper physicist unfindable by any subject at all. An
        # imprecise inherited subject beats no subject.
        return 0

    dropped = 0
    for row in session.execute(
        select(Evidence).where(
            Evidence.person_id == person.id,
            Evidence.attribute_type.in_(DERIVED_ATTRS),
        )
    ).scalars().all():
        session.delete(row)
        dropped += 1
    # Before the inserts: a rewritten subject usually has the same
    # (person, type, value, record) as the row it replaces, and evidence is
    # unique on exactly that, so an unflushed delete collides with its own
    # replacement.
    session.flush()

    for topic in topics:
        session.add(Evidence(
            person_id=person.id, attribute_type="research_interest", value=topic,
            extracted_info="from this person's own papers, after a manual split",
            source=(record.source if record else "split"),
            source_record_id=(record.id if record else None),
            confidence=TOPIC_CONFIDENCE, verification_state="unverified",
        ))
    return dropped + len(topics)


def _own_institutions(record: SourceRecord | None, publication_ids_by_work) -> set:
    """Institutions this author listed on the moved papers."""
    if record is None:
        return set()
    payload = record.raw if isinstance(record.raw, dict) else json.loads(record.raw or "{}")
    mine = ((payload.get("author") or {}).get("id") or "").rsplit("/", 1)[-1]
    if not mine:
        return set()
    found = set()
    for work in payload.get("works") or []:
        key = (work.get("id") or "").rsplit("/", 1)[-1]
        if key not in publication_ids_by_work:
            continue
        for line in work.get("authorships") or []:
            who = ((line.get("author") or {}).get("id") or "").rsplit("/", 1)[-1]
            if who == mine:
                found |= {i.get("display_name") for i in line.get("institutions") or []
                          if i.get("display_name")}
    return found


def split_off(session: Session, person_id: str, publication_ids: list[int],
              name: str | None = None, note: str | None = None) -> SplitResult:
    """Move PUBLICATION_IDS off this person onto a new one.

    Raises ValueError rather than leaving half a split behind: the papers must
    all belong to this person, and some must be left, because moving every
    paper renames somebody rather than splitting them.
    """
    person = session.get(Person, person_id)
    if person is None:
        raise ValueError(f"person {person_id} not found")
    wanted = set(publication_ids)
    if not wanted:
        raise ValueError("no publications given to split off")

    theirs = set(session.execute(
        select(Authorship.publication_id).where(Authorship.person_id == person_id)
    ).scalars().all())
    stray = wanted - theirs
    if stray:
        raise ValueError(f"not this person's papers: {sorted(stray)[:5]}")
    if wanted == theirs:
        raise ValueError("that is every paper they have; a split has to leave some")

    record = session.execute(
        select(SourceRecord)
        .join(IdentityLink, IdentityLink.source_record_id == SourceRecord.id)
        .where(IdentityLink.person_id == person_id)
        .order_by(SourceRecord.id)
    ).scalars().first()

    other = Person(canonical_name=name or person.canonical_name,
                   location=person.location, country=person.country,
                   profile_urls=list(person.profile_urls or []))
    session.add(other)
    session.flush()

    for authorship in session.execute(
        select(Authorship).where(
            Authorship.person_id == person_id,
            Authorship.publication_id.in_(list(wanted)),
        )
    ).scalars().all():
        authorship.person_id = other.id

    # Affiliations follow the institutions named on the papers that moved, and
    # only leave the original if it does not also claim them from what stayed.
    by_work = {
        (external_id or "").rsplit("/", 1)[-1]
        for (external_id,) in session.execute(
            select(Publication.external_id).where(Publication.id.in_(list(wanted)))
        )
    }
    moved_names = _own_institutions(record, by_work)
    kept_names = _own_institutions(record, {
        (external_id or "").rsplit("/", 1)[-1]
        for (external_id,) in session.execute(
            select(Publication.external_id).where(Publication.id.in_(sorted(theirs - wanted)))
        )
    })
    affiliations = 0
    for affiliation in session.execute(
        select(Affiliation).where(Affiliation.person_id == person_id)
    ).scalars().all():
        organization = session.get(Organization, affiliation.organization_id)
        label = organization.name if organization else None
        if label in moved_names and label not in kept_names:
            affiliation.person_id = other.id
            affiliations += 1

    rewritten = _rewrite_subjects(session, person, record)
    rewritten += _rewrite_subjects(session, other, record)

    if record is not None:
        session.add(PersonSplit(
            source_record_id=record.id, from_person_id=person.id,
            to_person_id=other.id, publication_ids=sorted(wanted), note=note,
        ))
    session.commit()
    return SplitResult(
        from_person_id=person.id, to_person_id=other.id,
        papers_moved=len(wanted), evidence_rewritten=rewritten,
        affiliations_moved=affiliations,
        frozen_record_id=(record.id if record is not None else None),
    )


def was_split(session: Session, source_record_id: int) -> bool:
    """Has a person pulled this record apart? Then re-ingesting it would put
    them back together."""
    return session.execute(
        select(PersonSplit.id).where(PersonSplit.source_record_id == source_record_id).limit(1)
    ).scalar_one_or_none() is not None
