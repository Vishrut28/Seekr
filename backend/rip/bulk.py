"""Bulk streaming ingest from a JSONL dump.

Reads line by line (plain or gzip) so a multi-GB dump never lands in RAM,
commits every batch, and writes rejected lines to a sidecar file for retry.
Requires the blocked entity resolution in resolution.py — without blocking,
bulk ingest is quadratic.

Each line is either:
    {"external_id": "A123"}            -> fetched via the connector
    {"id": "https://openalex.org/A1"}  -> a raw source object, normalized offline
"""

import gzip
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .connectors import get_connector
from .ingest import ingest_profile

logger = logging.getLogger("rip.bulk")


@dataclass
class BulkResult:
    processed: int = 0
    ingested: int = 0
    failed: int = 0
    failure_file: str | None = None


def _open(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def _profile_for(connector, obj: dict):
    """Normalize a dump line offline when possible; fetch only if it's a bare id."""
    external_id = obj.get("external_id") or obj.get("id")
    if external_id is None:
        raise ValueError("line has neither 'external_id' nor 'id'")
    external_id = str(external_id).rsplit("/", 1)[-1]
    if set(obj) <= {"external_id", "id"}:
        return connector.fetch(external_id)  # bare identifier: network needed
    try:
        return connector.renormalize(external_id, obj)
    except (NotImplementedError, KeyError):
        return connector.fetch(external_id)


def bulk_ingest(
    session: Session,
    source: str,
    file_path: str,
    *,
    batch_size: int = 500,
    limit: int | None = None,
    enrich_chain: bool = False,
    progress=print,
) -> BulkResult:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(file_path)
    connector = get_connector(source)
    failures_path = path.with_suffix(path.suffix + ".failures.jsonl")
    result = BulkResult(failure_file=str(failures_path))
    failures = None

    try:
        with _open(path) as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                if limit is not None and result.processed >= limit:
                    break
                result.processed += 1
                try:
                    obj = json.loads(line)
                    profile = _profile_for(connector, obj)
                    ingest_profile(session, profile)
                    result.ingested += 1
                    if enrich_chain:
                        from .enrich import enrich

                        enrich(session, profile)
                except Exception as exc:
                    # a poisoned session fails every later row, so always roll back
                    session.rollback()
                    result.failed += 1
                    if failures is None:
                        failures = open(failures_path, "w", encoding="utf-8")
                    failures.write(
                        json.dumps({"line": line_no, "error": f"{type(exc).__name__}: {exc}",
                                    "raw": line[:2000]}) + "\n"
                    )
                    logger.warning("bulk line %s failed: %s", line_no, exc)
                if result.processed % batch_size == 0:
                    session.commit()
                    progress(
                        f"  {result.processed} processed "
                        f"({result.ingested} ingested, {result.failed} failed)"
                    )
        session.commit()
    finally:
        if failures is not None:
            failures.close()
    if result.failed == 0:
        result.failure_file = None
    return result
