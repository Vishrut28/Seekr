"""Names written differently: nicknames and transliterations in search,
transliterations only in resolution, and names with commas in them."""

from rip.ingest import ingest_profile
from rip.models import Person
from rip.names import name_phrase_forms, search_forms, spelling_key
from rip.nlq import execute_progressive, parse
from rip.normalize import OrgAffiliation
from rip.personhood import assess
from tests.test_resolution import make_profile


def person(session, ext, name, orgs=(), aliases=()):
    return ingest_profile(session, make_profile(
        external_id=ext, url=f"https://github.com/{ext}", raw={"login": ext},
        name=name, usernames=[f"github:{ext}"], aliases=list(aliases),
        organizations=[OrgAffiliation(name=o) for o in orgs]))


def found(session, query):
    rows, _, _ = execute_progressive(session, parse(session, query))
    return sorted(p.canonical_name for p in rows)


def test_nicknames_and_transliterations_are_the_same_word_to_search():
    assert "william" in search_forms("Bill")
    assert "muhammad" in search_forms("Mohammed")
    assert "kathryn" in search_forms("katherine")          # look-alike: search only
    assert "samantha" not in search_forms("samuel")        # "sam" joins neither to the other
    assert name_phrase_forms("bill gates")[0] == "bill gates"
    assert "william gates" in name_phrase_forms("bill gates")


def test_resolution_folds_transliterations_but_not_lookalikes_or_nicknames():
    assert spelling_key("Aggarwal") == spelling_key("agrawal")
    assert spelling_key("katherine") != spelling_key("kathryn")
    assert spelling_key("bill") != spelling_key("william")


def test_a_search_by_nickname_finds_the_full_name(session):
    person(session, "w", "William Gates")
    person(session, "o", "Melinda French")
    assert found(session, "Bill Gates") == ["William Gates"]


def test_a_search_by_one_transliteration_finds_the_other(session):
    person(session, "a", "Rahul Aggarwal")
    person(session, "b", "Mohammed Rafi")
    assert found(session, "Rahul Agarwal") == ["Rahul Aggarwal"]
    assert found(session, "Muhammad Rafi") == ["Mohammed Rafi"]


def test_transliterations_of_one_name_at_one_institute_are_merged(session):
    # two sources: two IDs from ONE source are that source saying "two people"
    ingest_profile(session, make_profile(
        source="openalex", external_id="A1", url="https://openalex.org/A1", raw={},
        name="Rahul Agrawal", usernames=[], organizations=[OrgAffiliation(name="IIT Delhi")]))
    ingest_profile(session, make_profile(
        source="dblp", external_id="d/1", url="https://dblp.org/pid/d/1", raw={},
        name="Rahul Aggarwal", usernames=[], organizations=[OrgAffiliation(name="IIT Delhi")]))
    assert session.query(Person).filter(Person.merged_into.is_(None)).count() == 1


def test_lookalike_names_at_one_institute_are_not_merged(session):
    ingest_profile(session, make_profile(
        source="openalex", external_id="A1", url="https://openalex.org/A1", raw={},
        name="Katherine Johnson", usernames=[], organizations=[OrgAffiliation(name="Acme Labs")]))
    ingest_profile(session, make_profile(
        source="dblp", external_id="d/1", url="https://dblp.org/pid/d/1", raw={},
        name="Kathryn Johnson", usernames=[], organizations=[OrgAffiliation(name="Acme Labs")]))
    assert session.query(Person).filter(Person.merged_into.is_(None)).count() == 2


def test_an_initials_alias_does_not_match_every_initials_name(session):
    """"Karan Singh" carries the alias "K Singh"; a different K. Singh at the
    same institute used to score 100 against it and be merged in."""
    # two sources, so only the alias rule can keep them apart
    ingest_profile(session, make_profile(
        source="openalex", external_id="A1", url="https://openalex.org/A1", raw={},
        name="Karan Singh", aliases=["K Singh"], usernames=[],
        organizations=[OrgAffiliation(name="IIT Delhi")]))
    ingest_profile(session, make_profile(
        source="dblp", external_id="d/9", url="https://dblp.org/pid/d/9", raw={},
        name="K. Singh", usernames=[], organizations=[OrgAffiliation(name="IIT Delhi")]))
    assert session.query(Person).filter(Person.merged_into.is_(None)).count() == 2


def test_names_with_commas_in_them_are_unpacked():
    assert assess("Singh, Karan").name == "Karan Singh"
    assert assess("Smith, John A.").name == "John A. Smith"
    assert assess("Dhruv Dixit, PhD").name == "Dhruv Dixit"
    assert assess("Goutam Sarker,").name == "Goutam Sarker"
    assert assess("Dhruv Dixit, Bangalore").name == "Dhruv Dixit"
    listed = assess("Rahul M Mulajkar,Rahul Mukundrao Mulajkar,RMM,Rahul Mulajkar")
    assert listed.name == "Rahul Mukundrao Mulajkar"
    assert "Rahul Mulajkar" in listed.aliases
    assert assess("Paris, France").name == "Paris, France"   # not a name to rearrange


def test_the_other_spellings_become_aliases_at_ingest(session):
    stored = person(session, "m", "Rahul M Mulajkar,Rahul Mukundrao Mulajkar,RMM,Rahul Mulajkar")
    assert stored.canonical_name == "Rahul Mukundrao Mulajkar"
    assert "Rahul Mulajkar" in stored.aliases


def test_purging_renames_comma_names_and_keeps_their_spellings(session, monkeypatch):
    """Records stored before the comma rule are cleaned by purge-nonpersons."""
    import argparse

    from rip import cli

    junk = Person(canonical_name="Rahul M Mulajkar,Rahul Mukundrao Mulajkar,RMM,Rahul Mulajkar",
                  aliases=[])
    session.add(junk)
    session.commit()
    monkeypatch.setattr("rip.db.SessionLocal", lambda: session)
    cli.cmd_purge_nonpersons(argparse.Namespace(yes=True))
    session.refresh(junk)
    assert junk.canonical_name == "Rahul Mukundrao Mulajkar"
    assert "Rahul Mulajkar" in junk.aliases
    assert "Rahul Mukundrao Mulajkar" not in junk.aliases



def test_half_a_name_does_not_answer_a_whole_one(session):
    """"Katherine Jones" found Kate Tilling — Katherine by nickname, Jones by
    nobody. It was then shown as a partial match; half a name is not the
    person, so nobody here is the answer, and live search asks for the whole
    name."""
    person(session, "a", "Kate Tilling")
    rows, parsed, _dropped = execute_progressive(session, parse(session, "Katherine Jones"))
    assert rows == []
    assert parsed.name_terms == ["Katherine", "Jones"] and parsed.unmatched_terms == []


def test_a_whole_name_that_matches_is_a_full_match(session):
    person(session, "a", "Kate Tilling")
    rows, _parsed, _dropped = execute_progressive(session, parse(session, "Kate Tilling"))
    assert getattr(rows[0], "partial_match", None) is None


def test_an_unknown_employer_is_labelled_an_employer_not_a_name(session):
    person(session, "a", "Dhruv Dixit")
    rows, _parsed, _dropped = execute_progressive(
        session, parse(session, "Dhruv Dixit at Zzyzx"))
    assert rows[0].partial_match == {"missing": [{"term": "Zzyzx", "as": "org"}]}


def test_a_subject_does_not_make_half_a_name_whole(session):
    """Kate Tilling works on robotics, and is still not Katherine Jones."""
    from rip.normalize import EvidenceItem

    ingest_profile(session, make_profile(
        external_id="a", url="https://github.com/a", raw={"login": "a"},
        name="Kate Tilling", usernames=["github:a"],
        evidence=[EvidenceItem(attribute_type="research_interest", value="Robotics")]))
    rows, _parsed, _dropped = execute_progressive(
        session, parse(session, "robotics Katherine Jones"))
    assert rows == []


def test_a_product_style_word_is_not_a_missing_name(session):
    """"gpt4" is product branding, not a surname: reporting it as part of the
    person's name would be a different claim. It is still reported as a term
    that was not applied."""
    person(session, "a", "Kate Tilling")
    rows, parsed, _dropped = execute_progressive(session, parse(session, "Kate Tilling gpt4"))
    assert parsed.unmatched_terms == ["gpt4"]
    assert rows and getattr(rows[0], "partial_match", None) is None


def test_an_unmatched_word_beside_a_subject_is_not_a_missing_name(session):
    """It is reported as an unapplied term instead; calling every result
    partial would label an ordinary topic search a near miss."""
    from rip.normalize import EvidenceItem

    ingest_profile(session, make_profile(
        external_id="a", url="https://github.com/a", raw={"login": "a"},
        name="Ada Lovelace", usernames=["github:a"],
        evidence=[EvidenceItem(attribute_type="research_interest",
                               value="Large Language Models")]))
    parsed = parse(session, "LLM evaluation")
    rows, _parsed, _dropped = execute_progressive(session, parsed)
    assert parsed.unmatched_terms == ["evaluation"]
    assert rows and getattr(rows[0], "partial_match", None) is None
