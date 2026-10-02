# Seekr

*Resource intelligence platform — worldwide people discovery*

**New here? Read [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md)** — what Seekr is,
how it works, and what it can and cannot do, in plain language. This file is
the operator reference.

A data layer that discovers, collects, normalizes, and maintains **evidence-backed
profiles of publicly discoverable people** — specialized experts and strong
generalists — and exposes them through a clean read API.

**`/v1/query` ranks; `/v1/persons` does not.** Natural-language search orders
results by how well the evidence backs what was asked — see [Relevance
ranking](#relevance-ranking). The faceted filter API stays deliberately
unordered, so a downstream tool can still apply its own scoring to a raw
match set.

```
Internet / External Sources
        ↓
Source → Connector → Raw SourceRecord → Normalizer → Entity Resolution → Resource DB
        ↓
Resource API  (stable IDs, evidence, provenance, change feed)
        ↓                              ↓
/v1/persons  (filters, unordered)   /v1/query  (filters + relevance ranking)
```

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# ingest
.venv/bin/python -m rip.cli search-openalex "Geoffrey Hinton"   # find author IDs
.venv/bin/python -m rip.cli ingest openalex A5110248343
.venv/bin/python -m rip.cli ingest github torvalds

# serve everything — API at /docs, UI at /ui. Reads .env for API keys.
.venv/bin/python -m rip.cli serve

# keep data fresh (run from cron)
.venv/bin/python -m rip.cli refresh --older-than-hours 24

# review suspicious merges and possible duplicates (also available via API)
.venv/bin/python -m rip.cli review list
.venv/bin/python -m rip.cli review triage        # judge the queue; --yes to act on it
.venv/bin/python -m rip.cli review approve 114   # confirm a fuzzy merge
.venv/bin/python -m rip.cli review split 22      # undo one: detach into new person
.venv/bin/python -m rip.cli review merge 7       # fold a possible-duplicate pair together
.venv/bin/python -m rip.cli review dismiss 7     # or mark the pair as distinct people

# rebuild the search index from the tables (no network; normally automatic)
.venv/bin/python -m rip.cli reindex

# re-run parsers over stored raw payloads (no network; after parser improvements)
.venv/bin/python -m rip.cli reparse

# bulk load a JSONL(.gz) dump; blocked resolution keeps this sub-quadratic
.venv/bin/python -m rip.cli bulk-ingest openalex --file authors.jsonl --batch-size 500

# continuous lead worker (separate process from `serve`; SQLite WAL handles both)
.venv/bin/python -m rip.cli worker --poll-interval 30 --limit 25
# ...or four of them: each claims its own leads (also: ingest-leads, refresh)
.venv/bin/python -m rip.cli worker --processes 4

# how big is the backlog, and how long will it take?
.venv/bin/python -m rip.cli queue-stats
# one-time catch-up: large batch, no enrichment (safe without API tokens)
RIP_LEAD_BATCH=500 .venv/bin/python -m rip.cli worker --limit 500 --once --no-enrich

# environment / queue-depth sanity check
.venv/bin/python -m rip.cli check-db

# push queued change events to webhook subscribers
.venv/bin/python -m rip.cli deliver-webhooks

# bulk discovery: mine stored records for new people, then drain the queue
.venv/bin/python -m rip.cli discover                        # co-authors (no API calls)
.venv/bin/python -m rip.cli discover --github-contributors  # + repo contributors (live)
.venv/bin/python -m rip.cli ingest-leads --limit 25

# tests
.venv/bin/python -m pytest tests
```

Environment:

| Variable | Purpose |
|---|---|
| `RIP_DATABASE_URL` | SQLAlchemy URL (default `sqlite:///rip.db`; use Postgres in production) |
| `GITHUB_TOKEN` | Optional. Raises GitHub rate limit from 60/h to 5000/h |
| `OPENALEX_MAILTO` | Optional. Joins the OpenAlex polite pool |

## Data model

- **Person** — stable UUID, canonical name, aliases, location, current role/org,
  profile URLs. The UUID never changes, so the ranking tool can reference it
  even as external profiles change.
- **SourceRecord** — one raw capture per external profile (`source` +
  `external_id` unique). Immutable-ish; never destroyed by merging. Carries
  `first_observed`, `last_observed`, `extracted_at`, and a `content_hash` for
  change detection.
- **IdentityLink** — the resolution decision binding a SourceRecord to a
  Person, with `match_method`, `match_confidence`, and the signals used.
  Every merge is auditable and reversible.
- **PersonKey** — strong identifiers (ORCID, email, `github:login`,
  normalized URLs) used for deterministic resolution.
- **Evidence** — every attribute claim (skill, research interest, role,
  education, location, bio, …) is a row pointing at the SourceRecord it came
  from, with confidence and `verification_state`
  (`unverified` → `corroborated` when a second source makes the same claim).
- **Organization / Affiliation** — `worked_at` / `studied_at` edges with roles
  and dates.
- **Publication / Authorship** — deduped by DOI or source ID; author position
  preserved.
- **Project / Contribution** — repos and other projects with technologies and
  activity metrics.
- **ChangeLog** — field-level change history; also records conflicts and key
  collisions. Powers the incremental `/v1/changes` feed.
- **IngestionRun** — per-fetch status for source health monitoring.

### Conflicts are preserved, never overwritten

If GitHub says "Berlin" and a later source says "Zurich", the Person keeps the
first value, a `conflict:location` ChangeLog row is written, **both** location
Evidence rows remain queryable, and a first-class `attribute_conflict` row
records both sides with their provenance (`GET /v1/persons/{id}/conflicts`).
"Where did we get this?" is always answerable via
`/v1/persons/{id}/provenance`.

### A record's claims follow the record

Re-ingesting a record, whether by `refresh` or `reparse`, removes the claims
that record no longer makes. Examples: a keyword someone deleted from ORCID, or
one a parser now rejects. ORCID keywords that only advertise the person's own
name ("Rahul Kumar ceo") are one such case; they had turned "Rahul" into a
research subject. A claim that another source still makes stays, and is no
longer marked corroborated once only one source backs it. Locations and roles
are history rather than retractions: someone who moved from Berlin to Zurich
did live in Berlin (see above).

## Entity resolution

Order of strategies (see `backend/rip/resolution.py`):

1. **Deterministic** — any strong key already attached to a person
   (ORCID, public email, source username, normalized profile/website URL)
   → merge, confidence 0.97.
2. **Fuzzy** — name similarity ≥ 92 (token-sort) **and** a shared
   organization → merge, confidence 0.75.
3. Otherwise → new person.

Source records survive merging; only IdentityLinks are added. Key collisions
(a strong key claimed by two persons) are logged rather than silently
reassigned.

## API contract (for the ranking tool)

| Endpoint | Purpose |
|---|---|
| `GET /v1/persons?...` | faceted people search — see filter table below (no ranking) |
| `GET /v1/facets?field=` | available filter values + how many people carry each |
| `GET /v1/query?q=` | natural-language search: query parsed into filters against the live vocabulary; response lists `applied_filters` and `unmatched_terms` honestly; results ranked by evidence, each carrying `score` + `score_components` |
| `GET /ui` | internal exploration UI (archive-styled; token pasted in-page) |
| `GET /v1/persons/{id}` | profile by stable UUID |
| `GET /v1/persons/{id}/evidence?attribute_type=` | evidence with confidence + verification state |
| `GET /v1/persons/{id}/publications` | publications + author position |
| `GET /v1/persons/{id}/projects` | projects + contribution role |
| `GET /v1/persons/{id}/organizations` | affiliation history |
| `GET /v1/persons/{id}/provenance` | which sources, matched how, observed when |
| `GET /v1/persons/{id}/graph?depth=1..3` | organizations and co-authors out to three hops; each person's strongest `limit_coauthors` (20) followed, walk stops at `max_nodes` (200) and says `truncated` |
| `GET /v1/persons/{id}/documents` | published CV/résumé links and profile pages — **only links found on pages we fetched; Seekr never creates or hosts a document** |
| `GET /v1/changes?since_id=` | incremental sync feed (integer cursor + `has_more`; legacy `since` timestamp still accepted) |
| `POST /v1/review/duplicates/{id}/merge` \| `/reject` | act on near-miss duplicate candidates |
| `GET /v1/health/sources` | ingestion run status per source |
| `GET /v1/review/merges` | suspicious merges: fuzzy matches, heavily-linked persons, key collisions |
| `POST /v1/review/merges/{link_id}/approve` | mark a fuzzy merge as human-verified |
| `POST /v1/review/merges/{link_id}/split` | undo a merge: detach that source record (and all rows it produced) into a new person |

OpenAPI docs at `/docs` when serving.

## Relevance ranking

`GET /v1/query` filters first, then ranks the matches. Filtering decides who is
eligible; ranking decides who leads. Every result carries `score` and
`score_components`, so a position in the list is always traceable to evidence
rather than asserted:

```json
{ "canonical_name": "Abhishek Veeramalla", "score": 0.414,
  "score_components": { "depth": 0.27, "confidence": 0.9,
                        "corroboration": 0.0, "breadth": 0.431, "recency": 0.5 } }
```

| Signal | Weight | What it measures |
|---|---|---|
| `depth` | 0.25 | How much evidence backs the thing you asked for. A free-text bio mention counts at ¼ of a stated skill — it separates someone from nobody without outranking a decade of commits. |
| `output` | 0.25 | Work they actually shipped, and how far it landed: repository stars and forks, publication citations. On-topic work counts fully; unrelated work at ¼, so a famous side project never outranks someone who built the thing you asked about. |
| `confidence` | 0.15 | How strongly the source stated it. GitHub scales this by repository count, so it carries much of the volume signal for developers. |
| `recency` | 0.15 | Age of the newest thing we can date — a repository still being pushed to, a paper published, dated evidence — on a 2-year half-life. Undated scores a neutral 0.5: most sources never state a date, and treating "unknown" as "ancient" would bury the entire GitHub corpus. Off-topic work never sets recency, or a side repo would make a dormant specialism look current. |
| `corroboration` | 0.10 | Independent sources making the same claim (`verification_state = corroborated`). Needs enrichment, and needs the sources to share a strong key. |
| `breadth` | 0.10 | How many sources know this person at all, split merges excluded. |

**Projects and publications are one signal on purpose.** Stars and citations
are the same kind of evidence — work put out and taken up — and scoring them
separately would mean a developer outranks a researcher for reasons of source
rather than merit.

Counts are log-scaled to saturation points (12 evidence rows, 4 sources, 5,000
combined stars + citations): the 20th repository should not outweigh everything
else. Ties keep filter order, so equal-evidence results stay stable across
pages.

A deliberately plain linear blend, not a learned model — every result can
explain itself, and `match_feedback` has to accumulate real judgements before
anything can be trained on them. Weights live in `WEIGHTS` in `rip/nlq.py`.

**Two-stage retrieval.** Ranking needs to see the field before it can pick a
winner, so the filter yields a candidate pool (`RIP_CANDIDATE_POOL`, default
250) that is scored and *then* paged. `total_matches` still reports the true
filter count; paging deeper than the pool is not a meaningful request of a
ranked list.

When more people match than fit in the pool, the pool is cut by a stored
per-person prior that is exactly the query-independent half of the score
(output, recency, breadth) plus how much of the query's topical evidence each
person carries. Measured against scoring *every* match on a 10k corpus, this
cut returned the true top 50 for all 30 broad benchmark queries; the previous
evidence-count cut recovered only 65% of the true top 10.

**Several concepts are scored as a mean, not a sum.** For "robotics and
computer vision", depth is the rarity-weighted (IDF) mean of each concept's own
evidence, so someone deep in one and absent from the other no longer outranks
someone solidly evidenced in both.

## Search index

`/v1/query` runs on a search index (`backend/rip/search_index.py`) rather than
`LIKE '%term%'` scans. Every person is broken into `(term, field)` postings —
name, aliases, topics, bio/title text, roles, organisations, places, country,
project technologies — and every filter is an index seek driven by its rarest
constraint, so cost follows the size of the answer rather than the corpus.

| 54 benchmark queries, 10k people | parse | execute | count | total |
|---|---|---|---|---|
| before (LIKE scans) | 6,999 ms | 5,675 ms | 3,191 ms | 15,865 ms |
| search index | 142 ms | 1,321 ms | 2 ms | 1,466 ms |

- **It maintains itself.** A session hook re-indexes every person whose
  Person, Evidence, Affiliation, Contribution or IdentityLink rows changed,
  just before commit and inside the same transaction — no ingest path can
  forget it, and a rolled-back change never reaches it. Bulk SQL that bypasses
  the ORM (`merge-orgs`, `purge-nonpersons`) re-indexes explicitly.
- **Built on upgrade.** `init_db` builds it once for an existing graph (~6 s per
  10k people) and rebuilds when `INDEX_VERSION` changes. `rip.cli reindex`
  rebuilds on demand.
- **Worldwide text** (`backend/rip/textnorm.py`): accent- and case-insensitive
  ("Zürich" = "zurich", "José" = "jose"), words in any script, Chinese/Japanese/
  Thai split into character pairs so a term matches inside a longer value.
  `backend/rip/geo.py` adds every common country name including endonyms
  ("Deutschland", "中国"), demonyms, and major world cities with their other
  spellings (München/Munich, Bombay/Mumbai, 北京/Beijing).
- **Whole words, not fragments.** "rust" no longer matches a bio about "trust",
  nor "java" a JavaScript repository.
- **A city implies its country** in the index: "researchers in India" finds a
  profile whose location only says "Bengaluru". Ambiguous cities (Cambridge,
  Hyderabad, Portland) imply nothing, and `Person.country` itself still holds
  only what a source stated.
- **Cost:** ~120 bytes a posting, ~6 KB per real profile. The read-only
  snapshot keeps the index when it fits the size limit and otherwise ships
  without it; search then falls back to the SQL filters, which return the same
  people more slowly.
- **Portable.** Every statement is plain SQLAlchemy; the full query path was
  replayed on PostgreSQL 18 with identical results.
- **`/v1/persons` uses it too.** The text filters (`q`, `skill`,
  `organization`, `education`, `current_organization`, `role`, `country`,
  `location`, `technology`) become one index lookup; the numeric and
  structural filters apply to what it returns. 14 filter combinations on 10k
  people: 2,332 ms → 167 ms, same people (a test holds the two paths equal).
  The loose `skill=go*` form still uses SQL. Facet counts are cached per
  database and invalidated by writes.

## Records that are not people

Sources hand over things that are not people — web page titles ("20+ Deep
Learning Projects for Beginners", job posts, "Careers at Millennium"), GitHub
*User* accounts that are communities ("Deep Learning Türkiye"), and degenerate
author strings ("D. ."). `backend/rip/personhood.py` judges each name at ingest:

- **Page titles of real people are cleaned, not dropped:** "Dhruv Dixit's
  Profile | YMGrad" is stored as *Dhruv Dixit*. The raw payload keeps the
  original.
- **Clear non-people are refused before anything is written**
  (`NotAPerson`): a topic phrase or entity word inside the name, nothing that
  reads as a name, or — for web pages only — a title made mostly of ordinary
  words.
- **Anything short of a clear signal is kept.** Rejecting a real person loses
  them silently, so names like "Deep Singh" or "Learning Chen" pass.

The same check stops a stored community record from turning a topic into a
name filter. To clean a graph ingested before the check existed:

```bash
python -m rip.cli purge-nonpersons          # dry run: what would be removed / renamed
python -m rip.cli purge-nonpersons --yes    # apply
```

On the real graph this removed 15 of 463 records and cleaned 18 names.

## People stored more than once

Ingest merges on strong keys, or a near-identical name plus a shared
organization. Two kinds of duplicate survive that: one person across sources
with no shared identifier (Dhruv Dixit on OpenAlex, Semantic Scholar and dblp),
and one author split into several IDs by a scholarly index (OpenAlex does
this). `backend/rip/dedupe.py` merges them — **on evidence, never on a name**:
the same graph holds four different Rahul Guptas.

| | |
|---|---|
| veto | different ORCIDs; different IDs from a source that disambiguates people (dblp, ORCID, GitHub, Stack Overflow, Hugging Face); unrelated fields with nothing else shared; a pair rejected in review |
| merge | identical full name, no veto, and shared papers or a large share of co-authors — compared by full name, from papers with at most 25 authors |
| review | plausible but short of proof, initials-only names, or a record that matches a *different* person almost as well → the merge-review queue |

Clusters are complete-link, so one ambiguous record cannot chain two people
together. Every merge is an ordinary merge: reversible with `review split`.

```bash
python -m rip.cli dedupe          # dry run: proven merges and review candidates
python -m rip.cli dedupe --yes    # merge, queue reviews, repeat until stable
```

On the real graphs: 2 and 4 merges, every one backed by shared papers or
dozens of shared co-authors; the rest went to review.

### The review queue holds questions, not noise

Ingest queues a pair whenever two names look alike, and a name is not
evidence. One graph's queue reached 196 pairs, of which 23 were decidable:
the rest were one "Karan Singh" against thirty separate "K. Singh" records
sharing no paper, no co-author and no organization. `review triage` judges
every pending pair by the same evidence rules and sorts it:

| | |
|---|---|
| merged | proof, and the whole-group sweep agrees (so a record that fits two people equally well is not merged) |
| rejected | a veto: different ORCIDs, or a source that disambiguates people listing them as two |
| deferred | no evidence either way — **not a verdict**: the pair leaves the queue and returns to it by itself if either person later gains evidence |
| pending | left for you, because the evidence is real but short of proof |

```bash
python -m rip.cli review triage        # dry run: what it would decide, and why
python -m rip.cli review triage --yes  # carry it out
```

Both real queues: 196 → 23 and 55 → 28 pairs waiting, no person merged by it.
The flood is also stopped at the source — a name-only near miss now needs a
name that identifies someone on both sides, so "K. Singh" and a bare "Rahul"
are never queued against anyone. A pair with a *shared organization* still is:
that is evidence.

## What the words in a query are allowed to mean

A query is matched against the vocabulary the corpus actually holds. Four
rules decide what a term may become, all of them from queries that came back
wrong on the real graph:

- **A phrase nobody works on is reported, not answered with one of its
  words.** "graph neural networks" used to return wireless sensor network and
  VANET researchers, because the bare word "networks" matched them. Now the
  longest sub-phrase that means something wins ("neural networks"), the
  phrase itself is reported in `unmatched_terms`, and a lone word out of it
  never becomes a filter. A word that is real vocabulary on its own ("Python"
  in "Python developers") is untouched.
- **Number does not decide who is findable.** Words are compared by a crude
  stem, so "distributed system" and "distributed systems" reach the same
  people, and either reaches "Distributed systems and fault tolerance". Before
  this the singular found nobody at all, and "neural network" found the values
  spelled that way while missing "Neural Networks and Applications" beside
  them. An exact hit on a phrase widens the same way, or the exact spelling
  would return *fewer* people than a misspelling of it. The index stores the
  same stems for the fields where number is not identity — skills, bios,
  roles, technologies (`search_index.STEMMED_FIELDS`) — so free text follows
  the same rule: a bio reading "recommender systems" answers "recommender
  system". Names are deliberately excluded: "Rogers" is not "Roger".
- **A typo inside a phrase is repaired.** The per-word typo pass compares a
  word against whole vocabulary *values*, so it fixed "bangalor" but never
  "distributed sytems" — which used to apply the bare word "distributed" and
  reach Distributed Processing. Words are now also compared against the
  vocabulary's *words*, one transposition included ("learnign", "netowrks",
  "robotcis"), and a repair is used only when it makes the phrase match
  something real. The substitution is reported in `corrections`.
- **A word the vocabulary uses is a subject, not a surname.** A GitHub account
  display-named "Graph" turned "graph neural networks" into a name search.

`scripts/benchmark_queries.py` has a `variants` group that keeps these honest:
each singular/plural pair must return the same people, and each typo must
reach them too.

Set `RIP_VOCAB_TTL` (default 60s) to tune how long the query vocabulary is
cached — it is four `SELECT DISTINCT`s over the corpus, and rebuilding it per
request costs more than everything else in a query put together.

## Understanding a question

The rules above decide what a *word* may match. These decide what a
*question* means. Each one reports what it did in the response, so an answer
never quietly belongs to a different question than the one asked.

- **Subjects, not only strings** (`rip/concepts.py`). A broad subject reaches
  the topics under it: "deep learning" reaches Neural Networks and
  Applications, and "oncology" reaches cancer topics. OpenAlex's own
  topic → subfield → field filings count at half weight, so someone filed
  under Computer Vision still counts for "computer vision" with no topic spelled
  that way. Related subjects count at half weight too, capped at one topic's
  worth, so three neighbouring topics never outrank the subject itself.
  Reported in `rewrites`.
- **People nouns become their subjects.** "physicists" searches physics,
  "roboticists" robotics, "data scientists" data science (`rewrites`, `how:
  "people noun"`). Job titles such as "community managers" are still titles.
- **Organizations by any of their names.** "Google" is also Google (United
  States) and Google DeepMind (United Kingdom); "IIT Bombay" and "MIT" match
  by acronym. An exact country name is checked first, so "UK" is the United
  Kingdom and not the University of Karachi. "US" and "U.S." are the country,
  but "help us find" is not.
- **Names as people write them** (`rip/names.py`). Transliterations (Agarwal /
  Aggarwal / Agrawal) are one name for both search and entity resolution.
  Look-alikes (Katherine / Kathryn) and nicknames (Bill → William) are accepted
  by search only, because two such records at one institute are two people as
  often as one.
- **Too few full matches means partial ones, labelled.** When fewer than 10
  people meet every part of a query, the page is topped up with people who
  meet most of it, placed behind the full matches. Each one carries
  `match: "partial"` and `missing: [...]`. A place or employer gives way before
  the subject, and a name never does.
- **Protected attributes are never applied.** "women in machine learning"
  searches machine learning and reports `protected_terms`. This holds inside
  an exclusion too: "but not women" excludes nobody.
- **Compound questions.**
  - *Both:* "worked at both Google and Microsoft" requires every organization
    named (`require_all_orgs`). Without "both", "at MIT and Stanford" stays a
    choice.
  - *Negation:* "not at Google", "who don't work at Google", "excluding
    Stanford", "but not computer vision", "not in India". The negated phrase is
    parsed like a query of its own, and anyone matching it is left out
    (`exclusions`). A subject's related topics are not excluded with it, unless
    they are all the corpus holds for it. "Not just ML" widens a question rather
    than excluding anything. An exclusion never gives way when a query is
    relaxed.
  - *Counts:* "at least 20 papers", "50+ publications", "over 1,000 citations",
    "cited more than 2k times" (`min_publications`, `min_citations`; "over N"
    means N+1 or more). A bare number is not a threshold. The totals are the
    larger of what a source reports for the whole author (OpenAlex, Semantic
    Scholar, dblp) and what is stored. Stored works are only each person's
    most cited. A threshold alone is a valid question: "people with at least
    50000 citations".

**Citations are compared within a field.** A citation count means different
things in cell biology and in mathematics. The `output` signal is scaled by
how a person's primary OpenAlex field is cited, relative to the whole corpus.
The factor is the square root of that ratio, shrunk toward the corpus median
for thinly populated fields and bounded to 0.5–2×.

## Measuring search quality

`backend/evaluation/judgments.json` holds judged queries, and
`scripts/eval_ranking.py` scores the parser and ranker against them: nDCG@10,
P@10 and recall@50. A judgment is a *criterion*, such as "a stated topic
mentioning tuberculosis grades 2", not a list of people. The grader applies it
to everyone in the corpus, so recall is measurable and new people are graded
without re-judging.

```bash
cd backend
python scripts/eval_ranking.py --db sqlite:///corpus-copy.db --save run.json
python scripts/eval_ranking.py --db sqlite:///corpus-copy.db --compare run.json  # lists queries that got worse
python scripts/eval_ranking.py --show t-tb     # the ranked list, graded
python scripts/eval_ranking.py --audit t-tb    # everyone the criteria call relevant
```

Always run it against a copy of the database, because it initializes and
reindexes. On the 709-person corpus, after the criteria review below:

| Set | Queries | nDCG@10 | recall@50 |
|---|---|---|---|
| tuned (topic, concept, agent, constrained, soft, typo, name, protected) | 52 | 0.89 | 0.74 |
| `holdout`: written mid-way, partly tuned after | 16 | 0.86 | 0.65 |
| `holdout2`: written last, criteria fixed before the first run | 20 | 0.88 | 0.63 |

Overall: nDCG@10 0.88, P@10 0.74, recall@50 0.69. **Read recall@50 against its
ceiling, which is 0.96, not against 1.0**: ten queries have more than fifty
relevant people, so no ranker can retrieve them all in fifty results.
`machine learning engineers` has 126, capping it at 0.40.
`scripts/audit_judgments.py` prints the ceiling, the share of the corpus each
criterion calls relevant, and `--terms`, the report that found the six criteria
below.

Two lines under the summary name what a figure does not cover, and they are
part of the result:

- **measuring the subject only.** `soft` says that when nobody meets a query in
  full, the subject without the constraint is the best answer left — the right
  behaviour for `deep learning researchers at Oxford` when no Oxford person
  does deep learning. But then every relevant person carries the same gain, any
  order of them scores 1.000, and a ranker that ignored "at Oxford" is
  indistinguishable from one that honoured it. Four queries are in that state.
  Each is still worth scoring, because relaxing to the subject is what should
  happen; none is evidence that constraints work. The count of people who meet
  the constraint *at all* is printed with it — eight are at Oxford, eighty in
  India — because an empty conjunction and an empty corpus are different
  failures.
- **grading nobody.** A query the corpus cannot answer produces `None` for
  every figure and averages into nothing, so it sits in the set looking like a
  measurement. `n-aggarwal` did that unnoticed; it has been removed, and the
  condition is now named rather than left to be found again. The list is
  empty today.
- **subject ingested after failing.** Two queries about headache research
  scored nothing because the corpus held no headache researcher at all. People
  for that subject have since been ingested, so both now score 1.000 — on a
  corpus changed on their behalf. That is a coverage result and the run says
  so; see below.

### The criteria are drafts, and five of them were wrong

Every criterion here was written by the machine that also built the ranker,
which is the standing risk: when both make the same mistake, the benchmark
certifies it. On 2026-09-22 all 89 were audited against the corpus they grade,
and six graded the wrong field, all through a one-word `strong` term that means
different things in different places:

| query | term | wrongly graded 2 |
|---|---|---|
| `neuroscientists` | `neural` | 28 of 50 — neural *networks* |
| `chemists` | `synthesis` | 19 of 34 — image, speech and protein synthesis |
| `computational pathology` | `pathology` | 7 of 11 — pathology as *disease process* |
| `neuroscience` | `brain` | 13 of 25 — brain *metastases*, so oncologists |
| `statisticians` | `statistical` | 4 of 16 — statistical *mechanics*, and the DSM |
| `renewable energy researchers` | `solar` | 3 of 12 — solar *plasma physics* |

Geoffrey Hinton graded 2 — fully relevant — as a chemist and as a
neuroscientist. `neuroscientists` and `neuroscience` both scored a perfect
**1.000**, and corrected they score **0.52** and **0.50**: the ranker matched
those queries on "neural" and "brain" too, so the criterion and the ranker
agreed on the same error and the benchmark called it perfect. The loose
criteria were not noise; they were concealing a real search defect.

The soft rule hid a third perfect score the same way. It graded a person
relevant to `deep learning researchers at Oxford` for two paper titles
mentioning deep learning while not being at Oxford — two levels of weak
evidence adding up to relevant, and 61 of the 102 people it called relevant
were neither. The fallback now needs the subject *stated* as a topic. That
query scored **1.000** and now scores **0.92**; `computer vision researchers in
China` scored 0.31 and now scores **0.10**.

Fixing them cost 0.01 nDCG overall, so the headline claim survives — but four
of the six are `holdout` or `holdout2`, whose whole value was that nobody had
touched them after seeing results. They have been touched. Each now carries a
`tuned` field saying so, and **their numbers can no longer be read as
untouched-holdout numbers.** The 31 holdout queries without a `tuned` field
still can. `tests/test_evaluation.py` pins each confusion so it cannot return.

### Labels nothing in Seekr wrote

The criteria above grade people from the same stored words the ranker reads.
To check both from outside, `scripts/draw_relevance_pool.py` drew 20 of the
judged queries and pooled 321 people: Seekr's top ten, a sample the criteria
call relevant, a sample holding every word of the query, and three at random.
Each card was graded 0, 1 or 2 by reading it — topics, paper titles, role,
place — without knowing how it entered the pool.

The standard is a person's, not this project's. Their 14 grades over two
calibration rounds set the rule (2: something on the card is in the subject or
a close neighbour; 1: the subject shows only as skill tags, or the place asked
for is not shown; 0: nothing related); the other 307 cards were graded to that
rule and sealed by hash before a blind check. On 10 fresh cards the person
agreed on 7, short of the 8 set in advance, with every miss one grade apart.
They reviewed the three and accepted the labels, and
`evaluation/relevance_labels.json` records both. So these labels measure
whether a match is **plausible**, as one person judges it, and they rest on
that acceptance rather than on the blind check.
`scripts/score_relevance_labels.py` scores them:

| | cards | graded 2 | graded 1 or 2 |
|---|---|---|---|
| Seekr's top ten | 180 | **0.69** (0.62–0.75) | 0.96 |
| what the criteria call relevant | 153 | 0.72 | 0.92 |
| every word of the query | 45 | 0.98 | 0.98 |
| random | 60 | 0.12 | 0.15 |

- **On 13 of the 20 queries Seekr's top ten is 115 of 120 graded 2.** All the
  rest come from two kinds of query.
- **Developer queries** (`web`, `Python`, `JavaScript developers`): 0 of 30
  graded 2, 27 graded 1. These people are Stack Overflow users who show the
  subject only as tags, and the rule grades that 1. The pools hold one person
  graded 2 for web developers, one for JavaScript and none for Python, and
  Seekr's top ten has neither of the two.
- **"in India" queries**: 9 of 30 graded 2. Seekr ranks everyone who meets the
  place first; every person the criteria call relevant is in ranks 1–4. The
  rest of the page is topped up with people flagged as not in India, and those
  are the 1s. This is the designed behaviour working, not a defect.
- **The word baseline ties Seekr where it answers at all.** It found someone
  for only 5 of the 20 queries, all single-subject ones; there it scores 44 of
  45, and Seekr scores 48 of 49 on the same five. These labels show no case
  where Seekr picks better than a plain word match. What Seekr adds is
  answering the other 15.
- **The criteria agree with the labels on 58% of cards.** They call 9 cards 2
  that the labels call 0, and 26 cards 0 that the labels call 2. They miss far
  more than they invent, so the recall figures above are understated more than
  they are inflated. On Seekr's top ten the two are close: 0.74 graded 2 by the
  criteria and 0.69 by the labels.
- **Recall is the open question.** Of the people labelled 2 anywhere in a
  query's pool, Seekr's top ten holds 0.60 on average. That is an upper bound
  on real recall at ten. Three Indian machine-learning researchers whose cards
  show the subject only in paper titles were not among Seekr's matches at all.

The last row of the first table is the number to believe for queries nobody has
tuned for. Its
misses were mostly subjects the corpus had no people for — a coverage problem,
not a parsing one. `scripts/ingest_topics.py` fills those from the free
topical sources:

```bash
python scripts/ingest_topics.py --per-topic 8          # what it would ingest
python scripts/ingest_topics.py --per-topic 8 --yes    # do it
```

Twelve such subjects (112 authors, four minutes) took eleven of them from no
answer at all to a page of the people you would expect, and moved `holdout2`
from 0.69 to 0.80 on the enlarged corpus. The other sets appear to fall on
that corpus, which is worth reading carefully: the criteria name particular
topic strings, and new people arrive under topic names the criteria never
enumerated — Fiji and QuPath authors rank for "computer vision researchers"
and grade 0. Numbers from two different corpora are not comparable, and the
judgments need review before they are.

### Filling a coverage gap is not the same as getting better

`headache and migraine researchers` scored **0.000** and `headache researchers
in Norway` had every figure withheld, because the corpus held **no headache
researcher at all**. That is a coverage gap, not a parsing one, and
`scripts/ingest_topics.py` is what fills it:

```bash
python scripts/ingest_topics.py --topic headache --topic migraine --per-topic 10
python scripts/ingest_topics.py --topic headache --topic migraine --per-topic 10 --yes
```

Fourteen people, 709 → 723. The subject was ingested, **not the query**: asking
for headache researchers *in Norway* would be answering the benchmark rather
than filling the gap it found. Norway arrived by itself, because Norway really
is a centre of headache research — Stovner and Hagen at NTNU Trondheim are in
the first ten OpenAlex returns for the subject. Both queries now score 1.000,
and `headache researchers in Norway` reaches recall 1.000.

**And the honest number is the one that did not move.** `holdout2` went 0.81 →
0.87, which looks like search improving. Excluding the two queries whose
subject was ingested:

| holdout2 | before | after |
|---|---|---|
| all 20 queries | 0.8144 | 0.8735 |
| the 18 nobody fed | 0.8596 | **0.8594** |

The whole gain is the two fed queries. Nothing else moved, because nothing
else changed: search did not get better, two unanswerable questions became
answerable. Both cases carry a `covered` field recording it and the run prints
a line for each, for the same reason `tuned` exists — a number obtained after
the corpus was changed on a query's behalf is not evidence that search
generalises.

Five queries got slightly worse, which is what a bigger corpus does: more
people compete for ten slots. `h-alz` (Alzheimer's) fell hardest, 0.43 → 0.37,
losing places to neurologists who now outrank it.

Three of the fourteen are not headache researchers at all — a thyroid
oncologist and two Wuhan clinicians whose COVID papers list headache as a
symptom. The criteria grade all three 0, so they cost nothing, but it is the
same polysemy that put seven disease-process clinicians into `computational
pathology`: a topical search matches the word, not the subject. They were left
in. Removing people because they are not what the benchmark wanted is how a
corpus stops resembling the world.

What changed is the next pass. `ingest_topics.py` now puts each fetched
profile to `ingest.on_subject` before storing it — the grader's own rule: the
person states the subject, or more than one of their titles is about it. The
ones it declines are printed with the topics they do state, so the call can
be argued with, and `--loose` stores whatever the source returned, as before.
The three already stored stay, for the reason above.

### A corpus typo defeats the typo tolerance

`reinforcment learning` scored **0.000** while `reinforcement learning
researchers` scored 1.000. Four ORCID profiles have the keyword misspelled —
what those four people typed about themselves, and ORCID keeps it as typed.
That made them invisible under the right spelling, which is the obvious half.
The other half is worse: the misspelling *was* a vocabulary term, held by real
people, so a query repeating the typo matched it **exactly**, the
typo-correction pass never ran, and the query answered with those four instead
of the sixty who do reinforcement learning.

The repair runs at ingest (`rip.ingest.correct_spelling`), not as a one-off
UPDATE, because `_retract_evidence` deletes any row its source record no
longer asserts: correcting the stored value works the day you do it and is
deleted by that profile's next refresh. `scripts/fix_spellings.py` fixes rows
already stored. The row records what the source said and the payload keeps it
verbatim, so nothing is rewritten out of existence.

It is a **named list**, not an edit-distance rule, and the corpus is the
argument. Of the 75 pairs of words one edit apart in these keywords, 74 are
not typos: mostly plurals (`technique`/`techniques`), some spelling variants
(`modelling`/`modeling`), and several different words — `generics`/`genetics`,
`ischemia`/`ischemic`, `microscope`/`microscopy`, and `material`/`maternal`,
which escaped being "corrected" into each other by a single holder. Exactly
one is a typo. Telling those apart takes a dictionary, not a distance.

### Widening a subject: what worked, and two things that did not

A query reaches people whose stated topic contains the words typed, and
nothing else unless `concepts.CONCEPTS` knows a wider set.
`"Alzheimer's researchers"` found the one person whose topic says Alzheimer's
and missed the dementia researchers beside them. The map is a hand-written
list of 77 subjects, which does not reach worldwide, so I tried to replace it
twice and measured both.

**Co-occurrence** — subjects held by the same people are related. At this
corpus size it is noise: 574 of 2,551 terms had any neighbour at all, none of
the subjects actually being missed had one, and `deep learning` came out
related to **ophthalmology**, because the handful of people carrying that
literal keyword happen to be retina-AI researchers. It encodes who is in the
corpus, not what the subjects mean.

**OpenAlex's own hierarchy** — every topic in a stored payload carries a
curated `subfield`. Two problems. It covers **33%** of the vocabulary and
misses every term in question, because payloads carry only an author's top few
topics. And a subfield is a shelf: *"Wildlife Ecology and Conservation"* sits
in Ecology beside *"hydrology and sediment transport processes"* — the exact
parent-category mistake that had to be taken out of the `related` lists.

So the map stays a list, and `scripts/concept_coverage.py --judged` counts
what it reaches: **37 of 81 query terms have no related subjects**. Eight
entries were added for measured misses, plus a general fix — a trailing
possessive is now dropped, because `"Alzheimer's"` stemmed to `alzheimer s`
and matched nothing, and Parkinson's, Crohn's and Hodgkin's are named the same
way.

nDCG@10 0.840 → 0.870, recall@50 0.659 → 0.694, no query worse,
`toxicology` 0.000 → 0.951 and `Alzheimer's researchers` 0.365 → 0.804.

**Four of those are holdout queries whose failures I read before writing the
entries.** `holdout` nDCG went 0.735 → 0.863 and most of it is those four.
They carry an `informed` field and the run prints a line for each: the
criteria did not change, the *search* did, knowing the query. That is a third
way a number stops meaning what it looks like, alongside `tuned` and
`covered`.

### Source payloads are stored compressed

Every source record keeps what the source actually sent, and that is not
waste — the payload is the evidence, and this codebase reaches for it
constantly: whether a record is a person, what a source really said before a
keyword was repaired, which author id OpenAlex meant. It was also **89% of the
database**, 199 MB of 248 MB for 767 people.

`models.CompressedJSON` deflates payloads over 2 KB on the way in and inflates
them on the way out, including for column-level selects. Base64 gives back a
third of the gain, so the net is about six to one. **The whole database went
248 MB → 60 MB**, and `scripts/eval_ranking.py` returns bit-identical numbers
against it.

The column type is unchanged — migrations here are additive only, and a JSON
column already holds text on both engines — so there is no migration and no
moment where old and new rows cannot coexist: an uncompressed row simply reads
back as itself. `scripts/compress_payloads.py` rewrites the ones already
stored, dry-run by default, and you want `VACUUM` afterwards.

The cost is that a payload is no longer readable in a SQL browser. It is
readable through the ORM, which is how every part of this project already
reads it.

### A filing category is a shelf, not a subject

`toxicology` returned nine people and none of them were toxicologists —
nDCG 0.000, the worst query in the set. OpenAlex files people under labels
like *"Pharmacology, Toxicology and Pharmaceutics"*, those labels are in the
search vocabulary so that `chemistry` reaches the people filed under
Chemistry, and containment let **any word** in a label answer for it.

**The comma separates a shelf from a subject.** A label that enumerates
subjects holds people doing any one of them, so matching one it merely lists
tells you nothing; a label without a comma — *"Cellular and Molecular
Neuroscience"* — is one subject with modifiers, and everybody under it is
doing that subject. For an enumerating label the query has to name what it
**leads** with, which is its principal subject.

Two blunter rules were tried and measured first, because both look obviously
right: requiring the whole label cost `radiologists` **0.915 → 0.000**, and
requiring the front of *every* field label cost `neuroscience`
**0.498 → 0.284**. Both are pinned in `tests/test_field_labels.py`.

Result: nDCG@10 0.838 → 0.840, precision 0.864 → 0.876, recall unchanged, no
query worse, and `diabetes researchers` 0.863 → 1.000. `toxicology` now
returns nothing rather than nine wrong answers, which the run reports as a
zero-result query with relevant people — a coverage gap stated instead of
disguised.

### And `related` is the same defect one level down

`strong` says what the query is about; `related` is meant for subjects *next*
to it, worth partial credit. A **parent category** is not that, because it
admits every sibling. `scripts/audit_judgments.py --related` prints, for each
related term, how much of a query's relevant set it admits on its own, and
eleven queries had one term admitting half or more:

| query | related term | admitted |
|---|---|---|
| `fungal infection researchers` | `infectious disease` | **73%** — tuberculosis, Ebola, leprosy |
| `toxicology` | `pharmacology` | **67%** |
| `reinforcment learning` | `machine learning` | **66%** |
| `reinforcement learning researchers` | `machine learning` | **64%** — computer vision, NLP |
| `renewable energy researchers` | `energy` | **61%** — plasma energy, energy metabolism |
| `tuberculosis` | `epidemiology` | **60%** — every epidemiologist |
| `recommender systems` | `topic modeling` | **56%** |
| `AI in healthcare` | `machine learning` | **55%** |
| `maternal and child health` | `public health` | **53%** |
| `wildlife conservation` | `ecology` | **50%** — insect, polar and marine ecologists |
| `graph neural networks` | `neural network` | **51%** |

All eleven corrected, each carrying a `tuned` note. The rule is not "drop
every related term": topic modelling really is an information-retrieval
technique, cosmology and particle physics really do overlap, and robotics is
where reinforcement learning is applied — those stay, and a test says so.

The effect is almost entirely on **recall**, which is the tell. nDCG@10 barely
moved (0.839 → 0.838) because the ranker was not returning those people
anyway; recall@50 went **0.628 → 0.659** overall, and `holdout2` 0.566 →
0.634, because the denominator stopped containing people the query was never
about. `reinforcement learning researchers` counted 66 relevant people and now
counts 25.

With the four rows repaired, `reinforcment learning` scores 1.000 and
`holdout2` — which nobody tuned for, and whose two reinforcement-learning
criteria were never touched — went from 0.76 to 0.81. No query got worse.

`scripts/verify_postgres.py` runs the engine-specific query paths (exclusions,
"both", count thresholds, the v9 migration, and the compressed-payload round
trip) against a real Postgres and compares the answers with SQLite's; point
`RIP_TEST_POSTGRES_URL` at a database it may create and drop tables in. Run
against PostgreSQL 18.3: **all query paths agree with SQLite**, which until
now was a claim nobody had checked in a while.

The eval corpus is small, so a single query moves a set's score by several
points, and the judgments remain drafts: the audit above found six bad
criteria and there is no reason to think it found the last one.

## Search filters

`GET /v1/persons` accepts these, combined with AND. Every one is a *filter*;
`sort` only reorders by a factual field and never by fitness for a role.

| Parameter | Matches |
|---|---|
| `q` | name or alias substring |
| `skill` | skill, research interest or specialization (substring) |
| `organization` | any affiliation, current or past |
| `current_organization` | present employer only |
| `education` | where they studied (`studied_at` affiliations) |
| `role` | job title, current or historical |
| `country` | ISO-3166 alpha-2 (`IN`, `US`, `DE`) — structured, not string matching |
| `location` | free-text place |
| `source` | has a record from this source (`github`, `orcid`, …) |
| `technology` | technology used in one of their projects |
| `min_publications` / `min_citations` | scholarly output thresholds |
| `min_sources` | corroborated across at least N sources |
| `active_since` | published since `YYYY` or `YYYY-MM-DD` |
| `updated_since` | Seekr record changed since a timestamp |
| `has_cv` / `has_email` | has a published CV link / public email |
| `sort` | `relevance` (insertion order), `recent`, `name` |
| `limit` / `offset` | paging; response carries `total_matches`, `has_more`, `next_offset` |

`GET /v1/facets?field=country|source|organization|skill|role` lists the values
actually present in the corpus with people-counts, so a UI can build filter
menus from live data instead of a hardcoded list.

## Connectors

| Connector | API | Identifier | Notes |
|---|---|---|---|
| `github` | api.github.com (official REST) | username | languages → skill evidence, top repos → projects, company → affiliation, blog/twitter → resolution links |
| `openalex` | api.openalex.org (fully open) | author ID | covers the scholarly graph (arXiv, journals, conferences); ORCID → strong identity key; topics → research interests; top-cited works → publications |
| `orcid` | pub.orcid.org public API (no key) | ORCID iD | employment/education history with roles + dates, keywords → research interests, researcher URLs → resolution links, works → publications (deduped by DOI against OpenAlex) |
| `stackoverflow` | api.stackexchange.com v2.3 | numeric user ID | top answer tags → skill evidence weighted by answer volume, website → resolution link. `STACKEXCHANGE_KEY` raises daily quota 300 → 10k |
| `dblp` | dblp.org (open, no key, 2s courtesy interval) | dblp PID | CS bibliography: homepage/Scholar/Wikipedia/Wikidata URLs → strong resolution keys, affiliations, awards, publications with co-author PIDs → discovery |
| `europepmc` | ebi.ac.uk Europe PMC REST (open, no key, no email) | **an ORCID** | medicine and biology: MeSH terms + keywords → research interests, institution read out of the affiliation string, articles → publications. Only authors publishing with an ORCID can be ingested, so every record merges onto the person who already holds it |
| `huggingface` | huggingface.co/api (public, no key) | username | org memberships → affiliations, models/datasets → projects, pipeline tags + libraries → ML skill evidence |
| `semanticscholar` | api.semanticscholar.org (official Graph API, no key; `SEMANTIC_SCHOLAR_API_KEY` for higher limits) | author ID | homepage → resolution link, affiliations, h-index/citations as evidence, papers deduped by DOI. Live search asks it about a *subject* (authors of papers on it, large collaborations skipped) only when a key is set — the shared pool answers paper search with 429s more often than not |
| `exa` | api.exa.ai (**paid**, `EXA_API_KEY`) | Exa person id | the only source reaching non-academic roles: job title, employer, work history, location. **Records are LinkedIn-derived** — see the note below |
| `web` | a public page, plus up to 3 same-site pages it links to as About, CV, Publications or Research (`RIP_WEB_MAX_SUBPAGES`) | URL | robots.txt honored for every page; JSON-LD Person, Open Graph, ORCID, displayed emails, links to known profile hosts, CV links. The page itself speaks first — a subpage only fills what it left out. Subpages are fetched directly, never through a paid renderer. Not a crawler |

### Auto-enrichment

Every ingest follows the identity signals it finds — an ORCID in a GitHub bio,
an OpenAlex ID on an ORCID record, a homepage on a dblp page — up to 3 hops,
skipping sources already stored and never letting a failed hop fail the root
ingest (`backend/rip/enrich.py`). So `ingest github <user>` can yield a GitHub +
ORCID + OpenAlex + homepage profile in one command. Disable with
`--no-enrich`.

Adding a source = one file in `backend/rip/connectors/` emitting a
`NormalizedProfile` (see `backend/rip/normalize.py`) + one registry line in
`backend/rip/connectors/__init__.py`. Nothing in the core pipeline changes.

The base connector (`backend/rip/connectors/base.py`) provides polite HTTP: minimum
request interval, retry with backoff on 5xx, rate-limit detection
(429 / `x-ratelimit-remaining`) honoring `Retry-After`.

### Finding people who aren't researchers

Nine of the ten connectors index scholarly or open-source output, so someone
with no publications and no public code is invisible to them. `exa` is the
exception and the only way Seekr answers "product designers at Swiggy".

**Know what you are ingesting.** Exa's person records are largely derived from
LinkedIn profiles. Seekr does not crawl LinkedIn — Exa is queried under a
commercial agreement and is responsible for its own index — but these are
personal records about identifiable people who did not consent to being in
your database. The originating URL is stored on every record so provenance is
never hidden, and `exa` is tried **last** in live discovery so the free
scholarly sources answer first and you only spend money when they cannot.
Decide your lawful basis and retention policy before ingesting at volume.

### Live search feeds the graph (pay once, not per query)

When a live provider returns a whole person record — Exa does — that record is
ingested immediately. The data is already bought; discarding it would mean
paying for the same people again next week. Two consequences:

- results appear as ordinary search results, not just as suggestions, because
  the query is re-parsed after storing (a company we had never heard of is a
  real filter once its people are in the graph)
- a `search_cache` row records each provider/query pair, so repeating a query
  within `SEEKR_SEARCH_TTL_DAYS` (default 7) skips the paid call entirely

Keyed on your original query, not the leftover words — otherwise storing new
people changes the residual string and the same request bills twice.

The free scholarly sources (OpenAlex, Semantic Scholar, dblp) return only an
identifier, so their full profile has to be fetched — but that fetch is free,
so up to `MAX_FREE_FETCHES` (10) per query are pulled and stored too. A query
the corpus cannot answer therefore grows the corpus.

People found this way are returned as results and flagged `from_live_search`
(shown as a **new** tag in the UI). They deliberately bypass the corpus
filter: that filter can only express what the corpus already knows, so a
person fetched seconds ago would fail it and the round-trip would be wasted.

### How a live search runs

Sources that always run (ORCID, Wikidata, GitHub, Hugging Face, Stack Overflow,
web) start searching the moment a query arrives, in parallel with the
scholarly sources, and their profiles are fetched in one concurrent pool
across hosts as soon as they answer. Results are still stored one at a time,
in source order, on the request thread.

Each source has **one shared connector per process**, whose request spacing
is thread-safe: concurrent fetches to dblp still arrive two seconds apart, as
dblp asks. (Previously every fetch built its own connector and clock, so
parallel fetches ignored the spacing, and each paid a fresh TLS handshake.)
Fetches not started within `RIP_LIVE_BUDGET_SECONDS` (default 30) are skipped
and reported on their result.

Measured against the live public APIs, same queries, same people stored:

| query | before | after |
|---|---|---|
| protein folding researchers | 20.7 s | 8.0 s |
| coral reef ecology researchers | 22.4 s | 10.4 s |
| Yann LeCun | 14.2 s | 8.5 s |
| Fei-Fei Li | 12.5 s | 7.9 s |
| glaciology researchers (new code run first) | 12.5 s | 9.4 s |
| Daphne Koller (new code run first) | 10.6 s | 6.0 s |

**OpenAlex** is the slowest source, so the gains come from asking fewer
questions, and asking for less in each. Author records for a whole search load
in one request (`OpenAlexConnector.prefetch`); topical discovery reads authors
from papers with at most 25 authors, falling back to all papers when a field
has only large collaborations. Measured on six fresh live queries: 10.6 s →
8.7 s on average, 18.4 s → 11.3 s for a collaboration-heavy topic.

Every request then names the fields it needs (`select=`), which is where the
rest went. OpenAlex returns a full record by default — abstracts, reference
lists, yearly counts, concept vectors — and none of it is read. Alternating
trials against the live API:

| request | full | with `select` |
|---|---|---|
| one author's works | 1.54 s / 292 KB | 1.08 s / 136 KB |
| topical works search | 2.22 s / 1.2 MB | 1.60 s / 232 KB |
| ten author records | 0.91 s / 23 KB | 0.53 s / 7.5 KB |
| **whole live search, mean of 6** | **9.6 s** | **7.8 s** |

The field lists are `AUTHOR_FIELDS` and `WORK_FIELDS` in the connector, and a
field added to `normalize` has to be added to them or it arrives as `None` — a
test builds a profile from a `select`-shaped payload to catch that. Splitting
the works request in two to drop consortium author lists was tried first: 50×
smaller payloads, but response time is roughly flat per request, so two
requests per author were slower than one. Reverted; `select` is the version of
that idea that keeps the request count.

`OPENALEX_MAILTO` joins OpenAlex's polite pool (higher, steadier limits). The
address is sent in every request URL, so use one you are happy to publish, or
leave it empty and stay on the anonymous pool. An address on the reserved
example domains is treated as the unedited placeholder and not sent.

### Europe PMC: coverage, not speed

Europe PMC was added because OpenAlex sets the pace of a live search and
answers medical questions thinly. What it is actually worth, measured on the
same queries:

| | without | with |
|---|---|---|
| "cardiologists studying arterial stiffness" | **0 people**, 3.7 s | **6 people**, 12.9 s |
| "soil microbiome researchers" | 15 people, 6.3 s | 18 people, 9.8 s |

So it is a coverage source, not a faster one: its median search is about 1.4 s,
comparable to OpenAlex, but its tail is not — the same request for "soil
microbiome" has come back in 0.55 s and in 14.4 s. Three things keep that tail
from setting the pace:

- it runs in the **background phase**, beside OpenAlex rather than after it;
- the connector's `request_timeout` is **8 s**, not the default 30 — a live
  search would rather lose one source for one query than wait;
- each background source now **fetches as soon as its own search returns**.
  One shared wait for every search came first, and it cost whatever the
  slowest source that query happened to hit: Europe PMC taking 8 s held back
  five other sources' profile fetches that had nothing to do with it.

`RIP_SKIP_SOURCES=europepmc` turns it off without a code change, because what
a source is worth differs by graph.

It identifies people **by ORCID only**. Europe PMC publishes no author IDs,
and a name is not an identity here: the four Stéphane Laurents in its hearing
and medicinal-chemistry papers carry no ORCID between them, while 62 of their
co-authors do — returning nothing for that name is the honest answer. The
upside is that an ORCID is a strong key, so these records land on the person
who already holds it instead of queueing another review.

One guard worth knowing: an ORCID in the literature is not always one
person's. The ORCID documentation example `0000-0002-1825-0097` sits on papers
by a dermatologist in Nanjing and a computer scientist in Halifax, each having
pasted it into a submission form. Only articles whose entry for that ORCID
agrees with the commonest name are kept, so a mistyped identifier cannot fuse
two strangers into one person.

### Topical discovery, not surname matching

Asking OpenAlex for "Rust developers" by author name returns people *surnamed*
Rust. Topical queries instead search works and take those works' authors, so
"computer vision researchers" returns Zisserman, Simonyan and Szeliski. Author
name search remains the fallback when the topical search finds nobody.

Scholarly indexes also carry entity records that are not people — "Computer
Vision Syndrome", "European Conference on Computer Vision", lab names. These
are rejected before ingest rather than becoming persons in the graph.

### Web search and page rendering (free tiers)

These are infrastructure, not people sources: they find URLs and fetch pages.

| Provider | Env var | Free tier | Used for |
|---|---|---|---|
| TinyFish | `TINYFISH_API_KEY` | Search + Fetch free, no credits | finding homepages first; rendering JS-heavy pages first. **Never Agent/Browser** |
| Tavily | `TAVILY_API_KEY` | 1,000 credits/month | finding homepages |
| SerpApi | `SERPAPI_API_KEY` | 250 searches/month | homepage search fallback |
| Firecrawl | `FIRECRAWL_API_KEY` | 1,000 credits/month | rendering JS-heavy pages |
| ZenRows | `ZENROWS_API_KEY` | 5,000 credits/month | render fallback |
| ScrapingBee | `SCRAPINGBEE_API_KEY` | 1,000 trial credits | render fallback |

```bash
# find homepages for people who have none, then read them
python -m rip.cli find-homepages --limit 20 --min-sources 2
```

Each is skipped when its key is unset, so a default install makes no
third-party calls. **robots.txt is always checked before any renderer** — the
render services exist for pages that are technically hard, never to reach a
page whose owner disallowed us.

### India-focused harvesting

The corpus is India-first. Two pipelines reach Indian people at scale:

```bash
# India-affiliated researchers (2.67M available in OpenAlex)
python -m rip.cli harvest openalex --india --out india.jsonl --limit 30000 \
  --filter "works_count:>20"
python -m rip.cli bulk-ingest openalex --file india.jsonl

# India-located developers (608k on GitHub; needs GITHUB_TOKEN for volume)
python -m rip.cli harvest github --india --out india_gh.jsonl --limit 5000
python -m rip.cli bulk-ingest github --file india_gh.jsonl
```

GitHub caps any one search at 1,000 results, so `harvest github --india`
slices by city (25 hubs from Bengaluru to Bhubaneswar) and then by follower
band within each city. OpenAlex uses `last_known_institutions.country_code:IN`
and has no such cap — resume a long harvest with `--cursor`.

**Sources deliberately excluded after checking:** Vidwan (INFLIBNET's national
expert database) publishes `robots.txt` with `Disallow: /` for every agent
except Googlebot, so it is off limits despite being the ideal source.
Shodhganga's OAI-PMH endpoint is misconfigured upstream and unreachable.
LinkedIn and Naukri forbid automated access in terms you accept by using them.

### Candidate next connectors (in rough value order)

1. **Apollo (licensed API)** — professional/company data, where the plan permits
2. **Personal websites / blogs** — respectful fetch honoring robots.txt
3. **Kaggle** — requires API credentials
4. **arXiv API** — deliberately skipped for now: no author IDs (name-only
   matching is too weak for safe resolution) and OpenAlex already indexes
   arXiv papers

## Bulk discovery

`backend/rip/discover.py` mines existing source records for people not yet ingested
and queues them as `DiscoveryLead` rows (unique per source+identifier,
skipping anyone already ingested):

- **OpenAlex co-authors** — raw-only, zero extra API calls
- **dblp co-authors** — raw-only, co-author PIDs from stored records
- **GitHub contributors** — bounded live calls: top 3 starred repos per known
  person, top 10 contributors each, bots excluded

`ingest-leads --limit N` drains the queue through the normal pipeline, so
discovery volume never outruns rate limits. Each lead keeps `reason` +
`discovered_via_record_id` — discovery itself is provenance-tracked. Running
discover → ingest-leads → discover repeatedly expands the graph one hop at a
time under your control.

## Compliance & responsible collection

**Protected attributes are redacted at ingest.** Free-text fields — bios,
headlines, roles, awards, locations — are screened before they become
queryable evidence, and material about someone's family, faith, health, age,
citizenship or gender identity is replaced with a named marker rather than
stored. The raw payload keeps the original, so provenance is intact and
nothing is silently rewritten; what changes is only what can be searched on.

The screening insists the mention be *about the person*. A researcher whose
stated interest is "how technology is affecting children and society" is
writing about a topic, not disclosing a family, and a colleague named
Christian is not disclosing a religion. `rip.cli audit-protected` scans what
is already stored and redacts in place with `--yes`.


- Official public APIs only; no scraping behind auth, CAPTCHAs, paywalls, or
  anti-bot measures — the base connector has no mechanisms for any of that.
- Rate limits honored (per-connector intervals + `Retry-After`).
- Only public professional data is collected; emails only when the person
  published them on their own profile. No sensitive-category data.
- Provenance retained for every claim, supporting deletion/audit requests
  (`DELETE person` cascade is a straightforward follow-up for GDPR-style
  erasure).

## Repository layout

```
backend/
  rip/
    db.py         engine/session, RIP_DATABASE_URL
    models.py     schema (person, source_record, evidence, edges, change_log, ...)
    normalize.py  NormalizedProfile IR + strong-key extraction
    resolution.py entity resolution strategies
    ingest.py     pipeline: upsert record → resolve → apply → change log
    nlq.py        natural-language query parsing and ranking
    discovery.py  live search of external sources, and storing what it finds
    search_index.py  postings index that /v1/query filters and ranks on
    textnorm.py   worldwide text folding and tokenisation (index + query)
    geo.py        countries, endonyms, demonyms, world cities
    api.py        FastAPI read API; serves the built UI at /ui and /static
    cli.py        init-db / ingest / search-openalex / refresh / serve
    connectors/   github.py, openalex.py, exa.py, base.py (polite HTTP)
  scripts/        snapshot build, Postgres migration, benchmarks, nightly cron
  tests/          resolution + ingestion tests (fixture-based, no network)

web/              the React app — this is what you edit
  src/
    main.tsx      entry; applies the saved theme before first paint
    App.tsx       routes (hash-based) and the auth gate
    api/client.ts the one door to the backend; bearer auth, 401 handling
    types.ts      the shapes /v1 returns
    pages/        Search, Person, Shortlists, Review, Sources, Gate
    components/   Shell, Filters, ResultsTable, Network, EmptyState
    lib/          icons, brand links, the traced mark, formatting, hooks
    styles.css    design tokens, layout, motion
  vite.config.ts  builds into ../frontend, proxies /v1 in dev

frontend/         BUILD OUTPUT — generated by `npm run build`, do not edit

demo/             the product film and the script that records it
  film.py         the running order, driven over the DevTools protocol
  capture.py      the headless Chrome harness
  scenes.py       title cards, captions, typing, frame recording
  post.py         push-in, dissolves, and per-frame timing
  sound.py        the score, synthesised
  mix.py          cues the score to the scene marks
  seekr-demo.mp4  the cut
```

The two halves talk over HTTP and nothing else. The frontend is a React +
TypeScript single-page app built by Vite; the backend serves the built files
as static assets and never renders HTML.

`frontend/` is committed rather than ignored because the deploy target runs
Python only — it never runs npm — so the built bundle has to be in the
repository for `/ui` to work in production. Treat it as an artefact: edit
`web/`, run the build, commit both.

### Working on the frontend

```bash
cd web && npm install
```

Two ways to run it. For frontend work, Vite's dev server gives hot reload and
proxies `/v1` to the API on port 8000:

```bash
cd web && npm run dev
```

For everything else, build once and let the backend serve it — same URL,
same `/ui`, no second process:

```bash
cd web && npm run build
```

`npm run typecheck` runs TypeScript with no emit, which is what CI should
gate on.

## Deploying

**Handing this to someone who will deploy it? Start at
[deploy/README.md](deploy/README.md)** — the database decision, a local
Postgres stack to try first, and Kubernetes manifests.

## Deploying with Docker

One image serves the API and the UI on port 8000. The UI is built from `web/`
inside the image, so what ships is what the source says, not whatever bundle
happened to be committed.

```bash
docker build -t seekr .
```

```bash
docker run -d --name seekr -p 8000:8000 \
  -v seekr-data:/data \
  -e RIP_API_TOKEN=<a long random string> \
  --env-file .env \
  seekr
```

The UI is then at `http://localhost:8000/ui`.

**Set `RIP_API_TOKEN`.** Without it every `/v1` *read* is open, and these
routes return personal data about real people. The container starts either
way and says which mode it is in on the first line of its log — read it.

Writes are the exception, and they are closed by default: with no token set,
`POST`, `PUT`, `PATCH` and `DELETE` on `/v1` are answered only for requests
from the machine itself. Serving reads to the world is a deployment this
project supports; serving writes to it is not something anybody chooses on
purpose, and the shipped `.env` has the token empty. **Behind a reverse proxy
this protection does not apply** — the socket peer is then the proxy, so every
forwarded request looks local. `X-Forwarded-For` is deliberately not trusted
(anybody can send one), so a deployment behind a proxy must set the token.

**Rate limiting.** `/v1` answers 429 past `RIP_RATE_LIMIT` requests a minute
from one address (120 by default, 0 to disable), on a sliding window rather
than a counter cleared on the minute — that would let twice the limit through
across a boundary. Requests from the machine itself are exempt, as writes are.
The counters live in process memory, so with `--processes 4` the real
allowance is four times the number; it is a flood stop, not a quota.
`SourceThrottle` keeps the equivalent *outbound* state in the database for
exactly this reason, and the same could be done here at the price of a write
per request.

**`GET /v1/query` is a read that writes.** When the corpus cannot answer, live
discovery searches the free sources and keeps whole person payloads it gets
back — the provider has been paid by then, so the next caller is answered from
the graph instead. `stored_from_live` counts it. That makes an unauthenticated
GET a way to grow the database, so it follows the same rule as any other
write: with no token set, only this machine's queries persist. Everyone else
gets the search, the suggestions, and `"persisted": false`, and the corpus is
unchanged. A read-only snapshot is then really read-only.

**What a live search may cost.** `RIP_LIVE_SEARCH_SECONDS` (8) bounds the
search half, as `RIP_LIVE_BUDGET_SECONDS` (30) bounds the fetches. Nothing
bounded the searches before, so one provider having a bad day set the latency
of the whole query — wikidata failing after 5.2s with OpenAlex already
answered at 2.3s, and on another run OpenAlex itself taking **70 seconds**
while the sequential phase queued behind it. Sources still running when the
budget expires are abandoned and reported as `timed out`; the sequential phase
stops starting new ones. A call already in flight cannot be interrupted —
`BaseConnector.request_timeout` (30s) is the only cap on that one.

A query that names nothing is not searched at all: `!!! ??? ***` and `the and
of in` parse to no terms and used to reach eight providers to be told nothing.
Gibberish still is searched, deliberately — `zqxjv plormbat` and `photonic
metasurface` are the same shape to a parser, and refusing the second is a
worse failure than paying for the first.

Note that with `OPENALEX_MAILTO` unset you are in OpenAlex's common pool
rather than its polite one, and live latency is then at their discretion.

### The vocabulary is rebuilt when it changes, not on a timer

Parsing needs every topic, organization and location the corpus knows, and
building that index is not cheap and does not stay cheap. Measured on a
synthetic corpus:

| distinct terms | `_build_vocab` | `_build_aux` | total |
|---:|---:|---:|---:|
| 2,726 (this corpus) | 188 ms | 329 ms | 0.5 s |
| 24,898 | 398 ms | 692 ms | 1.1 s |
| 99,897 | 1,369 ms | 5,158 ms | 6.5 s |
| 299,894 | 5,998 ms | 19,193 ms | **25 s** |

It used to be rebuilt every 60 seconds whether or not anything had changed.
At roughly 3.5 distinct terms per person that is 85,000 people spending 25
seconds out of every 60 rebuilding an unchanged vocabulary — per worker
process — and past about 285,000 people the rebuild outlasts its own window,
so the cache can never be warm at all.

Two cheap questions replace the timer. **The largest id in each table it
reads** says nothing was added: 0.4 ms at half a million evidence rows, where
`COUNT(*)` is 32 ms and `MAX(updated_at)` is 13 ms. **A count**, asked only
when `RIP_VOCAB_TTL` expires, says nothing was deleted — a maximum id cannot
fall. So a corpus nobody is writing to is never rebuilt, and an addition is
picked up on the next query instead of up to a minute later.

Measured on the 300,000-term corpus: a stale cache with nothing changed went
from a **20,167 ms** rebuild to a **28 ms** check, and repeat calls cost
0.23 ms each.

Two limits, both deliberate. `Person` contributes locations and has no integer
key, so a lone location edit from another process waits for the timer — a
person arriving essentially always brings evidence with them, which is what is
fingerprinted. And `RIP_VOCAB_MIN_INTERVAL` stops a bulk ingest in another
process buying a rebuild for every query while it runs.

The UI follows the same switch rather than deciding for itself: it asks
`GET /v1/auth`, which the bearer middleware refuses when a token is set, and
shows its sign-in screen only then. With the token unset — the usual state in
development — it opens straight into the app, because a sign-in that accepts
anything protects nothing.

**`/data` is a volume, and it is the graph.** A container filesystem is
disposable; the SQLite file must not be. `RIP_DATABASE_URL` already points at
`/data/rip.db`, and the schema is created on first start, so an empty volume
is a valid starting point. To carry an existing graph in, take a consistent
snapshot rather than copying the file — a live database has un-checkpointed
WAL beside it, and copying the three files separately produces a torn read:

```bash
python -c "import sqlite3; sqlite3.connect('rip.db').execute('VACUUM INTO ?', ('seed.db',))"
```

Then place `seed.db` in the volume as `rip.db` before the first start.

**Secrets come from the environment, never the image.** `.env` and every
`*.db` are excluded by `.dockerignore`, so a build cannot bake in a token or
a copy of the graph. Pass them at run time with `--env-file` or `-e`.

The image runs as UID 10001, not root. On a Linux host using a bind mount
rather than a named volume, `chown -R 10001:10001 ./data` first, or the
process cannot write.

### Notes for a real deployment

- **Workers.** SQLite takes one writer, so `serve` gives a writable SQLite
  file one worker. A read-only snapshot (`mode=ro`) has no writer, and gets
  every worker asked for. To run more than one process against a graph that
  also grows, move to Postgres via `RIP_DATABASE_URL` — the connection pool
  settings in `db.py` already switch on for server-backed engines.
- **Ingestion is not this container's job.** It serves. Run `ingest`,
  `refresh` and `deliver-webhooks` as separate jobs against the same volume
  or database.
- **Health.** `HEALTHCHECK` polls `/`, which needs no token and touches no
  tables, so it reports the process rather than the data.

> **Deploying this?** `DEPLOYMENT.md` has the instructions, and says
> which of them have been verified and which have not.

## Linting, types, coverage, audit

```bash
pip install -e ".[dev]"
ruff check .                 # style, dead code, likely bugs
ruff check . --fix
mypy                         # configured in pyproject: backend/rip and backend/scripts
coverage run -m pytest && coverage report
pip-audit                    # known vulnerabilities in the dependency tree
```

None of these had ever run against these 32,000 lines. The first `ruff` pass
found **267 things**, most of them cosmetic and 158 auto-fixable, and three
that were not:

- **`GET /v1/persons?has_email=true` raised `NameError` on every call.**
  `PersonKey` was used in the query and never imported. It is a documented
  filter in the table above and 711 tests went past it, because not one of
  them used it. `tests/test_person_filters.py` now walks the filters the
  endpoint *declares* rather than the ones somebody remembered, which is the
  only version of that test that would have caught it.
- **`rip/dossier.py` could not be imported on the Python this project claims
  to support.** It used backslashes inside f-strings, which is Python 3.12+;
  `requires-python` says 3.10. Nobody noticed because nobody here runs 3.10.
- Duplicate entries in half a dozen set literals, and three re-imports
  shadowing a module-level name.

Both now run clean: `ruff check .` and `mypy` report nothing (they stood at
88 and 202 on 2026-09-29). Nearly all of the fixes were annotations. Four
were not, because mypy had found something true:

- **`GET /v1/persons/{id}` returned 500 for a tombstone whose target is
  gone.** `merged_into` has no foreign key, so a dangling one is possible;
  it is now a 404.
- **`discovery_suggestions` declared `session=None` as allowed** and then
  called `session.execute` on it unguarded. Both arguments are now required,
  which is how every caller already used it.
- **A gate annotated as returning `(query, mode)` returned the string
  `"ok"`.** Callers only tested it against None, so nothing broke; it
  returns a bool now.
- **Europe PMC author lists can hold None** (an author with no name) where
  the type said `list[str]`. The type now says so, rather than the data being
  changed to fit it.

The `type: ignore`s that remain are each commented, and `warn_unused_ignores`
fails the run if one stops being needed.

`.github/workflows/checks.yml` runs ruff, mypy and the tests on Python 3.10
and 3.14, and typechecks the web app, on every push. Its first runs failed in
ways this machine could not show: import order that depended on a stray
directory at the repository root (now `src = ["backend"]`), SQLAlchemy 2.1
installed unasked on 3.14 (now `<2.1`, the version tested), and a test that
relied on Windows reading environment variables case-insensitively. Ruff and
mypy are pinned to exact versions, so CI checks what was checked here.

mypy also reads the scripts, several of which write to the database, and the
bodies of unannotated functions. Not the tests: fakes and monkeypatched
methods are what they are for.

Coverage is **74%** overall and **89%** for the `rip` package, on 2026-09-30.
It was 69%, with `rip/dossier.py` and `rip/harvest.py` at 0% and `rip/cli.py`
at 13%; those are now 91%, 94% and 64%. What stays low is one-off scripts.
`pip-audit -r requirements.txt` reports no known vulnerability (2026-09-30).

## Running the full build

This is the version with every capability. One command:

```bash
.venv/bin/python -m rip.cli serve
```

It reads `.env` itself, prints what it is serving, and refuses to pretend:
if the database is read-only it says so, and if `RIP_API_TOKEN` is unset it
warns that the API is open. `--host 0.0.0.0` accepts connections from other
machines; `--workers N` needs Postgres for a writable graph, because SQLite
does not take concurrent writers. A read-only SQLite snapshot has no writer
to serialise against, so it takes `--workers N` as it stands.

**Use the workers.** Ranking is CPU-bound Python, so a second concurrent
request does not get a second core. Measured over HTTP against the
767-person corpus, natural-language queries with live discovery off:

| concurrent callers | 1 | 2 | 4 | 8 | 16 |
|---|---|---|---|---|---|
| one worker (req/s) | 13.7 | 9.5 | 15.9 | 9.2 | 7.8 |
| four workers (req/s) | 12.4 | 27.3 | 41.1 | **49.4** | 31.8 |

On one process throughput *falls* as callers are added — they contend for
the GIL rather than sharing the work — and p95 goes from 112 ms serially to
1.4 s at eight-way. Four processes serve the same eight-way load at a p95 of
281 ms. More processes is the only thing that raises the number; more
threads lower it. For comparison, `/v1/persons` (plain SQL, no ranking)
does 50 req/s serially and 90 at eight-way on a single worker.

What "full" means, against the read-only snapshot described below:

| | Full build | Bundled snapshot |
|---|---|---|
| People served | the whole graph (50,800+) | ~14,000 |
| Live search | finds people **and keeps them** | finds them, cannot store them |
| Corpus | grows with every query | fixed until redeployed |
| Raw payloads | kept, so parsers can improve without re-crawling | stripped |
| Background worker, webhooks | yes | no |
| Request time | unbounded | capped by the platform |

The graph is ~1.2 GB and grows. Any host that gives you a writable disk and
a long-running process will serve it; a serverless platform generally will
not.

### Postgres

SQLite is the default and is fine for one process. For several, or for a
graph you keep writing to while serving:

```bash
export RIP_DATABASE_URL=postgresql+psycopg://user:pass@host/seekr
.venv/bin/python -m rip.cli init-db
.venv/bin/python backend/scripts/migrate_to_postgres.py   # copies an existing SQLite graph
```

No code changes — every query in the codebase runs on both.

## Deployment as a read-only snapshot (optional)

A cut-down copy can be served from a platform with a read-only filesystem
(this repo carries a Vercel entrypoint at `api/index.py`). It is a demo of
the read API, not the product: the snapshot is capped at 100 MB, which is
roughly 14,000 people, and nothing found live can be kept.

```bash
.venv/bin/python backend/scripts/build_snapshot.py   # never copy rip.db by hand
```

`backend/scripts/build_snapshot.py` is the only supported way to build it. It
checkpoints the WAL, strips raw payloads and forces `journal_mode=DELETE` —
a WAL database **cannot be opened on a read-only filesystem**, so a
hand-copied `rip.db` produces a deployment where every `/v1` route returns
500 ("unable to open database file"). The script refuses to emit a WAL file
or a snapshot over the size limit.

Responses from such a deployment carry `"storage": "read-only"`, and the UI
says plainly that people found live are shown but not saved.

## Operations

**Never ingest inside the API server process.** The API is read-only and, in
production, reads a snapshot on a read-only filesystem. Ingestion runs from
cron (`backend/scripts/nightly_refresh.sh`) or a separate `rip.cli worker` process.
On SQLite that separation is what WAL mode makes safe; on Postgres it lets
you run several workers at once.

**Environment variables for the ingest host** (none are needed to *serve*):

| Variable | Why it matters |
|---|---|
| `GITHUB_TOKEN` | 60 → 5,000 requests/hour. Without it, enrichment stalls almost immediately |
| `SEMANTIC_SCHOLAR_API_KEY` | avoids shared-pool 429s |
| `OPENALEX_MAILTO` | polite pool: higher, more reliable limits |
| `RIP_LEAD_BATCH` | leads drained per nightly run (default 100) |
| `SEEKR_SEARCH_TTL_DAYS` | reuse a cached live search for this many days (default 7) |
| `RIP_DATABASE_URL` | defaults to `sqlite:///rip.db` |
| `RIP_API_TOKEN` | set on the *serving* side; makes `/v1` require a bearer token |
| `RIP_RATE_LIMIT` | requests a minute from one address before `/v1` answers 429 (default 120, 0 = off) |
| `RIP_LIVE_SEARCH_SECONDS` | how long live *searches* may take before stragglers are abandoned (default 8) |
| `RIP_VOCAB_TTL` | how often to check for DELETED terms with a count (default 600s; additions are noticed at once) |
| `RIP_VOCAB_MIN_INTERVAL` | floor between vocabulary rebuilds while a writer is busy (default 5s) |

`rip.cli check-db` prints which of these are set, along with engine, journal
mode, and queue depths — run it first when something looks wrong.

**Batch sizing.** `RIP_LEAD_BATCH` defaults to 100 per nightly run. Enrichment
multiplies API calls per person (one lead can become 3–4 fetches), so without
`GITHUB_TOKEN` a tokenless run exhausts GitHub's 60/hour almost immediately —
every ingest command warns when credentials are missing. For large catch-up
runs, use `--no-enrich`; enrichment can be applied later by re-ingesting.
The worker backs off exponentially (to 15 min) when a whole batch fails,
which is what throttling looks like, and finishes its current batch before
exiting on Ctrl-C.

**Parallel workers.** `worker`, `ingest-leads` and `refresh` take
`--processes N`. Three things make that safe:

- **A lead is claimed before it is fetched.** One conditional `UPDATE` marks a
  batch as this worker's, so two workers never hold the same lead — on SQLite
  under its single write lock, on Postgres with `FOR UPDATE SKIP LOCKED`, so a
  second worker takes the next rows instead of queueing behind the first. A
  worker that dies leaves claims that return to the queue after 30 minutes;
  one that is interrupted hands its unfinished leads back at once.
  `refresh` splits the stale records by id instead, so each is refreshed by
  exactly one process.
- **Two leads can be one person.** An OpenAlex author and their ORCID record,
  drained by two workers at once, both find nobody and both create a person;
  the second commit hits the unique ORCID key. That write is retried, and
  resolves onto the person the first worker stored.
- **The fleet is exactly as polite as one worker.** Request spacing is kept per
  process, so each of N workers spaces its requests N times wider
  (`RIP_WORKER_PROCESSES`, set for you). Throughput still scales, because a
  request's own latency dwarfs the gap between requests.

Measured with a source answering in 0.3 s, 120 leads including 20 pairs that
are the same person: one process 39.7 s, four processes 12.9 s — every lead
ingested once, every pair merged, no errors. If you start workers by hand
rather than with `--processes`, set `RIP_WORKER_PROCESSES` to how many you run;
never set it for `serve`, which would slow live search for nothing.

**Webhooks only fire when `deliver-webhooks` runs.** Nothing is pushed from
the API. Deliveries accumulate in an outbox until the CLI (invoked by the
nightly script) sends them, so a failing cron shows up as a growing backlog:
check `GET /v1/webhooks/health` or `rip.cli check-db`.

**Live discovery** is opt-in per request on `/v1/query`:

- `discover=false` (default) — local corpus only
- `discover=true` — additionally returns `discovery_suggestions` from live
  source searches; **nothing is ingested**
- `discover=queue` — same, and adds each suggestion to the discovery-lead
  queue for a worker to ingest later. Still no ingest inside the request

## Known limitations / roadmap

Done: ~~fuzzy resolution scans all persons~~ (blocked by org + name token),
~~no webhooks~~ (outbox + `deliver-webhooks`), ~~merge review needs a UI~~
(`/ui`), ~~single-source profiles~~ (enrichment chain), ~~single-process
workers~~ (`--processes N`, claimed leads), ~~graph depth 1~~ (three hops,
capped per person and in total).

Still open:

- Web connector reads a page and at most three of its About / CV /
  Publications / Research pages — a profile spread deeper than that is
  partial.
- Subjects are searched live in OpenAlex and Europe PMC, and in Semantic
  Scholar only with a (free) `SEMANTIC_SCHOLAR_API_KEY`. Every other source is
  asked by name.
- **Industry roles** — product managers, founders, engineers at companies who
  publish nothing — are reachable only through Exa, which is paid and
  LinkedIn-derived. The free sources cover people who publish, ship code or
  answer questions in public.
- Negation, "both" and count thresholds are pattern-based. "not" inside a
  longer clause ("researchers who are not only…") is handled for the common
  forms, not for every English construction.
- Search quality on queries nobody tuned for (`holdout2`, nDCG@10 0.76) is
  below the tuned sets (0.86), and five of those criteria have now been
  edited after seeing results — see *Measuring search quality*.
- Bulk ingest is single-threaded; throughput is bounded by resolution, not IO.
