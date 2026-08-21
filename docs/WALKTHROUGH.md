# Seekr — a walkthrough

*What this is, how it works, and what it can and cannot do.*

---

## In one paragraph

Seekr finds people on the public internet, works out which scattered profiles
belong to the same human, and keeps a receipt for every fact it records. It
does **not** decide who is good — scoring, matching and shortlisting happen in
your existing ranking tool. Seekr's job is to hand that tool clean, current,
evidence-backed data.

Today it holds **49,879 people** across **136 countries**, drawn from 10
public sources, with **243,921 pieces of evidence** behind their attributes.

---

## The problem it solves

Suppose you want people who work on neural networks. The information exists,
but it is scattered and none of it agrees on a name:

| Source | What it knows |
|---|---|
| GitHub | `geoffhinton` writes Python |
| OpenAlex | *G. E. Hinton* published 300 papers |
| dblp | *Geoffrey E. Hinton*, University of Toronto, Turing Award |
| His homepage | his email and CV |

Four records, four spellings, no shared ID. A person can see these are one
human in about two seconds. A computer cannot — unless something does the
matching deliberately and shows its work. That is Seekr.

---

## How it works

```
public APIs → connector → raw record → normalizer → entity resolution → graph → read API
                  ↑                                        ↓
             enrichment chain                        merge review
```

**1. Fetch.** Ten connectors, one per source, each using that site's official
public API: OpenAlex, GitHub, ORCID, dblp, Semantic Scholar, Stack Overflow,
Hugging Face, Wikidata, and personal web pages. No scraping behind logins, no
bypassing anything. Each waits politely between requests.

**2. Normalize.** Every source returns a different shape. Each connector
converts its source into one common format, so everything downstream deals
with a single structure. Adding an eleventh source means writing one file.

**3. Decide who is who.** The heart of the system, in strict order:

- **Certain** — two records share an ORCID, an email, or a profile URL →
  same person. No guessing.
- **Likely** — names match ≥92% *and* they share an employer → same person,
  flagged for human confirmation.
- **Ambiguous** — names close but not close enough (85–91%) → **do not merge**;
  queue for review.
- **Otherwise** — a new person.

Two rules keep this honest. **Never merge on name alone** — there are many
Wei Zhangs. And **never merge two records the same source kept apart**: if
OpenAlex issued two different author IDs, OpenAlex already decided they are
two people, and name similarity must not overrule that. Without that second
rule, a test of 10,000 people collapsed into 50 — every common name at a big
institution fused into one monster record.

**4. Pull the thread.** When a GitHub profile mentions an ORCID, Seekr fetches
that ORCID record; that record points at OpenAlex, so it fetches that too,
then the person's homepage. One command becomes a four-source profile. It
stops after three hops and never loops.

**5. Keep receipts.** Every fact — a skill, a job, a location — is a row that
records *where it came from, when it was seen, and how confident we are*. When
two sources disagree (GitHub says Berlin, ORCID says Zurich), Seekr **keeps
both** and flags the disagreement rather than silently picking a winner.

**6. Grow.** From people it already knows, it collects co-authors and
collaborators as leads — 53,371 are queued right now. A background worker
drains them at a rate the sources tolerate.

**7. Serve.** A read API your ranking tool calls. Every person has a permanent
UUID that never changes, so a reference stored today still resolves in a year
— even after merges, because old IDs become tombstones that redirect rather
than 404. A `/v1/changes` feed lets the tool sync only what moved.

---

## Take the tour

```bash
python -m rip.cli serve          # then open http://127.0.0.1:8000/ui
```

**Search.** Type plain English: `deep learning at University of Toronto`.
Seekr shows which of your terms it actually applied and which it could not —
so you are never guessing whether a filter took effect. If nothing matches, it
says so instead of returning arbitrary rows.

**Filter.** Seventeen filters: country, organization, current employer, where
they studied, role, skill, technology, publication and citation thresholds,
"corroborated across N sources", has-a-CV, has-an-email, active-since. They
combine with AND. Sorting is factual only — recency or name, never "best fit",
because that would be ranking.

**Open a person.** The dossier shows every attribute with how many sources
attest it, publications, affiliations, disputed facts side by side, published
CV links, a co-authorship network you can click through, and full provenance:
which source, matched how, with what confidence, seen when.

**Review merges.** Anything Seekr was unsure about waits here for a human.
Approve, split, merge or dismiss — every decision is reversible because the
original records are never destroyed.

---

## Where the data comes from

| Source | What it contributes |
|---|---|
| **OpenAlex** | 125M+ authors: publications, institutions, topics, ORCID |
| **GitHub** | code, languages as skill evidence, repos as projects |
| **ORCID** | employment and education history, verified researcher IDs |
| **dblp** | computer-science bibliography, awards, homepage links |
| **Semantic Scholar** | papers, citation counts, h-index |
| **Wikidata** | an identity hub — links a person's ORCID, GitHub, Scholar and dblp IDs together |
| **Stack Overflow** | answer history as skill evidence |
| **Hugging Face** | published models and datasets |
| **Exa** *(paid)* | professional profiles: job titles, employers, work history — the only non-academic source |
| **Web pages** | personal sites: CV links, emails, cross-profile links |

Scale is reachable through bulk harvesting rather than one-at-a-time calls:
OpenAlex alone exposes 125.7M authors at 200 per request. Measured throughput
is ~65 authors/second to download and ~52/second to ingest — about nine hours
for a million people.

---

## What it is good at, and what it is not

**Good at:** researchers, academics, open-source developers, and anyone with a
public scholarly or code footprint. Cross-source identity resolution. Telling
you *why* it believes something.

How typed search works now — AND then relax, skills vs names vs places, cities
in bios, and when live APIs run — is in [SEARCH.md](SEARCH.md).

**Corporate roles** now work through Exa, a commercial search API: "product
designers at Swiggy Bangalore" returns real people with roles and cities. Read
the tradeoff first — Exa's person records are largely LinkedIn-derived. Seekr
does not crawl LinkedIn, but these are personal records about identifiable
people, so the originating URL is kept on every record and Exa is queried last,
after the free scholarly sources. It is the one source where you should decide
your lawful basis and retention policy deliberately.

**Two limits worth knowing:** the hosted deployment serves a snapshot capped at
~14,000 people by a platform file-size limit — past that, point
`RIP_DATABASE_URL` at Postgres (see `docs/POSTGRES.md`). And enrichment needs a
free `GITHUB_TOKEN`; without one GitHub allows 60 requests an hour and the
chain stalls almost immediately.

---

## The one idea underneath

**Never destroy anything, never invent anything.**

Raw payloads are kept, so parsers can be improved and re-run without
re-downloading. Merges are recorded as decisions, not applied destructively, so
any of them can be undone. Disagreements are preserved instead of resolved. No
value is ever fabricated to fill a gap — a missing field stays missing.

When your ranking tool asks "why do you believe this?", there is always a URL
at the end of the chain.
