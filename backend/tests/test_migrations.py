"""An existing database gains the columns and indexes later versions rely on.

search_doc's publication/citation totals arrived with INDEX_VERSION 9, and
`index=True` on a model column creates nothing for a table that already
exists — a count filter with no subject would scan.
"""

from sqlalchemy import create_engine, inspect, text

from rip import db as db_module
from rip import (
    models,  # noqa: F401  registers the tables
    search_index,  # noqa: F401  registers search_doc / search_term
)


def test_an_older_search_doc_gains_the_totals_and_their_indexes(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    db_module.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # the shape before the totals existed
        conn.execute(text("DROP TABLE search_doc"))
        conn.execute(text(
            "CREATE TABLE search_doc (person_id VARCHAR(36) PRIMARY KEY, prior FLOAT, "
            "evidence_count INTEGER, source_count INTEGER, impact FLOAT)"))
        conn.execute(text("INSERT INTO search_doc VALUES ('p1', 0.5, 3, 1, 12.0)"))

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "_build_search_index", lambda: None)
    db_module._migrate()

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("search_doc")}
    assert {"publications", "citations"} <= columns
    names = {ix["name"] for ix in inspector.get_indexes("search_doc")}
    assert {"ix_search_doc_publications", "ix_search_doc_citations"} <= names
    # the row that was already there keeps its values and reads zero totals
    with engine.begin() as conn:
        assert conn.execute(text(
            "SELECT prior, publications, citations FROM search_doc")).one() == (0.5, 0, 0)
    engine.dispose()
