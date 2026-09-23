"""How many processes `serve` is allowed to start, which decided the capacity
of the main feature without anybody choosing it.

/v1/query is CPU-bound Python. Measured over HTTP against the real corpus,
with live discovery off, at eight concurrent callers:

    one worker     9.2 req/s    p95 1,406 ms
    four workers  49.4 req/s    p95   281 ms

Threads inside one process do not help — the work is under the GIL, and
throughput FELL as concurrency rose (13.7 req/s serial, 7.8 at sixteen-way).
Processes are the only lever, and `serve` was refusing them on every SQLite
database with "SQLite does not take concurrent writers".

That sentence is true and it was being applied to the wrong set. SQLite takes
one writer and as many readers as you like. The deployed snapshot is opened
`mode=ro`, where there is no writer to serialise against, so the clamp was
costing 5.4x on precisely the deployment that could never have hit the
problem it guards. A writable file still gets one worker, because live
discovery stores people and two processes writing one SQLite file is the
"database is locked" this was written to prevent.
"""

import io
from contextlib import redirect_stdout

import pytest

from rip import cli


class Args:
    host = "127.0.0.1"
    port = 8000

    def __init__(self, workers):
        self.workers = workers


@pytest.fixture()
def served(monkeypatch):
    """Runs cmd_serve up to the point it would hand over to uvicorn, and
    reports the worker count it decided on plus what it told the operator."""
    def run(url, *, read_only, workers):
        started = {}
        monkeypatch.setattr("rip.db.DB_URL", url, raising=False)
        monkeypatch.setattr("rip.db.READ_ONLY", read_only, raising=False)
        monkeypatch.setattr("uvicorn.run",
                            lambda *a, **kw: started.update(kw), raising=False)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_serve(Args(workers))
        return started.get("workers"), out.getvalue()

    return run


def test_a_writable_sqlite_still_gets_one_worker(served):
    """Unchanged, and the reason is unchanged: live discovery writes, and two
    processes writing one SQLite file is 'database is locked'."""
    workers, said = served("sqlite:///rip.db", read_only=False, workers=4)
    assert workers == 1
    assert "concurrent writers" in said


def test_a_read_only_snapshot_may_use_every_worker_asked_for(served):
    """The 5.4x. Nothing can write to it, so there is no writer to serialise
    against."""
    workers, said = served("sqlite:///file:snap.db?mode=ro&uri=true",
                           read_only=True, workers=4)
    assert workers == 4
    assert "read-only" in said


def test_postgres_is_never_clamped(served):
    workers, _said = served("postgresql+psycopg://u:p@h/db", read_only=False, workers=4)
    assert workers == 4


@pytest.mark.parametrize("read_only", [True, False])
def test_one_worker_is_one_worker_either_way(served, read_only):
    """The default must not change shape: asking for one gets one, with no
    warning printed at somebody who asked for nothing unusual."""
    workers, said = served("sqlite:///rip.db", read_only=read_only, workers=1)
    assert workers == 1
    assert "concurrent writers" not in said


def test_the_operator_is_told_what_it_decided(served):
    """Either way the startup banner has to say the number, because it is not
    always the number they asked for."""
    _w, clamped = served("sqlite:///rip.db", read_only=False, workers=8)
    _w2, allowed = served("sqlite:///file:snap.db?mode=ro&uri=true",
                          read_only=True, workers=8)
    assert "one worker" in clamped
    assert "8" in allowed
