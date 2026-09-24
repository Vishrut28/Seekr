"""The dossier renderer, which nothing exercised at all.

rip/dossier.py was at 0% coverage — 109 statements, a user-facing page — and
that is exactly where its bug was able to live: it used backslashes inside
f-strings, which is Python 3.12+, while this project says it supports 3.10.
The module could not be imported on the version it claims to run on, and no
test noticed because no test touched it.

These are not thorough. They are the floor: the module imports, it renders,
it escapes what people type, and it survives a person with nothing on them.
"""

import pathlib
import re

import pytest
from rip.dossier import _esc, _link, collect, render_html
from rip.models import Affiliation, Evidence, Organization, Person, PersonKey
from sqlalchemy import select


@pytest.fixture
def somebody(session):
    person = Person(id="p1", canonical_name="Ada Lovelace", country="GB",
                    current_organization="Analytical Engine Works")
    org = Organization(name="Analytical Engine Works")
    session.add_all([person, org])
    session.flush()
    session.add_all([
        Evidence(person_id="p1", attribute_type="research_interest",
                 value="Analytical engines", source="orcid",
                 verification_state="corroborated",
                 url="https://example.org/a"),
        Evidence(person_id="p1", attribute_type="bio",
                 value="Worked on the Analytical Engine.", source="web"),
        PersonKey(person_id="p1", key_type="orcid", key_value="0000-0002-1825-0097"),
        Affiliation(person_id="p1", organization_id=org.id, role="Mathematician"),
    ])
    session.commit()
    return person


def test_it_renders_a_page_for_a_real_person(session, somebody):
    html = render_html(collect(session, somebody))
    assert "Ada Lovelace" in html
    assert "Analytical engines" in html
    assert html.lstrip().startswith("<")
    assert "corroborated" in html          # the tag constant is reachable
    assert "<a href='https://example.org/a'>source</a>" in html


def test_it_renders_for_somebody_with_nothing_on_them(session):
    """A person with no evidence, no works and no affiliations still has a
    page; every section of it is a loop over an empty list."""
    bare = Person(id="p2", canonical_name="Nobody Yet")
    session.add(bare)
    session.commit()
    html = render_html(collect(session, bare))
    assert "Nobody Yet" in html
    assert "<html" in html.lower() or html.lstrip().startswith("<")


def test_what_a_person_typed_cannot_become_markup(session):
    """Names and topics come from sources and from people. Both reach this
    page verbatim unless something escapes them."""
    nasty = Person(id="p3", canonical_name="<script>alert(1)</script>")
    session.add(nasty)
    session.flush()
    session.add(Evidence(person_id="p3", attribute_type="research_interest",
                         value="a & b <b>bold</b>", source="web"))
    session.commit()
    html = render_html(collect(session, nasty))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>bold</b>" not in html
    assert "&amp;" in html


def test_escaping_and_links_in_isolation():
    assert _esc("<&>") == "&lt;&amp;&gt;"
    assert _esc(None) == ""
    assert _link("https://example.org", "there") == "<a href='https://example.org'>there</a>"
    assert _link(None, "there") == "there"
    # the quoting that needed Python 3.12 to express inside an f-string
    assert "'" in _link("https://example.org", "x")


def test_nothing_here_uses_syntax_newer_than_the_project_supports():
    """requires-python says 3.10, and a backslash inside an f-string is 3.12+.

    The obvious test for this does not work: ast.parse(feature_version=(3, 10))
    accepts the old form quite happily, because feature_version covers a short
    list of features and the f-string tokenizer is not on it. I wrote that
    version first and it passed against the bug it was meant to catch.

    ruff does read target-version, so ruff is the guard. Skipped rather than
    faked when it is not installed.
    """
    import importlib.util
    import subprocess
    import sys

    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff not installed; pip install -e '.[dev]'")
    done = subprocess.run([sys.executable, "-m", "ruff", "check", "backend/rip",
                           "--output-format=concise"],
                          cwd=pathlib.Path(__file__).resolve().parents[2],
                          capture_output=True, text=True)
    too_new = [line for line in done.stdout.splitlines() if "invalid-syntax" in line]
    assert not too_new, "\n".join(too_new)


def test_every_person_in_a_corpus_renders(session, somebody):
    """collect() reads a dozen relationships; a missing one raises rather than
    rendering badly, so the cheapest useful assertion is that it does not."""
    for person in session.execute(select(Person)).scalars():
        html = render_html(collect(session, person))
        assert re.search(r"<\w+", html)
