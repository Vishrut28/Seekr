"""What the API does as it starts, now through FastAPI's lifespan handler.

@app.on_event("startup") was deprecated; the same work moved to a lifespan
function. These pin that it still happens: the schema is ensured, and an
open deployment is warned about.
"""

import logging

from fastapi.testclient import TestClient

from rip import api


def test_starting_the_app_ensures_the_schema_and_warns_when_open(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(api, "init_db", lambda: calls.append(1))
    monkeypatch.delenv("RIP_API_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING, logger="rip"), TestClient(api.app):
        pass
    assert calls == [1]
    assert any("RIP_API_TOKEN is not set" in r.message for r in caplog.records)


def test_a_database_that_will_not_initialise_does_not_stop_the_app(monkeypatch, caplog):
    """Read-only deployments serve a snapshot: routes fail one by one instead."""
    def refuse():
        raise OSError("read-only file system")

    monkeypatch.setattr(api, "init_db", refuse)
    monkeypatch.setenv("RIP_API_TOKEN", "t")
    with caplog.at_level(logging.ERROR, logger="rip"), TestClient(api.app) as client:
        assert client.get("/").status_code == 200
    assert any("init_db failed at startup" in r.message for r in caplog.records)
