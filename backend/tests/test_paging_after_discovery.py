"""A live search grows the corpus mid-query, and the page cursor must follow.

/v1/query answers once, runs a live search, and — if that stored anybody —
asks the corpus again, because the people it just fetched are answers too.
The next-page cursor was computed during the FIRST pass and never revisited,
so it described a corpus that no longer existed: a query the corpus could not
answer at all reported "no more pages" while holding a page full of freshly
stored people and more behind it. The button never appeared and the rest were
unreachable.
"""

from rip.ingest import ingest_profile
from rip.normalize import EvidenceItem
from tests.test_resolution import make_profile

from rip import api


def rust_person(session, tag: str):
    return ingest_profile(session, make_profile(
        external_id=tag, url=f"https://github.com/{tag}", raw={"login": tag},
        name=f"Rust Person {tag}", usernames=[f"github:{tag}"],
        evidence=[EvidenceItem(attribute_type="skill", value="Rust")]))


def discovery_that_stores(session, tags):
    """Stand in for a live search that fetched people and kept them."""
    def fake(*args, **kwargs):
        found = [rust_person(session, t) for t in tags]
        session.commit()
        from rip.nlq import invalidate_vocab
        invalidate_vocab()
        return [{"name": p.canonical_name, "person_id": p.id, "stored": True} for p in found]
    return fake


def test_the_cursor_describes_the_corpus_the_live_search_left_behind(session, monkeypatch):
    rust_person(session, "seed0")
    session.commit()
    monkeypatch.setattr("rip.discovery.discovery_suggestions",
                        discovery_that_stores(session, [f"live{i}" for i in range(6)]))

    page = api.nl_query(q="rust developers", limit=4, offset=0, discover="true", db=session)

    assert page["total_matches"] == 7            # 1 seeded + 6 just stored
    assert page["has_more"] is True, "the rest of the corpus was unreachable"
    assert page["next_offset"] == 4

    rest = api.nl_query(q="rust developers", limit=4, offset=page["next_offset"],
                        discover="false", db=session)
    shown = {r["id"] for r in page["results"]} | {r["id"] for r in rest["results"]}
    everyone = {r["id"] for r in api.nl_query(
        q="rust developers", limit=50, discover="false", db=session)["results"]}
    assert everyone <= shown, "paging skipped someone the reader never saw"


def test_a_page_that_ends_the_corpus_offers_no_next(session, monkeypatch):
    for i in range(3):
        rust_person(session, f"seed{i}")
    session.commit()
    monkeypatch.setattr("rip.discovery.discovery_suggestions", lambda *a, **k: [])
    page = api.nl_query(q="rust developers", limit=50, offset=0, discover="true", db=session)
    assert page["has_more"] is False and page["next_offset"] is None
