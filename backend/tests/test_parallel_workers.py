"""Several workers draining one lead queue: each takes its own leads, a dead
worker's leads come back, and the fleet is exactly as polite to a source as a
single worker."""

import threading
import time
from datetime import datetime, timedelta, timezone
from itertools import pairwise

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from rip import discover
from rip.db import Base
from rip.models import DiscoveryLead, Person, SourceRecord
from rip.normalize import NormalizedProfile


@pytest.fixture()
def db_file(tmp_path):
    """A real file database with WAL, the way workers share one."""
    from sqlalchemy import event

    engines = []

    def make():
        engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}",
                               connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _pragmas(conn, _record):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")

        engines.append(engine)
        return sessionmaker(bind=engine, expire_on_commit=False)

    Base.metadata.create_all(make().kw["bind"])
    yield make
    for engine in engines:
        engine.dispose()


def add_leads(Session, n, source="openalex"):
    with Session() as s:
        for i in range(n):
            s.add(DiscoveryLead(source=source, identifier=f"A{i:04d}", status="pending"))
        s.commit()


def test_two_workers_never_hold_the_same_lead(db_file):
    add_leads(db_file(), 10)
    first, second = db_file()(), db_file()()
    a = {lead.id for lead in discover.claim_leads(first, "worker-a", 6)}
    b = {lead.id for lead in discover.claim_leads(second, "worker-b", 6)}
    assert len(a) == 6 and len(b) == 4 and not a & b


def test_workers_racing_for_the_queue_split_it_exactly(db_file):
    """Eight workers claiming at the same moment, each on its own connection."""
    add_leads(db_file(), 60)
    taken: dict[str, list[int]] = {}
    start = threading.Barrier(8)

    def work(name):
        session = db_file()()
        start.wait()
        taken[name] = [lead.id for lead in discover.claim_leads(session, name, 10)]
        session.close()

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    every = [lead_id for ids in taken.values() for lead_id in ids]
    assert len(every) == len(set(every))            # nobody twice
    assert len(every) == 60                          # and nothing left behind


def test_a_dead_workers_claims_go_back_to_the_queue(db_file):
    Session = db_file()
    add_leads(Session, 3)
    with Session() as s:
        discover.claim_leads(s, "crashed", 3)
        stale = datetime.now(timezone.utc).replace(tzinfo=None) \
            - timedelta(seconds=discover.CLAIM_TTL_SECONDS + 60)
        for lead in s.execute(select(DiscoveryLead)).scalars():
            lead.claimed_at = stale
        s.commit()
        assert [lead.claimed_by for lead in discover.claim_leads(s, "alive", 3)] == ["alive"] * 3


def test_a_live_workers_claims_are_left_alone(db_file):
    Session = db_file()
    add_leads(Session, 3)
    with Session() as s:
        discover.claim_leads(s, "slow-but-alive", 3)
        assert discover.claim_leads(s, "other", 3) == []


class FakeConnector:
    source = "openalex"

    def __init__(self, stop_on=None):
        self.stop_on = stop_on
        self.fetched = []

    def fetch(self, identifier):
        if identifier == self.stop_on:
            raise KeyboardInterrupt
        self.fetched.append(identifier)
        return NormalizedProfile(source="openalex", source_type="scholarly",
                                 external_id=identifier, url=f"https://openalex.org/{identifier}",
                                 raw={}, name=f"Person {identifier}")


def test_an_interrupted_batch_hands_its_remaining_leads_back(db_file, monkeypatch):
    Session = db_file()
    add_leads(Session, 4)
    fake = FakeConnector(stop_on="A0002")
    monkeypatch.setattr(discover, "get_connector", lambda source: fake)
    with Session() as s, pytest.raises(KeyboardInterrupt):
        discover.drain_leads(s, limit=4, worker="w")
    with Session() as s:
        status = dict(s.execute(select(DiscoveryLead.identifier, DiscoveryLead.status)).all())
    assert status == {"A0000": "ingested", "A0001": "ingested",
                      "A0002": "pending", "A0003": "pending"}


def test_a_write_that_collides_with_another_worker_is_retried(db_file, monkeypatch):
    """Two leads can be one person, drained by two workers at once: the
    second to commit hits the unique key the first just wrote."""
    Session = db_file()
    add_leads(Session, 1)
    attempts = []

    def run_connector(session, connector, identifier, enrich_chain=False):
        attempts.append(identifier)
        if len(attempts) == 1:
            raise IntegrityError("INSERT INTO person_key", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(discover, "get_connector", lambda source: FakeConnector())
    monkeypatch.setattr(discover, "run_connector", run_connector)
    with Session() as s:
        assert discover.drain_leads(s, limit=1, worker="w") == (1, 0)
    assert attempts == ["A0000", "A0000"]


def test_workers_draining_together_ingest_everyone_exactly_once(db_file, monkeypatch):
    add_leads(db_file(), 30)
    monkeypatch.setattr(discover, "get_connector", lambda source: FakeConnector())
    results = []

    def work(name):
        with db_file()() as session:
            results.append(discover.drain_leads(session, limit=12, worker=name))

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db_file()() as s:
        assert s.query(SourceRecord).count() == 30
        assert s.query(Person).count() == 30
        statuses = {st for (st,) in s.execute(select(DiscoveryLead.status)).all()}
    assert statuses == {"ingested"}
    assert sum(ok for ok, _ in results) == 30


def test_request_spacing_widens_with_the_number_of_worker_processes(monkeypatch):
    from rip.connectors.base import BaseConnector

    class Spaced(BaseConnector):
        source = "spaced"
        min_request_interval = 0.05

        def fetch(self, identifier):
            raise NotImplementedError

    def gaps(processes):
        monkeypatch.setenv("RIP_WORKER_PROCESSES", str(processes))
        conn = Spaced.__new__(Spaced)
        stamps = []
        for _ in range(3):
            conn._wait_for_slot()
            stamps.append(time.monotonic())
        return min(b - a for a, b in pairwise(stamps))

    assert gaps(1) >= 0.045
    assert gaps(4) >= 0.19                     # four processes, four times apart
    monkeypatch.setenv("RIP_WORKER_PROCESSES", "not a number")
    from rip.connectors.base import worker_processes
    assert worker_processes() == 1


def test_refresh_shards_cover_every_record_exactly_once(db_file, monkeypatch):
    """`refresh --processes 4`: every stale record refreshed by one child."""
    import argparse

    from rip import cli

    Session = db_file()
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)
    with Session() as s:
        for i in range(25):
            s.add(SourceRecord(source="openalex", source_type="scholarly",
                               external_id=f"A{i}", raw={}, last_observed=old))
        s.commit()
    refreshed = []
    monkeypatch.setattr(cli, "SessionLocal", Session)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "get_connector", lambda source: None)
    monkeypatch.setattr(cli, "run_connector",
                        lambda session, conn, ident, **kw: refreshed.append(ident))
    for index in range(4):
        cli.cmd_refresh(argparse.Namespace(older_than_hours=24.0, processes=1,
                                           shard=(index, 4)))
    assert sorted(refreshed) == sorted(f"A{i}" for i in range(25))
