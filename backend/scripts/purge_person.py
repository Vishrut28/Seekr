"""Remove a record that is not a person, and everything it alone brought in.

    python scripts/purge_person.py d6800ca5              # plan only
    python scripts/purge_person.py d6800ca5 --apply

Nothing else in this codebase deletes a person, on purpose: merging leaves a
tombstone so the id stays resolvable, and a real person removed by mistake is
silently lost. But personhood.assess is a gate on INGEST only, so a record
stored before a rule existed stays for ever. "Verlag Hans Huber" is a Swiss
medical publisher that OpenAlex credits as an author; it sat in the corpus
with 25 works and scored 0.40 on the conflation detector, which is what a
publisher's catalogue looks like from the inside.

THE SAFETY RULE, and it is the only reason this script is allowed to exist:
it refuses any record that personhood.assess still calls a person. So it can
only ever remove what the gate would now refuse at the door, and it cannot be
turned on somebody real -- not by a typo, and not by an id prefix matching
more than was meant. Rejecting the record first is the whole authorisation.

There is a second way in, and it is safe for the same reason: a record with no
name, no evidence and no publications. assess() cannot rule on one -- "no name
given" means there is nothing to JUDGE, not that nobody is there -- but a
record holding nothing cannot be a real person losing their data, because
there is no data. Europe PMC answers an ORCID search it holds nothing for with
an empty payload, and one of those became a living person with a null name.
Ingest now refuses it (rip.ingest.ingest_profile); this removes the ones
stored before that rule existed.

WHAT GOES, AND WHAT STAYS

  the person       and every row referencing it, found by walking the mapped
                   tables rather than by a hand-written list, because there
                   are eighteen of them and a missed one leaves a dangling id
  its records      the source records nobody else holds
  its publications only those that came from those records AND have no other
                   author. A book the publisher is credited on alongside a
                   real author belongs to the author: the publication stays
                   and only the publisher's authorship row goes.

A publication that stayed would otherwise point at a deleted source record,
which SQLite tolerates and Postgres does not -- both are supported, so the
sweep has to leave neither.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def is_empty(session, person) -> bool:
    """A record holding nothing at all: no name, no claim, no paper.

    Not a judgement about whether somebody is a person -- it is the absence of
    anybody to judge. Deleting it cannot cost a real person their record,
    because the record carries nothing of theirs.
    """
    from rip.models import Authorship, Evidence
    from sqlalchemy import func, select

    if (person.canonical_name or "").strip():
        return False
    for table in (Evidence, Authorship):
        if session.execute(select(func.count(table.id))
                           .where(table.person_id == person.id)).scalar_one():
            return False
    return True


def referencing_columns(target_table: str) -> list:
    """Every (mapped class, column) that is a foreign key to TARGET_TABLE.

    Derived from the metadata, not written down: person alone is referenced
    by eighteen tables and the list grows, and a purge that misses one leaves
    a row pointing at an id that is gone.
    """
    from rip.db import Base

    found = []
    for mapper in Base.registry.mappers:
        for column in mapper.persist_selectable.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == target_table:
                    found.append((mapper.class_, column))
    return found


def plan(session, person) -> dict:
    """What would go. Read-only."""
    from rip.models import Authorship, IdentityLink, Publication
    from sqlalchemy import func, select

    # Every record this person holds is theirs alone: identity_link is unique
    # on source_record_id, so one record can never be linked to two people.
    # (A person may hold several records -- the merged combinatorialist holds
    # three -- but not the other way round.) So there is no sharing to check
    # for here, and a check would be a branch that can never run.
    records = list(session.execute(
        select(IdentityLink.source_record_id)
        .where(IdentityLink.person_id == person.id).distinct()).scalars())

    # publications from those records that no one else authors
    publications, kept = [], []
    for (publication_id,) in session.execute(
            select(Authorship.publication_id)
            .where(Authorship.person_id == person.id).distinct()):
        pub = session.get(Publication, publication_id)
        others = session.execute(
            select(func.count()).select_from(Authorship).where(
                Authorship.publication_id == publication_id,
                Authorship.person_id != person.id)).scalar()
        if others == 0 and pub.source_record_id in records:
            publications.append(publication_id)
        else:
            kept.append((publication_id, others, pub.title))

    rows: dict = {}
    for table, ids in (("person", [person.id]), ("publication", publications),
                       ("source_record", records)):
        if not ids:
            continue
        for model, column in referencing_columns(table):
            if model is Publication and column.name == "source_record_id":
                continue          # those publications are being deleted anyway
            n = session.execute(select(func.count()).select_from(model)
                                .where(column.in_(ids))).scalar()
            if n:
                rows[f"{model.__tablename__}.{column.name}"] = n
    return {"records": records, "publications": publications,
            "kept_publications": kept, "rows": rows}


def purge(session, person, proposed: dict) -> None:
    from rip.models import Publication, SourceRecord
    from sqlalchemy import delete

    records, publications = proposed["records"], proposed["publications"]

    # children first, in the order the foreign keys point: rows that reference
    # a publication, then the publications; then rows that reference the
    # person, then the person; then the records nothing is left pointing at.
    for table, ids in (("publication", publications), ("person", [person.id])):
        if not ids:
            continue
        for model, column in referencing_columns(table):
            if model is Publication and column.name == "source_record_id":
                continue
            session.execute(delete(model).where(column.in_(ids)))
        session.flush()
        if table == "publication":
            session.execute(delete(Publication).where(Publication.id.in_(ids)))
            session.flush()

    session.delete(person)
    session.flush()
    for model, column in referencing_columns("source_record"):
        if model is Publication and column.name == "source_record_id":
            continue
        session.execute(delete(model).where(column.in_(records)))
    session.flush()
    session.execute(delete(SourceRecord).where(SourceRecord.id.in_(records)))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("person", help="person id, or its first characters")
    parser.add_argument("--apply", action="store_true", help="actually delete")
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db
    from rip.models import Person
    from rip.personhood import assess
    from sqlalchemy import select

    init_db()
    with SessionLocal() as session:
        people = session.execute(
            select(Person).where(Person.id.like(args.person + "%"))
        ).scalars().all()
        if not people:
            sys.exit(f"no person whose id starts with {args.person}")
        if len(people) > 1:
            sys.exit(f"{args.person} matches {len(people)} people; give more of the id")
        person = people[0]

        verdict = assess(person.canonical_name, "openalex")
        print(f"{person.id}  {person.canonical_name!r}")
        if verdict.is_person and not is_empty(session, person):
            sys.exit(
                "REFUSED: personhood still calls this a person, so this script "
                "will not touch it.\nIt removes only what ingest would now "
                "refuse at the door. If the record really is not a person, the "
                "fix is a rule in rip/personhood.py -- with a test -- and then "
                "this script follows from it.")
        reason = ("holds no name, no evidence and no publications"
                  if is_empty(session, person) else verdict.reason)
        print(f"not a person: {reason}\n")

        proposed = plan(session, person)
        print(f"source records to delete : {proposed['records']}")
        print(f"publications to delete   : {len(proposed['publications'])}")
        for publication_id, others, title in proposed["kept_publications"]:
            print(f"  KEEPING pub#{publication_id} ({others} other author(s)): "
                  f"{(title or '')[:56]}")
        print("rows to delete:")
        for where, n in sorted(proposed["rows"].items()):
            print(f"  {n:>5}  {where}")

        if not args.apply:
            print("\nplan only; pass --apply to delete")
            return
        purge(session, person, proposed)
        print("\npurged")


if __name__ == "__main__":
    main()
