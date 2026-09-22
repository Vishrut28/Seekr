"""Compress source payloads already stored, and report what it saved.

    python scripts/compress_payloads.py            # what it would save
    python scripts/compress_payloads.py --yes      # do it

Every source record keeps what the source actually sent, which is the point:
the payload is the evidence, and this project reaches for it constantly --
to settle whether a record is a person, what a source really said before a
keyword was repaired, which author id OpenAlex meant. It is also 89% of the
database, 203 MB of 228 MB for 767 people.

models.CompressedJSON compresses payloads on the way in from now on. This
rewrites the ones already there. Nothing is lost and nothing is converted:
reading is transparent either way, so a database half-done works exactly like
one fully done, and the only reason to run it is size.

Back up the database before running it with --yes. Afterwards run VACUUM to
return the freed pages to the filesystem -- SQLite keeps them otherwise.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("RIP_DATABASE_URL"))
    parser.add_argument("--yes", action="store_true", help="rewrite the rows")
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args()
    if args.db:
        os.environ["RIP_DATABASE_URL"] = args.db

    import json

    from rip.db import SessionLocal
    from rip.models import CompressedJSON, SourceRecord
    from sqlalchemy import func, select
    from sqlalchemy.orm.attributes import flag_modified

    coder = CompressedJSON()
    before = after = 0
    rewritten = already = 0
    with SessionLocal() as session:
        total = session.execute(select(func.count(SourceRecord.id))).scalar_one()
        ids = [i for (i,) in session.execute(select(SourceRecord.id))]
        for start in range(0, len(ids), args.batch):
            chunk = ids[start:start + args.batch]
            for record in session.execute(
                select(SourceRecord).where(SourceRecord.id.in_(chunk))
            ).scalars():
                payload = record.raw
                plain = len(json.dumps(payload, separators=(",", ":"),
                                       ensure_ascii=False))
                packed = coder.process_bind_param(payload, None)
                before += plain
                if isinstance(packed, dict) and CompressedJSON.KEY in packed:
                    after += len(packed[CompressedJSON.KEY])
                    rewritten += 1
                    if args.yes:
                        # The value does not change, only how it is stored, so
                        # assigning it back is not enough: SQLAlchemy compares
                        # equal and skips the UPDATE. The first run of this
                        # script "rewrote" 634 payloads and shrank the file by
                        # 2 MB of the 166 it had just reported.
                        flag_modified(record, "raw")
                else:
                    after += plain
                    already += 1
            if args.yes:
                session.commit()
            print(f"\r  {min(start + args.batch, len(ids))}/{total} records", end="")
    print()
    saved = before - after
    verb = "" if args.yes else " (dry run — pass --yes to apply)"
    print(f"\n{rewritten} payloads compress, {already} are already small enough")
    print(f"  {before / 1048576:.1f} MB -> {after / 1048576:.1f} MB"
          f"   saved {saved / 1048576:.1f} MB"
          f"   ({before / max(after, 1):.1f}x){verb}")
    if args.yes:
        print("\nRun VACUUM to return the freed pages to the filesystem.")


if __name__ == "__main__":
    main()
