"""Semantic Scholar as a topical source: authors of papers on the subject,
asked only when a key makes the request likely to be answered."""

from rip.connectors.semanticscholar import MAX_TOPIC_TEAM, SemanticScholarConnector

from rip import discovery


def paper(*authors, cited=0):
    return {"citationCount": cited,
            "authors": [{"authorId": aid, "name": name} for aid, name in authors]}


class Fake:
    def __init__(self, papers):
        self.papers = papers
        self.calls = []

    def __call__(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return {"data": self.papers}


def connector(fake):
    conn = SemanticScholarConnector.__new__(SemanticScholarConnector)
    conn.get_json = fake
    return conn


def test_authors_with_more_papers_on_the_topic_come_first():
    fake = Fake([
        paper(("1", "Ada Lovelace"), ("2", "Bob Babbage")),
        paper(("2", "Bob Babbage"), ("3", "Cy Turing")),
        paper(("2", "Bob Babbage")),
    ])
    found = connector(fake).search_authors_by_topic("soil microbiome", limit=10)
    assert [a["name"] for a in found] == ["Bob Babbage", "Ada Lovelace", "Cy Turing"]
    assert fake.calls[0][0].endswith("/paper/search")
    assert fake.calls[0][1]["query"] == "soil microbiome"


def test_large_collaborations_do_not_fill_the_list():
    consortium = paper(*[(str(100 + i), f"Member {i}") for i in range(MAX_TOPIC_TEAM + 1)])
    fake = Fake([consortium, paper(("1", "Ada Lovelace"))])
    found = connector(fake).search_authors_by_topic("particle physics", limit=5)
    assert [a["name"] for a in found] == ["Ada Lovelace"]


def test_limit_is_respected():
    fake = Fake([paper(*[(str(i), f"Person {i}") for i in range(10)])])
    assert len(connector(fake).search_authors_by_topic("x", limit=3)) == 3


class Recorder:
    def __init__(self):
        self.topic = self.name = 0

    def search_authors_by_topic(self, query, limit):
        self.topic += 1
        return [{"id": "9", "name": "Ada Lovelace", "affiliations": []}]

    def search_authors(self, query, limit):
        self.name += 1
        return [{"id": "8", "name": "Ada Lovelace", "affiliations": ["Oxford"]}]


def test_a_subject_is_searched_by_topic_only_with_a_key(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("rip.connectors.get_connector", lambda source: rec)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    assert discovery._search_semanticscholar("soil microbiome researchers", 5) == []
    assert rec.topic == 0
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    rows = discovery._search_semanticscholar("soil microbiome researchers", 5)
    assert rec.topic == 1
    assert rows[0]["source"] == "semanticscholar" and rows[0]["external_id"] == "9"


def test_a_name_is_still_searched_by_name(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("rip.connectors.get_connector", lambda source: rec)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "k")
    rows = discovery._search_semanticscholar("Ada Lovelace", 5)
    assert (rec.name, rec.topic) == (1, 0)
    assert rows[0]["affiliation"] == "Oxford"
