"""The operator commands no test had run.

rip/cli.py was 37% covered on 2026-09-30: of its commands, ingest, reparse,
review, harvest, bulk-ingest, find-homepages, deliver-webhooks, queue-stats,
dedupe, reindex and audit-protected had never executed under a test, and
several had been edited since by a type-checking pass that nothing then ran.
These drive each one the way a person does -- through its arguments -- with
every network call faked and the database the test's own.
"""

import json
import types

import pytest

from rip import cli
from rip.models import DiscoveryLead, Evidence, Person, SourceRecord
from tests.test_foreign_work import career, openalex_person
from tests.test_resolution import make_profile


def args(**kw):
    return types.SimpleNamespace(**kw)


@pytest.fixture
def db(session, monkeypatch):
    """Every command's session is the test's; nothing touches a real database."""
    from rip import db as db_module

    monkeypatch.setattr(cli, "SessionLocal", lambda: session)
    monkeypatch.setattr(db_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(cli, "init_db", lambda *_a, **_k: None)
    return session


class FakeConnector:
    source = "github"

    def __init__(self, profile):
        self.profile = profile

    def fetch(self, identifier):
        return self.profile


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------

def test_ingest_stores_the_person_and_says_who(db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_connector",
                        lambda source: FakeConnector(make_profile(name="Ada Lovelace")))
    cli.cmd_ingest(args(source="github", identifier="ada", no_enrich=True))
    out = capsys.readouterr().out
    person = db.query(Person).one()
    assert f"ingested -> person {person.id} (Ada Lovelace)" in out


def test_ingest_that_stores_nobody_says_so_rather_than_crashing(db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_connector", lambda source: object())
    monkeypatch.setattr(cli, "run_connector", lambda *a, **k: None)
    cli.cmd_ingest(args(source="github", identifier="ada", no_enrich=True))
    assert "skipped: nothing ingested" in capsys.readouterr().out


# --------------------------------------------------------------------------
# reparse: rebuilds people from stored payloads, no network
# --------------------------------------------------------------------------

def test_reparse_rebuilds_from_the_stored_payload(db, capsys):
    openalex_person(db, "rp", "Ada Reparse", career(3))
    cli.cmd_reparse(args(source="openalex", verbose=False))
    assert "reparsed 1, skipped 0 (no stored raw), failed 0" in capsys.readouterr().out


def test_reparse_reports_a_payload_it_cannot_read_and_fails(db, capsys):
    openalex_person(db, "rp", "Ada Reparse", career(3))
    record = db.query(SourceRecord).one()
    record.raw = {"not": "an openalex payload"}
    db.commit()
    with pytest.raises(SystemExit) as stopped:
        cli.cmd_reparse(args(source="openalex", verbose=False))
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert "failed 1" in captured.out
    assert "FAILED openalex:" in captured.err


# --------------------------------------------------------------------------
# audit-protected: it rewrites stored text
# --------------------------------------------------------------------------

def _bio(db, text):
    db.add(Person(id="p1", canonical_name="Sam Coder"))
    db.flush()
    db.add(Evidence(person_id="p1", attribute_type="bio", value=text, source="github"))
    db.commit()


def test_audit_reports_protected_text_and_waits_to_be_told_twice(db, capsys):
    _bio(db, "Backend engineer. Proud to be gay. Rust and Go.")
    cli.cmd_audit_protected(args(yes=False))
    out = capsys.readouterr().out
    assert "1 carrying protected attributes" in out
    assert "sexual_orientation" in out
    assert "re-run with --yes" in out
    assert "gay" in db.query(Evidence).one().value, "rewritten without being asked twice"


def test_audit_with_yes_redacts_and_keeps_the_rest(db, capsys):
    _bio(db, "Backend engineer. Proud to be gay. Rust and Go.")
    cli.cmd_audit_protected(args(yes=True))
    assert "redacted 1 values" in capsys.readouterr().out
    value = db.query(Evidence).one().value
    assert "gay" not in value
    assert "Backend engineer" in value and "Rust and Go" in value


# --------------------------------------------------------------------------
# review, dedupe: say what a merge can and cannot take back
# --------------------------------------------------------------------------

def test_review_list_prints_the_queue_as_json(db, capsys):
    cli.cmd_review(args(action="list", yes=False, link_id=None))
    queue = json.loads(capsys.readouterr().out)
    assert queue["possible_duplicates"] == []


def test_review_triage_on_an_empty_queue_writes_nothing(db, capsys):
    cli.cmd_review(args(action="triage", yes=False, link_id=None))
    out = capsys.readouterr().out
    assert "0 pairs were waiting for a human" in out
    assert "nothing written" in out


def test_dedupe_with_nothing_to_merge_says_so(db, capsys):
    db.add_all([Person(id="a", canonical_name="Ada One"), Person(id="b", canonical_name="Bob Two")])
    db.commit()
    cli.cmd_dedupe(args(yes=False))
    out = capsys.readouterr().out
    assert "0 proven duplicates would be merged" in out


def test_no_command_promises_that_a_merge_is_undone():
    """The Review page says a merge cannot be undone, and it cannot: splitting
    a source record back out makes a NEW person, and the old id stays a
    tombstone. The commands said "every merge is reversible"."""
    import inspect

    src = inspect.getsource(cli)
    assert "every merge is reversible" not in src


# --------------------------------------------------------------------------
# reindex, queue-stats
# --------------------------------------------------------------------------

def test_reindex_indexes_everyone_and_refreshes_name_keys(db, capsys):
    db.add_all([Person(id="a", canonical_name="Ada One"), Person(id="b", canonical_name="Bob Two")])
    db.commit()
    cli.cmd_reindex(args(batch=100))
    out = capsys.readouterr().out
    assert "indexed 2 people" in out
    assert "name blocking keys refreshed for 2 people" in out


def test_queue_stats_counts_the_backlog_by_source(db, capsys):
    for i in range(3):
        db.add(DiscoveryLead(source="github", identifier=f"u{i}", status="pending"))
    db.add(DiscoveryLead(source="orcid", identifier="o1", status="pending"))
    db.add(DiscoveryLead(source="orcid", identifier="o2", status="ingested"))
    db.commit()
    cli.cmd_queue_stats(args(batch_size=2))
    out = capsys.readouterr().out
    assert "github" in out and "TOTAL" in out
    assert "ingested: 1" in out
    assert "at 2/run: 2 runs" in out


# --------------------------------------------------------------------------
# harvest, bulk-ingest
# --------------------------------------------------------------------------

def test_harvest_india_adds_the_country_to_any_filter(monkeypatch, capsys):
    seen = []

    def fake(out, *, limit, filter_expr, cursor):
        seen.append(filter_expr)
        return types.SimpleNamespace(next_cursor="c9")

    monkeypatch.setattr("rip.harvest.harvest_openalex", fake)
    base = {"source": "openalex", "out": "x.jsonl", "limit": 10, "cursor": "*"}
    cli.cmd_harvest(args(**base, filter=None, india=True))
    cli.cmd_harvest(args(**base, filter="works_count:>50", india=True))
    cli.cmd_harvest(args(**base, filter="works_count:>50", india=False))
    assert seen == ["last_known_institutions.country_code:IN",
                    "works_count:>50,last_known_institutions.country_code:IN",
                    "works_count:>50"]
    assert "resume with --cursor): c9" in capsys.readouterr().out


def test_harvest_refuses_what_it_does_not_do():
    with pytest.raises(SystemExit):
        cli.cmd_harvest(args(source="github", out="x", limit=1, india=False))
    with pytest.raises(SystemExit):
        cli.cmd_harvest(args(source="dblp", out="x", limit=1, india=False))


def test_bulk_ingest_offline_reads_the_file_it_is_given(db, tmp_path, capsys):
    rows = [{"id": "A1", "author": {"id": "https://openalex.org/A1", "display_name": "Ada Bulk"},
             "works": []}]
    path = tmp_path / "authors.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    cli.cmd_bulk_ingest(args(source="openalex", file=str(path), batch_size=10, limit=None,
                             no_enrich=True, offline=True))
    assert "processed 1, ingested 1, failed 0" in capsys.readouterr().out
    assert db.query(Person).filter_by(canonical_name="Ada Bulk").count() == 1


# --------------------------------------------------------------------------
# find-homepages, deliver-webhooks
# --------------------------------------------------------------------------

def test_find_homepages_needs_a_search_key(db, monkeypatch):
    monkeypatch.setattr("rip.websearch.available_backends", lambda: [])
    with pytest.raises(SystemExit, match="no search backend configured"):
        cli.cmd_find_homepages(args(limit=10, min_sources=1, candidates=3))


def test_find_homepages_ingests_the_first_page_that_reads(db, monkeypatch, capsys):
    db.add(Person(id="p1", canonical_name="Ada Lovelace", current_organization="Analytical"))
    db.commit()
    asked, fetched = [], []
    monkeypatch.setattr("rip.websearch.available_backends", lambda: ["tinyfish"])
    monkeypatch.setattr("rip.websearch.find_homepage",
                        lambda name, org, limit: asked.append((name, org)) or [
                            {"url": "https://broken.example"}, {"url": "https://ada.example"}])
    monkeypatch.setattr(cli, "get_connector", lambda source: object())

    def fake_run(session, connector, url, enrich_chain):
        fetched.append(url)
        if "broken" in url:
            raise RuntimeError("robots.txt disallows fetching it")

    monkeypatch.setattr("rip.ingest.run_connector", fake_run)
    cli.cmd_find_homepages(args(limit=10, min_sources=1, candidates=3))
    out = capsys.readouterr().out
    assert asked == [("Ada Lovelace", "Analytical")]
    assert fetched == ["https://broken.example", "https://ada.example"]
    assert "searched 1, candidates for 1, ingested 1" in out


def test_deliver_webhooks_fails_the_run_past_its_threshold(db, monkeypatch, capsys):
    monkeypatch.setattr("rip.webhooks.deliver_pending", lambda session, limit: (2, 5))
    with pytest.raises(SystemExit) as stopped:
        cli.cmd_deliver_webhooks(args(limit=50, fail_threshold=3))
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert "delivered 2, failed 5" in captured.out
    assert "5 failures exceed threshold 3" in captured.err

    monkeypatch.setattr("rip.webhooks.deliver_pending", lambda session, limit: (2, 1))
    cli.cmd_deliver_webhooks(args(limit=50, fail_threshold=3))     # under it: no exit
