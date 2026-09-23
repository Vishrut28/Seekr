"""A ratio whose denominator is two is not a measurement.

The duplicate queue's one-line summary is what a reviewer actually decides
on, and it said "2 shared co-authors (100%)". That is the most persuasive
sentence the queue can produce, and in the case that provoked this it meant
almost nothing: Tim Steiner's record holds ONE paper, "The International
Classification of Headache Disorders: 2nd edition" — a consensus document of
his field — so both of his two co-authors are shared with every headache
researcher alive. The overlap is 100% because the pool is 2.

Three pairs in the live queue read 100%, and all three are 2-of-2. The trap
had been written out by hand in one row's deferral note; these tests put it
in the line every row gets.

Nothing about the merge gate changes. `proof` already requires shared_co >= 5
before a ratio may stand on its own, so a thin 100% never merged anyone —
it only read as though it should.
"""

from rip.dedupe import Features, judge


def features(name, *, coauthors, pubs=(), orgs=(), topics=(), orcids=()):
    return Features(
        person_id=name.lower().replace(" ", "-"),
        name=name,
        name_key=" ".join(name.lower().split()),
        full_words=len([w for w in name.split() if len(w) > 1]),
        pubs=set(pubs),
        coauthors=set(coauthors),
        orgs=set(orgs),
        topics=set(topics),
        orcids=set(orcids),
        source_ids={},
    )


def summary(a, b):
    return judge(a, b, names_vouched=True).reason


def test_a_thin_hundred_percent_says_how_thin_it_is():
    """The Steiner case. Two co-authors, both shared, and the line must not
    let that read as total agreement."""
    thin = features("Tim Steiner", coauthors=["jes olesen", "peter goadsby"])
    broad = features("Tim Steiner",
                     coauthors=[f"co{i}" for i in range(38)] + ["jes olesen",
                                                                "peter goadsby"])
    why = summary(thin, broad)
    assert "2 shared co-authors" in why
    assert "of the 2 on the smaller record" in why, why
    assert "100%" not in why, "the percentage is the misleading part"


def test_a_broad_overlap_reads_differently_from_a_thin_one():
    """The whole point: the two must not produce the same sentence."""
    shared = [f"co{i}" for i in range(12)]
    a = features("Asha Rao", coauthors=shared + ["x1", "x2"])
    b = features("Asha Rao", coauthors=shared + [f"y{i}" for i in range(28)])
    thin_a = features("Asha Rao", coauthors=["co0", "co1"])
    thin_b = features("Asha Rao", coauthors=["co0", "co1", "z"])

    assert "of the 14 on the smaller record" in summary(a, b)
    assert "of the 2 on the smaller record" in summary(thin_a, thin_b)
    assert summary(a, b) != summary(thin_a, thin_b)


def test_the_denominator_travels_with_the_signals():
    """Stored on the queue row, so a reviewer reading the JSON — or a later
    change to the wording — still has the number."""
    a = features("Asha Rao", coauthors=["p", "q"])
    b = features("Asha Rao", coauthors=["p", "q", "r", "s"])
    signals = judge(a, b, names_vouched=True).signals
    assert signals["shared_coauthors"] == 2
    assert signals["coauthor_pool"] == 2
    assert signals["coauthor_overlap"] == 1.0


def test_a_thin_ratio_still_does_not_merge_anyone():
    """Unchanged, and asserted because the wording change must not be read as
    having loosened anything: `proof` wants five shared co-authors before a
    ratio counts, so 2-of-2 has always gone to review, not to merge."""
    a = features("Asha Rao", coauthors=["p", "q"], orgs=["acme"], topics=["x", "y"])
    b = features("Asha Rao", coauthors=["p", "q", "r"], orgs=["acme"], topics=["x", "z"])
    assert judge(a, b, names_vouched=True).decision == "review"
