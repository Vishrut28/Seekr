"""Phase 5: bulk streaming ingest."""

import gzip
import json

import pytest

from rip.bulk import bulk_ingest
from rip.models import Person, SourceRecord


def _author(i: int) -> dict:
    return {
        "author": {
            "id": f"https://openalex.org/A{i}",
            "display_name": f"Author {i}",
            "display_name_alternatives": [],
            "last_known_institutions": [{"display_name": "Bulk University", "type": "education"}],
            "topics": [{"display_name": "Topic X", "count": 3}],
        },
        "works": [],
        "id": f"A{i}",
    }


@pytest.fixture()
def dump(tmp_path):
    path = tmp_path / "authors.jsonl"
    with open(path, "w") as fh:
        for i in range(100):
            fh.write(json.dumps(_author(i)) + "\n")
    return path


def test_streams_all_lines(session, dump):
    result = bulk_ingest(session, "openalex", str(dump), batch_size=25, progress=lambda m: None)
    assert result.processed == 100
    assert result.ingested == 100
    assert result.failed == 0
    assert result.failure_file is None
    assert session.query(SourceRecord).count() == 100


def test_limit_stops_early(session, dump):
    result = bulk_ingest(session, "openalex", str(dump), limit=10, progress=lambda m: None)
    assert result.processed == 10 and result.ingested == 10


def test_rerun_is_idempotent(session, dump):
    bulk_ingest(session, "openalex", str(dump), limit=20, progress=lambda m: None)
    persons_before = session.query(Person).count()
    records_before = session.query(SourceRecord).count()
    bulk_ingest(session, "openalex", str(dump), limit=20, progress=lambda m: None)
    assert session.query(Person).count() == persons_before
    assert session.query(SourceRecord).count() == records_before


def test_bad_line_does_not_poison_the_rest(session, tmp_path):
    """Regression: a failed row must roll back so later rows still ingest."""
    path = tmp_path / "mixed.jsonl"
    with open(path, "w") as fh:
        fh.write(json.dumps(_author(1)) + "\n")
        fh.write("{ this is not json\n")
        fh.write(json.dumps({"nothing": "useful"}) + "\n")
        fh.write(json.dumps(_author(2)) + "\n")
    result = bulk_ingest(session, "openalex", str(path), progress=lambda m: None)
    assert result.processed == 4
    assert result.ingested == 2  # both good rows survived the bad ones
    assert result.failed == 2
    assert result.failure_file
    with open(result.failure_file) as fh:
        failures = [json.loads(line) for line in fh]
    assert {f["line"] for f in failures} == {2, 3}


def test_gzip_supported(session, tmp_path):
    path = tmp_path / "authors.jsonl.gz"
    with gzip.open(path, "wt") as fh:
        for i in range(5):
            fh.write(json.dumps(_author(i)) + "\n")
    result = bulk_ingest(session, "openalex", str(path), progress=lambda m: None)
    assert result.ingested == 5


def test_blank_lines_skipped(session, tmp_path):
    path = tmp_path / "sparse.jsonl"
    with open(path, "w") as fh:
        fh.write(json.dumps(_author(1)) + "\n\n   \n" + json.dumps(_author(2)) + "\n")
    result = bulk_ingest(session, "openalex", str(path), progress=lambda m: None)
    assert result.processed == 2 and result.failed == 0


def test_missing_file_raises(session):
    with pytest.raises(FileNotFoundError):
        bulk_ingest(session, "openalex", "/nonexistent/path.jsonl")
