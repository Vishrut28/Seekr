"""Rows nothing can reach, and rows pointing at nothing.

    python scripts/purge_orphans.py           # report
    python scripts/purge_orphans.py --fix     # delete the unreachable ones

purge_person.py removes a record that is not a person. This removes what is
left when there is no person at all, which the corpus turned out to contain:

  source_record 358   semanticscholar:150034040, named "ARTERIAl STIffnESS" --
                      a section heading lifted out of a mangled PDF, not an
                      author. It holds no identity link, so no person claims
                      it, and its one publication had no authorship row.
  source_record 32    huggingface:Rahul-0, no publications and no link either.

Both predate every backup that contains them and nothing in the tree today
deletes an identity link, so their cause is not identified -- most likely an
ad-hoc cleanup in an earlier session that removed people and left their
records. Worth knowing rather than guessing at: this script exists so the
NEXT one is found by looking rather than by accident.

TWO KINDS, AND ONLY ONE IS SAFE TO DELETE

  unreachable   a source record no identity link names, or a publication no
                authorship names. Dead weight: nothing in the product can
                reach it, no search can return it. --fix removes these.
  dangling      a foreign key naming a row that is not there. That is
                corruption, not litter, and what it should become depends on
                what was lost -- so it is REPORTED AND NEVER TOUCHED. Deleting
                the row that points would finish destroying the evidence.

A publication is only removed if no authorship names it AND its source record
is going too, because a publication whose record is alive is one a refresh can
legitimately re-attach an author to.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def find(session) -> dict:
    """Read-only. Returns the two kinds, separately."""
    from sqlalchemy import select

    from rip.models import Authorship, IdentityLink, Publication, SourceRecord
    from scripts.purge_person import referencing_columns

    records = list(session.execute(
        select(SourceRecord.id).where(
            ~SourceRecord.id.in_(select(IdentityLink.source_record_id)))
    ).scalars())
    publications = list(session.execute(
        select(Publication.id).where(
            ~Publication.id.in_(select(Authorship.publication_id)),
            Publication.source_record_id.in_(records))
    ).scalars())
    # a publication nothing authors whose record is still alive: not litter,
    # because a refresh of that record can still give it an author
    attached = list(session.execute(
        select(Publication.id).where(
            ~Publication.id.in_(select(Authorship.publication_id)),
            Publication.id.notin_(publications) if publications else True)
    ).scalars())

    dangling = {}
    for target, model_of_target in (("person", "person"), ("publication", "publication"),
                                    ("source_record", "source_record")):
        from rip.db import Base

        table = next(m.class_ for m in Base.registry.mappers
                     if m.persist_selectable.name == model_of_target)
        key = table.__mapper__.primary_key[0]
        for model, column in referencing_columns(target):
            n = session.execute(
                select(column).where(column.isnot(None),
                                     column.notin_(select(key)))
            ).scalars().all()
            if n:
                dangling[f"{model.__tablename__}.{column.name}"] = n
    return {"records": records, "publications": publications,
            "publications_kept": attached, "dangling": dangling}


def purge(session, found: dict) -> None:
    from sqlalchemy import delete

    from rip.models import Publication, SourceRecord
    from scripts.purge_person import referencing_columns

    for model, column in referencing_columns("publication"):
        if found["publications"]:
            session.execute(delete(model).where(column.in_(found["publications"])))
    session.flush()
    if found["publications"]:
        session.execute(delete(Publication)
                        .where(Publication.id.in_(found["publications"])))
    session.flush()
    for model, column in referencing_columns("source_record"):
        if model is Publication and column.name == "source_record_id":
            continue          # any publication left is one we chose to keep
        if found["records"]:
            session.execute(delete(model).where(column.in_(found["records"])))
    session.flush()
    if found["records"]:
        session.execute(delete(SourceRecord)
                        .where(SourceRecord.id.in_(found["records"])))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true",
                        help="delete the unreachable rows (never the dangling ones)")
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db
    from rip.models import Publication, SourceRecord

    init_db()
    with SessionLocal() as session:
        found = find(session)

        print(f"source records no identity link names : {len(found['records'])}")
        for record_id in found["records"]:
            record = session.get(SourceRecord, record_id)
            print(f"  {record_id:<6} {record.source}:{record.external_id}")
        print(f"publications going with them          : {len(found['publications'])}")
        for publication_id in found["publications"]:
            pub = session.get(Publication, publication_id)
            print(f"  {publication_id:<6} {(pub.title or '')[:62]}")
        if found["publications_kept"]:
            print(f"publications with no author whose record is ALIVE, kept "
                  f"({len(found['publications_kept'])}): "
                  f"{found['publications_kept'][:8]}")

        if found["dangling"]:
            print("\nDANGLING foreign keys -- reported, never deleted:")
            for where, ids in sorted(found["dangling"].items()):
                print(f"  {len(ids):>5}  {where}  e.g. {ids[:3]}")
            print("  These name rows that are not there. What they should become "
                  "depends on\n  what was lost, so deciding is a person's job.")
        else:
            print("\nno dangling foreign keys")

        if not (found["records"] or found["publications"]):
            print("\nnothing unreachable to remove")
            return
        if not args.fix:
            print("\nreport only; pass --fix to delete the unreachable rows")
            return
        purge(session, found)
        print("\npurged")


if __name__ == "__main__":
    main()
