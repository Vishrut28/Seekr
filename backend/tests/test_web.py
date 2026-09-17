"""Phase 2B: single-page web connector (fixture HTML, no network)."""

from rip.connectors.web import WebConnector
from rip.ingest import ingest_profile
from rip.models import Evidence, Person

HTML = """<!doctype html>
<html><head>
<title>Dr. Jane Doe — Systems Research</title>
<meta property="og:description" content="Research engineer working on distributed storage.">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Person","name":"Jane Doe",
 "jobTitle":"Principal Engineer","affiliation":{"@type":"Organization","name":"Acme Labs"},
 "knowsAbout":["Distributed Systems","Consensus Protocols"]}
</script>
</head><body>
<p>Contact: jane@example.edu</p>
<p>ORCID: 0000-0002-1111-2222</p>
<a href="https://github.com/jdoe">code</a>
<a href="https://www.dblp.org/pid/99/1234.html">papers</a>
<a href="/local/page">internal</a>
<a href="https://example.com/unrelated">unrelated</a>
</body></html>"""


def parse(url="https://janedoe.ai/about", html=HTML):
    return WebConnector.normalize(WebConnector.__new__(WebConnector), url, html)


def test_extracts_jsonld_person():
    p = parse()
    assert p.name == "Jane Doe"
    assert p.organizations[0].name == "Acme Labs"
    assert p.organizations[0].role == "Principal Engineer"
    skills = {e.value for e in p.evidence if e.attribute_type == "skill"}
    assert skills == {"Distributed Systems", "Consensus Protocols"}


def test_extracts_identity_signals():
    p = parse()
    assert p.orcid == "0000-0002-1111-2222"
    assert "jane@example.edu" in p.emails
    assert "https://github.com/jdoe" in p.linked_urls
    assert any("dblp.org/pid/99/1234" in u for u in p.linked_urls)
    # non-profile hosts are not collected as identity links
    assert not any("example.com/unrelated" in u for u in p.linked_urls)


def test_external_id_is_normalized_url():
    assert parse("https://WWW.JaneDoe.ai/about/").external_id == "janedoe.ai/about"


def test_falls_back_to_title_when_no_jsonld():
    p = parse(html="<html><head><title>Ada Lovelace</title></head><body>hi</body></html>")
    assert p.name == "Ada Lovelace"
    assert p.organizations == []


def test_renormalize_from_stored_raw(session):
    person = ingest_profile(session, parse())
    record = person.identities[0].source_record
    again = WebConnector.renormalize(WebConnector.__new__(WebConnector), record.external_id, record.raw)
    assert again.name == "Jane Doe"
    assert again.orcid == "0000-0002-1111-2222"


def test_ingest_produces_evidence_and_person(session):
    person = ingest_profile(session, parse())
    assert person.canonical_name == "Jane Doe"
    assert session.query(Person).count() == 1
    bios = session.query(Evidence).filter_by(attribute_type="bio").all()
    assert bios and "distributed storage" in bios[0].value


def test_web_merges_into_person_via_orcid(session):
    from tests.test_enrich import orcid_profile

    p1 = ingest_profile(session, orcid_profile())
    p2 = ingest_profile(session, parse())
    assert p1.id == p2.id  # ORCID on the page is a strong key


def test_robots_disallow_blocks_fetch(monkeypatch):
    connector = WebConnector.__new__(WebConnector)

    class FakeResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /private"

    class FakeClient:
        def get(self, url, timeout=None):
            return FakeResp()

    connector._client = FakeClient()
    assert connector._robots_allows("https://site.example/public") is True
    assert connector._robots_allows("https://site.example/private/page") is False


CV_HTML = """<html><head><title>Prof A Sharma</title></head><body>
<a href="/files/cv.pdf">Curriculum Vitae</a>
<a href="https://example.edu/~a/resume.pdf">My resume (PDF)</a>
<a href="/papers/cvpr2021.pdf">CVPR 2021 paper</a>
<a href="/papers/discovery.pdf">Discovery of things</a>
<a href="/teaching">Teaching</a>
</body></html>"""


def test_cv_links_only_when_page_says_so():
    p = parse("https://example.edu/~a/", CV_HTML)
    cvs = {e.value for e in p.evidence if e.attribute_type == "cv_url"}
    assert cvs == {
        "https://example.edu/files/cv.pdf",
        "https://example.edu/~a/resume.pdf",
    }
    # "CVPR" is not a CV, and an unrelated PDF is not a CV
    assert not any("cvpr" in c for c in cvs)
    assert not any("discovery" in c for c in cvs)


def test_cv_evidence_keeps_provenance():
    p = parse("https://example.edu/~a/", CV_HTML)
    cv = next(e for e in p.evidence if e.attribute_type == "cv_url")
    assert cv.url == "https://example.edu/~a/"      # the page it was found on
    assert "linked as" in cv.extracted_info          # and how it was identified


def test_documents_endpoint_lists_only_found_links(session):
    from rip.api import get_documents

    person = ingest_profile(session, parse("https://example.edu/~a/", CV_HTML))
    docs = get_documents(person.id, db=session)
    assert {c["url"] for c in docs["cvs"]} == {
        "https://example.edu/files/cv.pdf",
        "https://example.edu/~a/resume.pdf",
    }
    assert all(c["found_on"] == "https://example.edu/~a/" for c in docs["cvs"])
    assert "generated by seekr" in docs["note"].lower()  # we do not create documents


def test_person_without_cv_reports_none(session):
    from rip.api import get_documents
    from tests.test_enrich import orcid_profile

    person = ingest_profile(session, orcid_profile())
    assert get_documents(person.id, db=session)["cvs"] == []


# --- subpages -------------------------------------------------------------------

HOME = """<html><head><title>Asha Rao</title>
<meta name="description" content="I study soil microbial ecology."></head><body>
<a href="/about.html">About</a>
<a href="https://www.asharao.org/cv/">CV</a>
<a href="publications">Publications</a>
<a href="/research">Research</a>
<a href="/teaching">Teaching</a>
<a href="https://other.org/about">About</a>
<a href="/files/cv.pdf">CV (PDF)</a>
<a href="https://asharao.org/">Home</a>
</body></html>"""

ABOUT = """<html><head><title>About Asha Rao</title>
<meta name="description" content="About page blurb."></head><body>
ORCID 0000-0003-3541-7853
<a href="https://github.com/asharao">GitHub</a>
</body></html>"""

CV_PAGE = """<html><body><a href="/files/rao-cv-2026.pdf">Download CV</a></body></html>"""


def test_subpages_are_same_site_and_named_as_such():
    found = WebConnector.subpages("https://asharao.org/", HOME, limit=10)
    assert found == [
        "https://asharao.org/about.html",
        "https://www.asharao.org/cv/",
        "https://asharao.org/publications",
        "https://asharao.org/research",
    ]
    # a limit keeps the most useful kinds
    assert WebConnector.subpages("https://asharao.org/", HOME, limit=2) == found[:2]
    assert WebConnector.subpages("https://asharao.org/", HOME, limit=0) == []


def test_a_link_merely_mentioning_a_kind_is_not_that_page():
    html = '<a href="/data">About this dataset</a><a href="/talks">CVPR talk</a>'
    assert WebConnector.subpages("https://asharao.org/", html, limit=10) == []


def test_other_sites_documents_and_the_page_itself_are_not_followed():
    html = ('<a href="https://other.org/about">About</a>'
            '<a href="/files/cv.pdf">CV</a>'
            '<a href="https://asharao.org/about">About</a>')
    assert WebConnector.subpages("https://asharao.org/about", html, limit=10) == []


def fake_connector(pages, disallowed=()):
    conn = WebConnector.__new__(WebConnector)
    fetched = []

    def get_text(url, params=None):
        fetched.append(url)
        if url not in pages:
            raise RuntimeError("404")
        return pages[url]

    conn.get_text = get_text
    conn._fetch_html = get_text
    conn._robots_allows = lambda url: not any(d in url for d in disallowed)
    return conn, fetched


def test_subpages_add_what_the_home_page_left_out(monkeypatch):
    monkeypatch.setattr("rip.connectors.web.MAX_SUBPAGES", 3)
    conn, fetched = fake_connector({
        "https://asharao.org/": HOME,
        "https://asharao.org/about.html": ABOUT,
        "https://www.asharao.org/cv/": CV_PAGE,
    })
    p = conn.fetch("https://asharao.org/")
    # the page speaks first: its own name and summary are kept
    assert p.name == "Asha Rao"
    assert p.summary == "I study soil microbial ecology."
    assert [e.value for e in p.evidence if e.attribute_type == "bio"] == [
        "I study soil microbial ecology."]
    # ...and subpages fill the rest
    assert p.orcid == "0000-0003-3541-7853"
    assert "https://github.com/asharao" in p.linked_urls
    cv = [e for e in p.evidence if e.attribute_type == "cv_url"]
    assert "https://www.asharao.org/files/rao-cv-2026.pdf" in [e.value for e in cv]
    assert any(e.url == "https://www.asharao.org/cv/" for e in cv)   # provenance
    # three subpages at most; a missing one is skipped
    assert fetched == ["https://asharao.org/", "https://asharao.org/about.html",
                       "https://www.asharao.org/cv/", "https://asharao.org/publications"]
    # stored, so a reparse sees the same pages
    again = conn.renormalize(p.external_id, p.raw)
    assert again.orcid == p.orcid and again.linked_urls == p.linked_urls


def test_robots_is_honored_for_each_subpage(monkeypatch):
    monkeypatch.setattr("rip.connectors.web.MAX_SUBPAGES", 3)
    conn, fetched = fake_connector({
        "https://asharao.org/": HOME,
        "https://asharao.org/about.html": ABOUT,
    }, disallowed=("about",))
    p = conn.fetch("https://asharao.org/")
    assert "https://asharao.org/about.html" not in fetched
    assert p.orcid is None


def test_robots_txt_is_read_once_per_site():
    connector = WebConnector.__new__(WebConnector)
    calls = []

    class FakeResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /private"

    class FakeClient:
        def get(self, url, timeout=None):
            calls.append(url)
            return FakeResp()

    connector._client = FakeClient()
    assert connector._robots_allows("https://site.example/a") is True
    assert connector._robots_allows("https://site.example/private/b") is False
    assert calls == ["https://site.example/robots.txt"]
