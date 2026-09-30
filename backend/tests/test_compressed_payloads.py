"""Source payloads are stored compressed, and nothing above notices.

Every source record keeps what the source actually sent, and this project
reaches for it constantly -- to settle whether a record is a person, what a
source really said before a keyword was repaired, which author id OpenAlex
meant. It is the evidence and it stays. It is also 89% of the database,
203 MB of 228 MB for 767 people, and it compresses about six to one once
base64 has taken its third back.

The column type does not change: migrations here are additive only, and a
JSON column already holds text on both engines. So the only thing that has to
be true is that reading is transparent, including for rows written before
this existed.
"""

import json

from sqlalchemy import select

from rip.models import CompressedJSON, SourceRecord

BIG = {"works": [{"id": f"W{i}", "title": f"A study of things, number {i}",
                  "authorships": [{"author": {"id": "A1", "display_name": "Ada Lovelace"}}]}
                 for i in range(200)]}
SMALL = {"id": "A1", "display_name": "Ada Lovelace"}


def store(session, external_id, payload):
    record = SourceRecord(source="openalex", source_type="scholarly",
                          external_id=external_id, raw=payload)
    session.add(record)
    session.commit()
    session.expunge_all()
    return record.id


def stored_text(session, external_id):
    """What is actually on disk, bypassing the type."""
    return session.connection().exec_driver_sql(
        "select raw from source_record where external_id = ?", (external_id,)).scalar()


def test_a_large_payload_round_trips_through_compression(session):
    store(session, "big", BIG)
    back = session.execute(select(SourceRecord)
                           .where(SourceRecord.external_id == "big")).scalar_one()
    assert back.raw == BIG
    on_disk = stored_text(session, "big")
    assert CompressedJSON.KEY in on_disk
    assert len(on_disk) < len(json.dumps(BIG)) / 4


def test_a_small_payload_is_left_alone(session):
    """Below the threshold the wrapper costs more than it saves."""
    store(session, "small", SMALL)
    on_disk = stored_text(session, "small")
    assert CompressedJSON.KEY not in on_disk
    back = session.execute(select(SourceRecord)
                           .where(SourceRecord.external_id == "small")).scalar_one()
    assert back.raw == SMALL


def test_a_row_written_before_this_existed_still_reads(session):
    """There is no migration to run, so old and new must coexist for ever."""
    store(session, "legacy", SMALL)
    session.connection().exec_driver_sql(
        "update source_record set raw = ? where external_id = ?",
        (json.dumps(BIG), "legacy"))
    session.commit()
    back = session.execute(select(SourceRecord)
                           .where(SourceRecord.external_id == "legacy")).scalar_one()
    assert back.raw == BIG


def test_a_column_level_select_decodes_too(session):
    """conflation.coauthor_institutions reads SourceRecord.raw as a column,
    not through an object."""
    store(session, "big", BIG)
    rows = session.execute(select(SourceRecord.raw)).all()
    assert rows
    for (raw,) in rows:
        assert isinstance(raw, dict)
        assert CompressedJSON.KEY not in raw


def test_something_incompressible_is_stored_as_it_is(session):
    """Compression that makes a payload bigger is not compression."""
    import os

    noise = {"blob": os.urandom(4000).hex()}
    coder = CompressedJSON()
    packed = coder.process_bind_param(noise, None)
    if isinstance(packed, dict) and CompressedJSON.KEY in packed:
        assert len(packed[CompressedJSON.KEY]) < len(json.dumps(noise))
    assert coder.process_result_value(packed, None) == noise


def test_none_and_lists_survive(session):
    coder = CompressedJSON()
    assert coder.process_bind_param(None, None) is None
    assert coder.process_result_value(None, None) is None
    big_list = [{"id": i, "title": "a reasonably long title to compress"} for i in range(300)]
    assert coder.process_result_value(
        coder.process_bind_param(big_list, None), None) == big_list
