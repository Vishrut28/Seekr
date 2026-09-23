"""When a bulk load stops being a bulk load.

`bulk-ingest` streams a dump that `harvest` wrote, and normalizes each row
without touching the network. If a row cannot be read that way it falls back
to fetching it — which is right for a dump of bare identifiers, and is also
what happens when a dump is simply the wrong shape. renormalize reads
raw["author"], so one missing key turns every line into a live request.

Nothing said so. Same command, same progress line, same summary, at a few
rows a second instead of a few hundred: a million-row load becomes fifty
hours of requests against a public API that did not ask to be crawled, and
the only symptom is that it is slow. Writing a benchmark with bare author
rows instead of harvest's {id, author, works} sent two hundred of them before
the slowness gave it away.

So the fallback is now counted on the result, warned about the first time it
happens, reported at the end, and refusable with --offline. Nothing about
when it fires has changed — a dump of bare identifiers is still fetched,
because that is what it is for.
"""

import json

import pytest
from rip.bulk import bulk_ingest
from rip.normalize import NormalizedProfile


class Connector:
    """Renormalizes a harvest-shaped row; anything else has to be fetched."""

    source = "openalex"

    def __init__(self):
        self.fetched = []

    def renormalize(self, external_id, raw):
        author = raw["author"]          # the KeyError a wrong-shaped dump hits
        return self._profile(external_id, author.get("display_name"))

    def fetch(self, external_id):
        self.fetched.append(external_id)
        return self._profile(external_id, f"Fetched {external_id}")

    def _profile(self, external_id, name):
        return NormalizedProfile(
            source="openalex", source_type="scholarly", external_id=external_id,
            url=f"https://openalex.org/{external_id}", raw={}, name=name,
            usernames=[f"openalex:{external_id}"])


@pytest.fixture()
def connector(monkeypatch):
    fake = Connector()
    monkeypatch.setattr("rip.bulk.get_connector", lambda _s: fake)
    return fake


def dump(tmp_path, rows):
    path = tmp_path / "authors.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def harvest_row(i):
    """What harvest.py actually writes."""
    return {"id": f"A{i}", "works": [],
            "author": {"id": f"https://openalex.org/A{i}",
                       "display_name": f"Ada Number{i}"}}


def wrong_shape(i):
    """A bare author record — plausible, wrong, and silently expensive."""
    return {"id": f"https://openalex.org/A{i}", "display_name": f"Ada Number{i}"}


def bare_id(i):
    return {"external_id": f"A{i}"}


# --------------------------------------------------------------------------
# the normal case must not have changed
# --------------------------------------------------------------------------

def test_a_harvest_shaped_dump_touches_nothing(tmp_path, session, connector):
    result = bulk_ingest(session, "openalex", dump(tmp_path, [harvest_row(i)
                                                              for i in range(5)]),
                         progress=lambda *_a: None)
    assert result.ingested == 5
    assert result.fetched == 0
    assert connector.fetched == [], "a readable dump reached the network"


def test_a_dump_of_bare_identifiers_is_still_fetched(tmp_path, session, connector):
    """That is what a bare-identifier dump is FOR — harvest writes them for
    GitHub. Counting it must not turn it into an error."""
    result = bulk_ingest(session, "openalex", dump(tmp_path, [bare_id(i)
                                                              for i in range(3)]),
                         progress=lambda *_a: None)
    assert result.ingested == 3
    assert result.fetched == 3
    assert connector.fetched == ["A0", "A1", "A2"]


# --------------------------------------------------------------------------
# the case that was invisible
# --------------------------------------------------------------------------

def test_a_wrong_shaped_dump_is_counted_not_just_slow(tmp_path, session, connector):
    """The summary has to be able to tell an operator this happened."""
    result = bulk_ingest(session, "openalex", dump(tmp_path, [wrong_shape(i)
                                                              for i in range(6)]),
                         progress=lambda *_a: None)
    assert result.processed == 6
    assert result.fetched == 6, "the fallback is still silent"
    assert len(connector.fetched) == 6


def test_it_says_so_the_first_time(tmp_path, session, connector, caplog):
    """Once, not per row: a warning on every line of a million-row dump is
    the same as no warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="rip.bulk"):
        bulk_ingest(session, "openalex", dump(tmp_path, [wrong_shape(i)
                                                         for i in range(6)]),
                    progress=lambda *_a: None)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "per row" in warnings[0].getMessage().lower()


def test_offline_refuses_rather_than_crawling(tmp_path, session, connector):
    """The operator who knows their dump is complete can say so, and find out
    immediately instead of overnight."""
    result = bulk_ingest(session, "openalex",
                         dump(tmp_path, [wrong_shape(i) for i in range(4)]),
                         offline=True, progress=lambda *_a: None)
    assert connector.fetched == [], "--offline still reached the network"
    assert result.ingested == 0
    assert result.failed == 4
    assert result.fetched == 0


def test_offline_does_not_break_a_dump_that_needs_nothing(tmp_path, session, connector):
    result = bulk_ingest(session, "openalex",
                         dump(tmp_path, [harvest_row(i) for i in range(4)]),
                         offline=True, progress=lambda *_a: None)
    assert result.ingested == 4
    assert connector.fetched == []
