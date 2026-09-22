"""What counts as proof that two records are one person.

The duplicate queue is the one place in this codebase where being wrong is
permanent: merging moves everything onto one record and nothing puts it back.
So the adjudicator behind scripts/adjudicate_duplicates.py is allowed to say
"proven" for exactly two things, a shared ORCID and a shared paper, and every
test here exists because some pair in the real queue looked like one of those
and was not.
"""

from rip.models import Authorship, MergeCandidate, Person, PersonKey, Publication
from scripts.adjudicate_duplicates import adjudicate, propagate, still_open


def person(session, name, orcid=None, as_url=False):
    """AS_URL stores the ORCID the way a profile link carries it.

    person_key is unique on (key_type, key_value), so two living records can
    never both hold one ORCID as a typed "orcid" key - the schema itself
    forbids the tidy version of this test. One holding it as a url is the
    shape that actually occurs.
    """
    who = Person(canonical_name=name)
    session.add(who)
    session.flush()
    if orcid:
        session.add(PersonKey(
            person_id=who.id,
            key_type="url" if as_url else "orcid",
            key_value=f"orcid.org/{orcid}" if as_url else orcid))
    session.flush()
    return who


def paper(session, who, title, authors, external_id=None):
    """One publication, credited to WHO. Sources store the same work twice
    under different ids - the arXiv copy and the published one - which is why
    the adjudicator compares titles rather than external ids."""
    pub = Publication(title=title, raw_authors=list(authors),
                      external_id=external_id or f"{title[:20]}-{who.id[:4]}")
    session.add(pub)
    session.flush()
    session.add(Authorship(person_id=who.id, publication_id=pub.id))
    session.flush()
    return pub


def queued(session, a, b):
    candidate = MergeCandidate(person_id=a.id, candidate_person_id=b.id, score=1.0,
                               signals={"reason": "same name"}, status="pending")
    session.add(candidate)
    session.commit()
    return candidate


def verdicts_for(session):
    pending = session.query(MergeCandidate).filter(
        MergeCandidate.status == "pending").order_by(MergeCandidate.id).all()
    pairs, living = still_open(session, pending)
    calls, known = adjudicate(session, pairs, living)
    propagate(calls, known, living)
    return {pair.id: (verdict, why) for verdict, pair, why in calls}


def test_the_same_orcid_on_both_sides_is_proof(session):
    """One record carries it as an ORCID, the other as the profile link it
    arrived on. Reading only the typed key would miss it."""
    a = person(session, "Ann Example", orcid="0000-0001-0000-0001")
    b = person(session, "A. Example", orcid="0000-0001-0000-0001", as_url=True)
    candidate = queued(session, a, b)
    verdict, why = verdicts_for(session)[candidate.id]
    assert verdict == "merge" and "0000-0001-0000-0001" in why


def test_two_different_orcids_are_proof_of_the_opposite(session):
    """Settled on the pair itself, and the reason has to say so. The second
    pass would also reject this, by pooling ORCIDs through merges that are not
    there -- so without checking the wording, removing the direct branch looks
    like it changed nothing and the reader is told about merges that never
    happened."""
    a = person(session, "Ann Example", orcid="0000-0001-0000-0001")
    b = person(session, "Ann Example", orcid="0000-0002-0000-0002")
    candidate = queued(session, a, b)
    verdict, why = verdicts_for(session)[candidate.id]
    assert verdict == "reject"
    assert why.startswith("different ORCIDs 0000-0001"), why
    assert "once the proven merges" not in why, why


def test_one_paper_under_both_records_is_proof(session):
    """The pair this was built for: one record held the arXiv preprint and
    the other the proceedings version of a two-author paper."""
    a, b = person(session, "Vishesh Jain"), person(session, "Vishesh Jain")
    title = "Optimal thresholds for Latin squares"
    paper(session, a, title, ["Vishesh Jain", "Huy Tuan Pham"], external_id="doi")
    paper(session, b, title, ["Vishesh Jain", "Huy Tuan Pham"], external_id="arxiv")
    candidate = queued(session, a, b)
    verdict, why = verdicts_for(session)[candidate.id]
    assert verdict == "merge" and "shared paper" in why


def test_a_thousand_author_paper_proves_nothing(session):
    """Two ALICE heavy-ion records shared four papers. OpenAlex stores the
    first hundred authors, so the stored list is not the list: the name could
    be on the paper again where nothing can see it."""
    a, b = person(session, "D. Dixit"), person(session, "D. Dixit")
    # The name IS on the paper, exactly once, on both copies - so only the
    # list being cut off can stop this being read as proof.
    crowd = ["Dhruv Dixit"] + [f"Collaborator {i}" for i in range(200)]
    title = "Measurement of charged-particle jet suppression"
    paper(session, a, title, crowd, external_id="alice-a")
    paper(session, b, title, crowd, external_id="alice-b")
    candidate = queued(session, a, b)
    assert verdicts_for(session)[candidate.id][0] == "hold"


def test_two_different_papers_with_one_title_prove_nothing(session):
    """Short titles collide. "Socio-economic disparities in India" is two
    unrelated papers, and the adjudicated name is on one of them - so the
    author list has to be read, not just matched in length."""
    a, b = person(session, "Ann Example"), person(session, "Ann Example")
    title = "Socio-economic disparities in India"
    paper(session, a, title, ["Ann Example", "Bob Colleague"], external_id="one")
    paper(session, b, title, ["Carla Other", "Dev Singh"], external_id="two")
    candidate = queued(session, a, b)
    assert verdicts_for(session)[candidate.id][0] == "hold"


def test_the_name_appearing_twice_proves_nothing(session):
    """An agronomy paper carried a Chetan Singh and a Karan Singh. Where the
    name is on the paper twice, the two records may be the two authors - the
    shared paper would then say the opposite of what it looks like."""
    a, b = person(session, "K. Singh"), person(session, "K. Singh")
    title = "Evaluation of IoT based smart drip irrigation"
    both = ["Karan Singh", "Kuldeep Singh", "Ravi Patel"]
    paper(session, a, title, both, external_id="agri-a")
    paper(session, b, title, both, external_id="agri-b")
    candidate = queued(session, a, b)
    assert verdicts_for(session)[candidate.id][0] == "hold"


def test_a_proven_merge_can_disprove_a_pair_nothing_else_could(session):
    """Neither of these pairs is decidable alone. A shared paper ties the
    ORCID-less record to one ORCID, and that contradicts the third record's.
    Without the second pass this sits at "some evidence, short of proof" for
    ever."""
    combinatorialist = person(session, "Vishesh Jain", orcid="0000-0002-7275-3218")
    fragment = person(session, "Vishesh Jain")                 # no ORCID of its own
    surgeon = person(session, "Vishesh Jain", orcid="0000-0002-9273-097X")

    title = "Optimal thresholds for Latin squares"
    paper(session, combinatorialist, title, ["Vishesh Jain", "Huy Tuan Pham"], "doi")
    paper(session, fragment, title, ["Vishesh Jain", "Huy Tuan Pham"], "arxiv")
    paper(session, surgeon, "Congenital diaphragmatic hernia", ["Vishesh Jain"], "surg")

    proven = queued(session, combinatorialist, fragment)
    undecided = queued(session, surgeon, fragment)

    calls = verdicts_for(session)
    assert calls[proven.id][0] == "merge"
    assert calls[undecided.id][0] == "reject"
    assert "once the proven merges are applied" in calls[undecided.id][1]


def test_a_pair_is_judged_on_where_its_people_ended_up(session):
    """Candidate rows name the ids the sweep saw, and merges since then turn
    some of those into tombstones. Judging the tombstone finds no papers and
    no ORCID on it and calls a settled pair circumstantial."""
    a = person(session, "Ann Example", orcid="0000-0001-0000-0001")
    b = person(session, "Ann Example", orcid="0000-0001-0000-0001", as_url=True)
    gone = person(session, "Ann Example")
    gone.merged_into = b.id
    candidate = queued(session, a, gone)

    verdict, why = verdicts_for(session)[candidate.id]
    assert verdict == "merge" and "0000-0001-0000-0001" in why


def test_a_pair_that_became_one_person_is_no_longer_a_question(session):
    a = person(session, "Ann Example")
    gone = person(session, "Ann Example")
    gone.merged_into = a.id
    queued(session, a, gone)
    assert verdicts_for(session) == {}


def test_two_rows_asking_the_same_question_are_asked_once(session):
    a, b = person(session, "Ann Example"), person(session, "Ann Example")
    queued(session, a, b)
    queued(session, b, a)
    assert len(verdicts_for(session)) == 1


def test_co_authors_and_topics_are_never_called_proof(session):
    """The queue's own strongest circumstantial signal. These two share every
    co-author they have and it still is not proof, because that is what being
    in one research group looks like."""
    a, b = person(session, "Ann Example"), person(session, "Ann Example")
    for i in range(4):
        paper(session, a, f"Paper A{i}", ["Ann Example", "Bob Colleague"])
        paper(session, b, f"Paper B{i}", ["Ann Example", "Bob Colleague"])
    candidate = queued(session, a, b)
    assert verdicts_for(session)[candidate.id][0] == "hold"


def test_a_person_with_no_name_at_all_is_still_reported(session):
    """Person.canonical_name is nullable and the corpus now has one: a GitHub
    account carrying nothing but a login ingests with aliases and no name.
    Formatting the report must not be what discovers that."""
    a = person(session, None)
    a.aliases = ["vish1810"]
    b = person(session, "Vish Example")
    session.flush()
    candidate = queued(session, a, b)
    assert verdicts_for(session)[candidate.id][0] == "hold"


def test_a_thirty_five_author_paper_with_one_of_the_name_is_proof(session):
    """The proof an earlier crowd-size ceiling threw away. A KLOE drift-chamber
    paper lists 35 authors and exactly one R. Messi, so there is one slot and
    one person who can be in it. Fully listed beats small."""
    a, b = person(session, "R. Messi"), person(session, "R. Messi")
    crowd = ["R. Messi"] + [f"Collaborator {i}" for i in range(34)]
    title = "The full-length prototype of the KLOE drift chamber"
    paper(session, a, title, crowd, external_id="openalex-copy")
    paper(session, b, title, crowd, external_id="other-copy")
    candidate = queued(session, a, b)
    verdict, why = verdicts_for(session)[candidate.id]
    assert verdict == "merge", why
    assert "35 authors" in why


def test_a_copy_with_no_author_list_counts_only_if_self_claimed(session):
    """ORCID stores no author list, so a work on an ORCID record looks like
    missing data. It is not: the holder put it there themselves. Any OTHER
    source with no list really is unknown and proves nothing."""
    title = "The full-length prototype of the KLOE drift chamber"
    crowd = ["R. Messi"] + [f"Collaborator {i}" for i in range(34)]

    a, b = person(session, "R. Messi"), person(session, "R. Messi")
    paper(session, a, title, crowd, external_id="W2020918607")
    paper(session, b, title, [], external_id="orcid-work:19109464")
    claimed = queued(session, a, b)

    c, d = person(session, "R. Messi"), person(session, "R. Messi")
    paper(session, c, title, crowd, external_id="W-other")
    paper(session, d, title, [], external_id="s2:whatever")
    unknown = queued(session, c, d)

    calls = verdicts_for(session)
    assert calls[claimed.id][0] == "merge", calls[claimed.id][1]
    assert "claimed on ORCID" in calls[claimed.id][1]
    assert calls[unknown.id][0] == "hold", calls[unknown.id][1]


def test_a_paper_at_the_storage_cap_is_refused_even_with_the_name_once(session):
    """Exactly at the cap is where truncation starts, so it is refused there
    rather than one above. One fewer author and the same paper is proof, which
    is what pins the boundary to the cap and not to somebody's taste."""
    from scripts.adjudicate_duplicates import AUTHOR_LIST_CAP, paper_proves

    a, b = person(session, "D. Dixit"), person(session, "D. Dixit")
    title = "Measurement of charged-particle jet suppression"
    at_cap = ["Dhruv Dixit"] + [f"Collaborator {i}" for i in range(AUTHOR_LIST_CAP - 1)]
    assert len(at_cap) == AUTHOR_LIST_CAP
    one_short = at_cap[:-1]

    pub_a = paper(session, a, title, at_cap, external_id="alice-a")
    pub_b = paper(session, b, title, at_cap, external_id="alice-b")
    candidate = queued(session, a, b)
    assert verdicts_for(session)[candidate.id][0] == "hold"

    ok, why = paper_proves(pub_a, pub_b, ("d", "dixit"))
    assert not ok and "cut off" in why, why

    pub_b.raw_authors = one_short
    session.commit()
    ok, why = paper_proves(pub_a, pub_b, ("d", "dixit"))
    assert not ok, "one side is still at the cap, so it is still truncated"

    pub_a.raw_authors = one_short
    session.commit()
    ok, why = paper_proves(pub_a, pub_b, ("d", "dixit"))
    assert ok and f"{AUTHOR_LIST_CAP - 1} authors" in why, why


def test_either_side_of_a_pair_is_followed_to_where_it_ended_up(session):
    """Both sides, not just one. A candidate row names whichever id the sweep
    saw first, so the tombstone turns up on the left as often as the right,
    and following only one side leaves half the queue judging empty records."""
    survivor = person(session, "Ann Example", orcid="0000-0001-0000-0001")
    partner = person(session, "Ann Example", orcid="0000-0001-0000-0001", as_url=True)
    gone = person(session, "Ann Example")
    gone.merged_into = survivor.id
    session.flush()

    left = queued(session, gone, partner)       # tombstone as person_id
    right = queued(session, partner, gone)      # and as candidate_person_id
    calls = verdicts_for(session)
    decided = [v for v, _w in calls.values()]
    assert decided and all(v == "merge" for v in decided), calls
    assert len(calls) == 1, "both rows ask the same question once resolved"
    assert set(calls) <= {left.id, right.id}
