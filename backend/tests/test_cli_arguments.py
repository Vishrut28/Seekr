"""The operator interface's numbers, none of which had a lower bound.

`rip reindex --batch -1` deleted both index tables, indexed nobody, returned
0, and printed "indexed 0 people". That is a success message for a corpus
that no longer answers a query — and it is printed to the person who ran
`reindex` precisely because they were trying to repair the index. `--batch 0`
emptied the tables and then raised out of `range()`, with the delete already
committed. Neither could be undone by re-running the command, because the
command was the thing that broke it.

It was the same omission that let `limit=-1` return the whole table over
HTTP: an upper bound written, a lower bound not. argparse has no `ge=`, so
the fix is type functions, applied to every count, threshold, interval and
port in the file.

The parser is 170 statements of configuration that nothing executed. It is
tested here by DRIVING it — building the real parser and parsing real
argv — rather than by reading the options back, because a test that asks
argparse what it was configured with agrees with any configuration.
"""

import argparse
import contextlib
import io

import pytest
from rip.search_index import SearchDoc, SearchTerm, rebuild

from rip import cli


def parse(argv):
    """Run the real main() on real argv, and let whatever it raises out.

    load_env is stubbed: main() calls it first, and a test that reads the
    developer's own .env is a test whose result depends on whose machine it
    runs on."""
    import sys

    with contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        patch.setattr(cli, "load_env", lambda *_a, **_k: 0)
        patch.setattr(sys, "argv", ["rip", *argv])
        cli.main()


# --------------------------------------------------------------------------
# the type functions, directly
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["0", "-1", "-1000"])
def test_a_count_of_zero_or_less_is_refused(text):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.positive_int(text)


def test_a_count_of_one_is_allowed():
    assert cli.positive_int("1") == 1


@pytest.mark.parametrize("text", ["-1", "-7"])
def test_a_threshold_may_be_zero_but_not_negative(text):
    assert cli.counting_int("0") == 0
    with pytest.raises(argparse.ArgumentTypeError):
        cli.counting_int(text)


@pytest.mark.parametrize("text", ["0", "-0.5"])
def test_an_interval_must_be_greater_than_zero(text):
    """A poll interval of 0 is a hot loop against the database, not a fast
    worker."""
    with pytest.raises(argparse.ArgumentTypeError):
        cli.positive_float(text)


@pytest.mark.parametrize("text", ["0", "-80", "65536", "99999"])
def test_a_port_outside_the_range_is_refused(text):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.port_number(text)


def test_the_usual_ports_are_allowed():
    assert cli.port_number("8000") == 8000
    assert cli.port_number("65535") == 65535


# --------------------------------------------------------------------------
# every numeric option, through the real parser
# --------------------------------------------------------------------------

# (argv that should be refused) — one per bounded option in the file, so a
# new option added without a bound is a missing row here rather than a
# silent gap.
REFUSED = [
    ["refresh", "--processes", "0"],
    ["refresh", "--older-than-hours", "0"],
    ["discover", "--max-repos", "0"],
    ["ingest-leads", "--limit", "-1"],
    ["ingest-leads", "--processes", "0"],
    ["review", "approve", "-3"],
    ["harvest", "openalex", "--out", "x.jsonl", "--limit", "-1"],
    ["bulk-ingest", "github", "--file", "x.jsonl", "--batch-size", "0"],
    ["bulk-ingest", "github", "--file", "x.jsonl", "--limit", "-5"],
    ["find-homepages", "--limit", "0"],
    ["find-homepages", "--candidates", "0"],
    ["find-homepages", "--min-sources", "-1"],
    ["deliver-webhooks", "--limit", "0"],
    ["deliver-webhooks", "--fail-threshold", "-1"],
    ["queue-stats", "--batch-size", "0"],
    ["reindex", "--batch", "-1"],
    ["reindex", "--batch", "0"],
    ["worker", "--limit", "0"],
    ["worker", "--processes", "0"],
    ["worker", "--poll-interval", "0"],
    ["serve", "--port", "0"],
    ["serve", "--port", "70000"],
    ["serve", "--workers", "0"],
]


@pytest.mark.parametrize("argv", REFUSED, ids=lambda a: " ".join(a))
def test_the_parser_refuses_a_number_that_cannot_mean_anything(argv):
    """argparse exits 2 on a bad argument, so nothing downstream ever runs."""
    with pytest.raises(SystemExit) as exit_info:
        parse(argv)
    assert exit_info.value.code == 2, argv


def test_no_numeric_option_is_left_unbounded():
    """The rows above are a list someone has to remember to extend. This is
    the check that notices when they did not: every `type=int`/`type=float`
    in the file must now go through one of the bounded type functions."""
    import pathlib
    import re

    source = (pathlib.Path(cli.__file__)).read_text(encoding="utf-8")
    bare = re.findall(r"add_argument\([^)]*?type=(?:int|float)[,)]", source, re.S)
    assert not bare, bare


# --------------------------------------------------------------------------
# what the bound was protecting
# --------------------------------------------------------------------------

@pytest.fixture
def indexed(session):
    from rip.ingest import ingest_profile
    from rip.normalize import EvidenceItem
    from tests.test_resolution import make_profile

    for i in range(3):
        ingest_profile(session, make_profile(
            external_id=f"u{i}", name=f"Person {i}",
            usernames=[f"github:u{i}"], raw={"login": f"u{i}"},
            evidence=[EvidenceItem(attribute_type="skill", value="Rust",
                                   confidence=0.7)]))
    session.commit()
    rebuild(session, batch=100, progress=lambda _d, _t: None)
    assert session.query(SearchTerm).count() > 0
    return session


@pytest.mark.parametrize("batch", [0, -1, -1000])
def test_a_bad_batch_is_refused_before_anything_is_deleted(indexed, batch):
    """The order matters more than the refusal: rebuild() used to empty both
    tables first and discover the problem afterwards, so the index was gone
    either way."""
    terms = indexed.query(SearchTerm).count()
    docs = indexed.query(SearchDoc).count()

    with pytest.raises(ValueError, match="at least 1"):
        rebuild(indexed, batch=batch, progress=lambda _d, _t: None)

    indexed.rollback()
    assert indexed.query(SearchTerm).count() == terms, "index deleted anyway"
    assert indexed.query(SearchDoc).count() == docs


def test_a_good_batch_still_rebuilds(indexed):
    """The guard must not be reachable by an ordinary call."""
    indexed.query(SearchTerm).delete()
    indexed.commit()
    assert rebuild(indexed, batch=1, progress=lambda _d, _t: None) > 0
    assert indexed.query(SearchTerm).count() > 0
