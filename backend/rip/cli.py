"""CLI entry point.

Examples:
    python -m rip.cli init-db
    python -m rip.cli ingest github torvalds
    python -m rip.cli search-openalex "Geoffrey Hinton"
    python -m rip.cli ingest openalex A5023888391
    python -m rip.cli refresh --older-than-hours 24
    python -m rip.cli serve
"""

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .connectors import get_connector
from .connectors.openalex import OpenAlexConnector
from .db import SessionLocal, init_db
from .ingest import run_connector
from .models import SourceRecord


def warn_missing_tokens(enriching: bool = True) -> None:
    """Tell the operator up front which sources will throttle.

    Without a GitHub token the limit is 60 requests/hour, which enrichment
    exhausts within a couple of dozen people — better to say so before a long
    run than to leave a pile of rate-limit errors in the log.
    """
    import os

    missing = [
        (var, note)
        for var, note in (
            ("GITHUB_TOKEN", "GitHub limited to 60 req/hour (5,000 with a token)"),
            ("SEMANTIC_SCHOLAR_API_KEY", "Semantic Scholar shared pool: expect 429s"),
            ("OPENALEX_MAILTO", "OpenAlex polite pool disabled: lower limits"),
        )
        if not os.environ.get(var)
    ]
    if not missing:
        return
    print("warning: missing API credentials —", file=sys.stderr)
    for var, note in missing:
        print(f"  {var} unset: {note}", file=sys.stderr)
    if enriching:
        print(
            "  enrichment multiplies calls per person; consider --no-enrich "
            "for large tokenless runs",
            file=sys.stderr,
        )


def cmd_ingest(args) -> None:
    warn_missing_tokens(enriching=not args.no_enrich)
    init_db()
    connector = get_connector(args.source)
    with SessionLocal() as session:
        person = run_connector(
            session, connector, args.identifier, enrich_chain=not args.no_enrich
        )
        print(f"ingested -> person {person.id} ({person.canonical_name})")


def cmd_search_openalex(args) -> None:
    results = OpenAlexConnector().search_authors(args.name)
    print(json.dumps(results, indent=2))


def cmd_search_dblp(args) -> None:
    from .connectors.dblp import DblpConnector

    print(json.dumps(DblpConnector().search_authors(args.name), indent=2))


def cmd_search_s2(args) -> None:
    from .connectors.semanticscholar import SemanticScholarConnector

    print(json.dumps(SemanticScholarConnector().search_authors(args.name), indent=2))


def _in_processes(command: str, args) -> bool:
    """Run COMMAND in `args.processes` worker processes. False when asked for one.

    Separate processes, not threads: ingest is Python work between network
    waits, and a thread pool would serialise it on the interpreter lock. Each
    child opens its own database connection and marks itself one of N, which
    widens its request spacing N times over (connectors.base.worker_processes)
    so the fleet stays exactly as polite to every source as a single worker.
    """
    import multiprocessing
    import os

    processes = getattr(args, "processes", 1) or 1
    if processes <= 1 or getattr(args, "shard", None) is not None:
        return False
    os.environ["RIP_WORKER_PROCESSES"] = str(processes)
    # migrate and index once, here: N children each running ALTER TABLE or an
    # index rebuild against one database at the same moment would race
    init_db()
    context = multiprocessing.get_context("spawn")
    children = [
        context.Process(target=_child, args=(command, vars(args), i, processes),
                        name=f"rip-{command}-{i}")
        for i in range(processes)
    ]
    print(f"{command}: {processes} processes", flush=True)
    for child in children:
        child.start()
    try:
        for child in children:
            child.join()
    except KeyboardInterrupt:
        # every child got the same interrupt and is finishing its batch
        for child in children:
            child.join()
    code = max((child.exitcode or 0) for child in children)
    if code:
        sys.exit(code)
    return True


def _child(command: str, arg_dict: dict, index: int, total: int) -> None:
    """One worker process: the same command, on its share of the work."""
    args = argparse.Namespace(**{**arg_dict, "processes": 1, "shard": (index, total)})
    if command == "ingest-leads":
        # --limit is the total asked for, not a quota per process
        args.limit = -(-args.limit // total)
    {"refresh": cmd_refresh, "ingest-leads": cmd_ingest_leads, "worker": cmd_worker}[command](args)


def cmd_refresh(args) -> None:
    """Re-fetch source records not observed recently (run from cron for freshness)."""
    if _in_processes("refresh", args):
        return
    init_db()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.older_than_hours)
    with SessionLocal() as session:
        stmt = select(SourceRecord).where(SourceRecord.last_observed < cutoff)
        shard = getattr(args, "shard", None)
        if shard is not None:
            # records split by id across processes: every stale record is
            # refreshed by exactly one of them
            index, total = shard
            stmt = stmt.where(SourceRecord.id % total == index)
        stale = session.execute(stmt).scalars().all()
        print(f"{len(stale)} stale source records")
        connectors = {}
        failures = 0
        for record in stale:
            connectors.setdefault(record.source, get_connector(record.source))
            try:
                run_connector(session, connectors[record.source], record.external_id)
                print(f"refreshed {record.source}:{record.external_id}")
            except Exception as exc:
                failures += 1
                print(f"FAILED {record.source}:{record.external_id}: {exc}", file=sys.stderr)
        if failures:
            sys.exit(1)


def cmd_discover(args) -> None:
    from .discover import (
        discover_dblp_coauthors,
        discover_github_contributors,
        discover_openalex_coauthors,
    )

    init_db()
    with SessionLocal() as session:
        added = discover_openalex_coauthors(session)
        print(f"openalex co-authors: {added} new leads")
        added = discover_dblp_coauthors(session)
        print(f"dblp co-authors: {added} new leads")
        if args.github_contributors:
            added = discover_github_contributors(
                session, max_repos_per_person=args.max_repos
            )
            print(f"github contributors: {added} new leads")


def cmd_ingest_leads(args) -> None:
    from .discover import drain_leads

    if _in_processes("ingest-leads", args):
        return
    warn_missing_tokens(enriching=not getattr(args, 'no_enrich', False))

    init_db()
    with SessionLocal() as session:
        ok, failed = drain_leads(
            session, limit=args.limit, source=args.source,
            enrich_chain=not getattr(args, "no_enrich", False),
        )
        print(f"ingested {ok}, failed {failed}")
        if failed:
            sys.exit(1)


def cmd_review_triage(session, triage, yes: bool) -> None:
    """Judge the review queue on evidence and report it in plain language."""
    from collections import Counter

    out = triage(session, apply=yes)
    total = sum(len(v) for v in out.values())
    print(f"{total} pairs were waiting for a human\n")
    labels = {
        "stale": "answered already (one side merged away or removed)",
        "rejected": "two different people, on the evidence",
        "deferred": "nothing to decide: no shared paper, co-author or organization",
        "merged": "proven the same person",
        "pending": "left for you: real evidence, short of proof",
    }
    for bucket, label in labels.items():
        rows = out[bucket]
        if not rows:
            continue
        print(f"  {len(rows):4d}  {label}")
        for reason, n in Counter(r["reason"] for r in rows).most_common(4):
            print(f"          {n:4d}  {reason}")
    for bucket, header in (("merged", "merged"), ("pending", "still yours to decide")):
        if out[bucket]:
            print(f"\n  {header}:")
            for r in out[bucket][:20]:
                a, b = r.get("names", ["?", "?"])
                print(f"    #{r['candidate_id']:<5} {a!r} / {b!r}  {r['reason']}")
            if len(out[bucket]) > 20:
                print(f"    ... and {len(out[bucket]) - 20} more")
    if yes:
        print(f"\ndone: {len(out['merged'])} merged, {len(out['rejected'])} rejected, "
              f"{len(out['deferred'])} deferred, {len(out['pending'])} left pending "
              f"(every merge is reversible: rip.cli review split <link_id>)")
    else:
        print("\nnothing written. re-run with --yes to carry this out")


def cmd_review(args) -> None:
    from .review import approve_link, list_suspicious, resolve_duplicate, split_link, triage

    init_db()
    with SessionLocal() as session:
        if args.action == "list":
            print(json.dumps(list_suspicious(session), indent=2, default=str))
        elif args.action == "triage":
            cmd_review_triage(session, triage, args.yes)
        elif args.action == "approve":
            link = approve_link(session, args.link_id)
            print(f"link {link.id} approved")
        elif args.action == "split":
            person = split_link(session, args.link_id)
            print(f"split -> new person {person.id} ({person.canonical_name})")
        elif args.action == "merge":
            print(json.dumps(resolve_duplicate(session, args.link_id, "merge"), default=str))
        elif args.action == "dismiss":
            print(json.dumps(resolve_duplicate(session, args.link_id, "reject"), default=str))


def cmd_reparse(args) -> None:
    """Re-run normalization over stored raw payloads — no network calls."""
    from .ingest import ingest_profile
    from .models import SourceRecord

    init_db()
    with SessionLocal() as session:
        stmt = select(SourceRecord)
        if args.source:
            stmt = stmt.where(SourceRecord.source == args.source)
        records = session.execute(stmt).scalars().all()
        connectors = {}
        ok = skipped = failed = 0
        for record in records:
            connectors.setdefault(record.source, get_connector(record.source))
            try:
                profile = connectors[record.source].renormalize(record.external_id, record.raw)
                ingest_profile(session, profile)
                ok += 1
            except NotImplementedError as exc:
                skipped += 1
                if args.verbose:
                    print(f"skip {record.source}:{record.external_id}: {exc}")
            except Exception as exc:
                failed += 1
                print(f"FAILED {record.source}:{record.external_id}: {exc}", file=sys.stderr)
        print(f"reparsed {ok}, skipped {skipped} (no stored raw), failed {failed}")
        if failed:
            sys.exit(1)


def cmd_harvest(args) -> None:
    from .harvest import harvest_github_india, harvest_openalex

    if args.source == "openalex":
        filter_expr = args.filter
        if args.india and not filter_expr:
            filter_expr = "last_known_institutions.country_code:IN"
        elif args.india:
            filter_expr = f"{filter_expr},last_known_institutions.country_code:IN"
        result = harvest_openalex(
            args.out, limit=args.limit, filter_expr=filter_expr, cursor=args.cursor
        )
        print(f"next cursor (resume with --cursor): {result.next_cursor}")
    elif args.source == "github":
        if not args.india:
            raise SystemExit("github harvesting currently targets India: pass --india")
        warn_missing_tokens(enriching=False)
        harvest_github_india(args.out, limit=args.limit)
    else:
        raise SystemExit("harvest supports 'openalex' and 'github'")


def cmd_bulk_ingest(args) -> None:
    from .bulk import bulk_ingest

    warn_missing_tokens(enriching=not args.no_enrich)

    init_db()
    with SessionLocal() as session:
        result = bulk_ingest(
            session, args.source, args.file,
            batch_size=args.batch_size, limit=args.limit,
            enrich_chain=not args.no_enrich,
        )
    print(
        f"processed {result.processed}, ingested {result.ingested}, failed {result.failed}"
    )
    if result.failure_file:
        print(f"failures written to {result.failure_file}")
        sys.exit(1)


def cmd_find_homepages(args) -> None:
    """Search the web for homepages of people who have none, then ingest them.

    Two stages on purpose: search finds candidate URLs, the web connector
    reads them (honouring robots.txt). Uses free search-provider tiers; does
    nothing at all when no search key is configured.
    """
    from sqlalchemy import func

    from .ingest import run_connector
    from .models import Affiliation, IdentityLink, Organization, Person, SourceRecord
    from .websearch import available_backends, find_homepage

    backends = available_backends()
    if not backends:
        raise SystemExit(
            "no search backend configured — set TAVILY_API_KEY or SERPAPI_API_KEY"
        )
    init_db()
    connector = get_connector("web")
    print(f"searching with: {', '.join(backends)}")

    with SessionLocal() as session:
        # people who have no web source record yet
        has_web = select(IdentityLink.person_id).join(
            SourceRecord, SourceRecord.id == IdentityLink.source_record_id
        ).where(SourceRecord.source == "web")
        stmt = (
            select(Person)
            .where(Person.canonical_name.isnot(None), Person.merged_into.is_(None),
                   ~Person.id.in_(has_web))
            .limit(args.limit)
        )
        if args.min_sources > 1:
            stmt = stmt.where(
                select(func.count(IdentityLink.id))
                .where(IdentityLink.person_id == Person.id)
                .scalar_subquery() >= args.min_sources
            )
        people = session.execute(stmt).scalars().all()
        print(f"{len(people)} people without a homepage on file")

        found = ingested = 0
        for person in people:
            org = person.current_organization or session.execute(
                select(Organization.name)
                .join(Affiliation, Affiliation.organization_id == Organization.id)
                .where(Affiliation.person_id == person.id).limit(1)
            ).scalar()
            candidates = find_homepage(person.canonical_name, org, limit=args.candidates)
            if not candidates:
                continue
            found += 1
            for candidate in candidates[: args.candidates]:
                try:
                    run_connector(session, connector, candidate["url"], enrich_chain=False)
                    ingested += 1
                    print(f"  {person.canonical_name} -> {candidate['url'][:70]}")
                    break
                except Exception as exc:
                    print(f"  skip {candidate['url'][:56]}: {str(exc)[:60]}")
        print(f"searched {len(people)}, candidates for {found}, ingested {ingested}")


def cmd_deliver_webhooks(args) -> None:
    from .webhooks import deliver_pending, health

    init_db()
    with SessionLocal() as session:
        delivered, failed = deliver_pending(session, limit=args.limit)
        state = health(session)
        print(
            f"delivered {delivered}, failed {failed}, still pending {state['pending']}"
        )
        if state["pending"]:
            print(f"  oldest pending queued at {state['oldest_pending_at']}")
        if failed > args.fail_threshold:
            print(
                f"error: {failed} failures exceed threshold {args.fail_threshold}",
                file=sys.stderr,
            )
            sys.exit(1)


def cmd_queue_stats(args) -> None:
    """Show discovery-lead backlog and how long draining it will take."""
    from sqlalchemy import func

    from .models import DiscoveryLead

    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(DiscoveryLead.source, DiscoveryLead.status, func.count(DiscoveryLead.id))
            .group_by(DiscoveryLead.source, DiscoveryLead.status)
        ).all()
        pending_by_source = {s: n for s, st, n in rows if st == "pending"}
        total_pending = sum(pending_by_source.values())
        oldest = session.execute(
            select(func.min(DiscoveryLead.created_at)).where(
                DiscoveryLead.status == "pending"
            )
        ).scalar()

        print("pending leads by source:")
        for source, n in sorted(pending_by_source.items(), key=lambda kv: -kv[1]):
            print(f"  {source:16} {n:>8,}")
        print(f"  {'TOTAL':16} {total_pending:>8,}")
        for status in ("claimed", "ingested", "error", "skipped"):
            n = sum(c for _, st, c in rows if st == status)
            if n:
                print(f"{status}: {n:,}")
        if oldest:
            print(f"oldest pending lead: {oldest}")
        if total_pending:
            nights = total_pending / max(args.batch_size, 1)
            print(
                f"at {args.batch_size}/run: {nights:,.0f} runs "
                f"({nights / 365:.1f} years at one run per night)"
            )
            print(
                "  catch-up:  RIP_LEAD_BATCH=500 python -m rip.cli worker "
                "--limit 500 --once --no-enrich"
            )


def cmd_purge_nonpersons(args) -> None:
    """Remove records that are index entities, not people.

    Scholarly indexes list conferences, labs and universities as authors, so
    they arrive as persons and then pollute name search — "computer" starts to
    look like a surname. New ones are rejected at ingest; this clears the ones
    that arrived before that guard existed.
    """
    from collections import defaultdict

    from sqlalchemy import delete, select

    from . import models as M
    from .db import SessionLocal
    from .personhood import assess
    from .resolution import sync_name_tokens

    session = SessionLocal()
    rows = session.execute(
        select(M.Person.id, M.Person.canonical_name).where(M.Person.merged_into.is_(None))
    ).all()
    sources: dict[str, set] = defaultdict(set)
    for pid, src in session.execute(
        select(M.IdentityLink.person_id, M.SourceRecord.source)
        .join(M.SourceRecord, M.SourceRecord.id == M.IdentityLink.source_record_id)
    ).all():
        sources[pid].add(src)

    doomed, renames = [], []
    for pid, name in rows:
        # judged as a page title only when every record behind it is a page
        kind = "web" if sources.get(pid) == {"web"} else next(iter(sources.get(pid) or {None}))
        verdict = assess(name, kind)
        if not verdict.is_person:
            doomed.append((pid, name, verdict.reason))
        elif verdict.name and verdict.name != name:
            renames.append((pid, name, verdict.name, verdict.aliases))

    print(f"{len(doomed)} of {len(rows)} records are not people")
    for _, name, reason in doomed[:20]:
        print(f"   - {name[:70]}  ({reason})")
    if len(doomed) > 20:
        print(f"   ... and {len(doomed) - 20} more")
    print(f"{len(renames)} names are page titles or carry decoration, and would be cleaned")
    for _, old, new, _ in renames[:20]:
        print(f"   ~ {old[:55]!r} -> {new!r}")
    if len(renames) > 20:
        print(f"   ... and {len(renames) - 20} more")
    if not doomed and not renames:
        return
    if not getattr(args, "yes", False):
        print("\nre-run with --yes to delete and rename them")
        return

    for pid, old, new, aliases in renames:
        person = session.get(M.Person, pid)
        person.canonical_name = new
        # "Rahul M Mulajkar,Rahul Mukundrao Mulajkar,RMM" keeps its other
        # spellings as aliases, as the same name does at ingest
        extra = [a for a in aliases if a not in (person.aliases or []) and a != new]
        if extra:
            person.aliases = [*(person.aliases or []), *extra]
        session.add(M.ChangeLog(person_id=pid, field="canonical_name",
                                old_value=old, new_value=new))
        sync_name_tokens(session, person)
    session.commit()      # the search index re-indexes renamed people here
    print(f"  renamed: {len(renames)}")
    if not doomed:
        return

    ids = [i for i, _, _ in doomed]
    for name in ("Affiliation", "AttributeConflict", "Authorship", "ChangeLog",
                 "Contribution", "Evidence", "IdentityLink", "MergeCandidate",
                 "PersonKey", "PersonNameToken", "SearchTerm", "SearchDoc",
                 "ShortlistMember", "MatchFeedback"):
        from . import search_index as SI

        model = getattr(M, name, None) or getattr(SI, name, None)
        col = getattr(model, "person_id", None) if model else None
        if col is None:
            continue
        n = session.execute(delete(model).where(col.in_(ids))).rowcount
        if n:
            print(f"  {model.__tablename__}: {n}")
    # the other side of a proposed duplicate pair points at them too
    session.execute(delete(M.MergeCandidate).where(M.MergeCandidate.candidate_person_id.in_(ids)))
    print(f"  person: {session.execute(delete(M.Person).where(M.Person.id.in_(ids))).rowcount}")
    session.commit()


def cmd_merge_orgs(args) -> None:
    """Fold organizations that are the same company spelled differently.

    "Deccan.AI" and "Deccan AI" were separate records with separate people, so
    searching one could not see the other. New records resolve on a normalized
    key; this folds the ones created before that.
    """
    from collections import defaultdict

    from sqlalchemy import delete, func, select, update

    from .db import SessionLocal
    from .models import Affiliation, Organization, normalize_org_name

    session = SessionLocal()
    groups = defaultdict(list)
    for org in session.execute(select(Organization)).scalars():
        groups[org.norm_name or normalize_org_name(org.name)].append(org)
    dupes = {k: v for k, v in groups.items() if k and len(v) > 1}
    print(f"{len(dupes)} organizations exist under more than one spelling")
    for key, orgs in list(dupes.items())[:15]:
        print(f"   {key}: " + ", ".join(repr(o.name) for o in orgs))
    if len(dupes) > 15:
        print(f"   ... and {len(dupes) - 15} more")
    if not dupes or not getattr(args, "yes", False):
        if dupes:
            print("\nre-run with --yes to merge them")
        return

    def _uses(org) -> int:
        return session.scalar(
            select(func.count()).select_from(Affiliation)
            .where(Affiliation.organization_id == org.id)
        ) or 0

    merged = 0
    touched: set[str] = set()
    for orgs in dupes.values():
        # Keep the spelling most sources used. Longest-name lost "Google" to
        # "Google Inc.", which is not what anyone calls it.
        keeper = max(orgs, key=lambda o: (_uses(o), -len(o.name or ""), -o.id))
        for other in orgs:
            if other.id == keeper.id:
                continue
            touched.update(session.execute(
                select(Affiliation.person_id).where(Affiliation.organization_id == other.id)
            ).scalars())
            session.execute(
                update(Affiliation)
                .where(Affiliation.organization_id == other.id)
                .values(organization_id=keeper.id)
            )
            session.execute(delete(Organization).where(Organization.id == other.id))
            merged += 1
    # a bulk UPDATE bypasses the ORM hooks that keep the search index current
    from .search_index import index_people

    index_people(session, touched)
    session.commit()
    print(f"merged {merged} duplicate organization records")


def cmd_dedupe(args) -> None:
    """Merge people stored more than once — only where evidence proves it."""
    from sqlalchemy import select

    from . import dedupe
    from .db import SessionLocal
    from .models import IdentityLink, Person, SourceRecord

    session = SessionLocal()

    def label(pid: str) -> str:
        person = session.get(Person, pid)
        sources = session.execute(
            select(SourceRecord.source, SourceRecord.external_id)
            .join(IdentityLink, IdentityLink.source_record_id == SourceRecord.id)
            .where(IdentityLink.person_id == pid)
        ).all()
        return f"{person.canonical_name} [{', '.join(f'{s}:{e}' for s, e in sources)}]"

    planned = dedupe.plan(session)
    print(f"examined {planned.groups_examined} names held by more than one record")
    print(f"{len(planned.merges)} proven duplicates would be merged")
    for keep, gone, j in planned.merges:
        print(f"   + {label(gone)}\n       into {label(keep)}\n       ({j.reason})")
    print(f"{len(planned.reviews)} possible duplicates would be queued for review")
    for a, b, j in planned.reviews[:15]:
        print(f"   ? {label(a)}  ~  {label(b)}\n       ({j.reason})")
    if len(planned.reviews) > 15:
        print(f"   ... and {len(planned.reviews) - 15} more")
    if not (planned.merges or planned.reviews):
        return
    if not getattr(args, "yes", False):
        print("\nre-run with --yes to merge and queue them "
              "(every merge is reversible: rip.cli review split <link_id>)")
        return
    merged, queued = dedupe.apply(session, planned)
    # A merge pools evidence, which can surface new review pairs (or, rarely,
    # new proof). Re-plan until nothing changes; bounded, since every merge
    # removes a record.
    for _ in range(5):
        again = dedupe.plan(session)
        if not (again.merges or again.reviews):
            break
        m, q = dedupe.apply(session, again)
        merged, queued = merged + m, queued + q
        if not (m or q):
            break
    print(f"merged {merged}; queued {queued} for review (rip.cli review list)")


def cmd_reindex(args) -> None:
    """Rebuild the search index from the tables (no network)."""
    import time

    from .db import SessionLocal
    from .search_index import rebuild

    session = SessionLocal()
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        print(f"  ...{done}/{total}", flush=True)

    n = rebuild(session, batch=args.batch, progress=progress)
    print(f"indexed {n} people in {time.monotonic() - started:.1f}s")
    # Name blocking keys too: they change when name handling does (spelling
    # groups), and a person ingested before that would not block with a new
    # record spelling their name the other way.
    from sqlalchemy import select as _select

    from .models import Person
    from .resolution import sync_name_tokens

    synced = 0
    for person in session.execute(
        _select(Person).where(Person.merged_into.is_(None))
    ).scalars():
        sync_name_tokens(session, person)
        synced += 1
    session.commit()
    print(f"name blocking keys refreshed for {synced} people")


def cmd_audit_protected(args) -> None:
    """Report protected-attribute material already sitting in the graph.

    Screening runs at ingest, so this is for everything collected before it
    did — and for checking that the patterns are not flagging topics.
    """
    from sqlalchemy import select

    from .compliance import redact, scan
    from .db import SessionLocal
    from .ingest import FREE_TEXT_ATTRS
    from .models import Evidence

    session = SessionLocal()
    rows = session.execute(
        select(Evidence).where(Evidence.attribute_type.in_(tuple(FREE_TEXT_ATTRS)))
    ).scalars().all()

    hits = [(e, scan(e.value)) for e in rows]
    hits = [(e, f) for e, f in hits if f]
    print(f"{len(rows):,} free-text evidence values scanned, {len(hits)} carrying "
          "protected attributes")
    by_kind: dict[str, int] = {}
    for _e, findings in hits:
        for f in findings:
            by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"   {kind:22} {n}")
    for e, findings in hits[:10]:
        print(f"\n   person {e.person_id}  ({e.attribute_type}, {e.source})")
        print(f"     now:  {str(e.value)[:110]}")
        print(f"     ->    {str(redact(e.value)[0])[:110]}")
    if hits and not getattr(args, "yes", False):
        print(f"\nre-run with --yes to redact these {len(hits)} values in place")
        return
    for e, _f in hits:
        e.value = redact(e.value)[0]
    if hits:
        session.commit()
        print(f"\nredacted {len(hits)} values")


def cmd_check_db(args) -> None:
    """Print engine, journal mode, credentials and queue depths."""
    import os

    from sqlalchemy import func, text

    from .db import DB_URL, engine
    from .models import DiscoveryLead, Person, SourceRecord, WebhookDelivery

    init_db()
    is_sqlite = DB_URL.startswith("sqlite")
    print(f"database url:  {DB_URL}")
    print(f"engine:        {engine.dialect.name}")
    if is_sqlite:
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
        print(f"journal_mode:  {mode}  (wal expected for local ingest)")
        print(f"busy_timeout:  {timeout} ms")
    with SessionLocal() as session:
        def count(model):
            return session.execute(select(func.count()).select_from(model)).scalar_one()

        print(f"persons:       {count(Person):,}")
        print(f"source records:{count(SourceRecord):,}")
        pending_leads = session.execute(
            select(func.count(DiscoveryLead.id)).where(DiscoveryLead.status == "pending")
        ).scalar_one()
        pending_hooks = session.execute(
            select(func.count(WebhookDelivery.id)).where(WebhookDelivery.status == "pending")
        ).scalar_one()
        print(f"pending leads: {pending_leads:,}")
        print(f"pending hooks: {pending_hooks:,}")
    print("credentials:")
    for var in ("GITHUB_TOKEN", "SEMANTIC_SCHOLAR_API_KEY", "OPENALEX_MAILTO",
                "RIP_API_TOKEN", "RIP_LEAD_BATCH"):
        print(f"  {var:26} {'set' if os.environ.get(var) else 'NOT SET'}")


def cmd_worker(args) -> None:
    """Drain the discovery-lead queue continuously."""
    import time

    if _in_processes("worker", args):
        return
    from .discover import drain_leads

    init_db()
    import signal

    warn_missing_tokens(enriching=not args.no_enrich)

    stopping = {"now": False}

    def _stop(_signum, _frame):
        # finish the batch in flight, then exit — never abandon a partial commit
        if stopping["now"]:
            raise KeyboardInterrupt
        stopping["now"] = True
        print("\n  stop requested; finishing current batch…", flush=True)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"worker: draining leads every {args.poll_interval}s (ctrl-c to stop)")
    backoff = 0.0
    while not stopping["now"]:
        with SessionLocal() as session:
            ok, failed = drain_leads(
                session, limit=args.limit, source=args.source,
                enrich_chain=not args.no_enrich,
            )
        if ok or failed:
            print(f"  ingested {ok}, failed {failed}", flush=True)
        # every lead failing usually means a source is throttling us; back off
        # rather than burning through the queue marking leads as errors
        if failed and not ok:
            backoff = min(max(backoff * 2, args.poll_interval), 900.0)
            print(f"  all failed — backing off {backoff:.0f}s", flush=True)
        else:
            backoff = 0.0
        if args.once or stopping["now"]:
            break
        time.sleep(backoff or args.poll_interval)
    print("worker stopped cleanly")


def cmd_serve(args) -> None:
    """Serve the whole graph, writable, with every capability enabled."""
    import os

    import uvicorn

    from .db import DB_URL, READ_ONLY

    print(f"Seekr on http://{args.host}:{args.port}/ui")
    print(f"  database  {DB_URL}{'  (READ-ONLY)' if READ_ONLY else ''}")
    if READ_ONLY:
        print("  ! live search can find people but cannot keep them")
    print(f"  auth      {'bearer token required' if os.environ.get('RIP_API_TOKEN') else 'OPEN — set RIP_API_TOKEN'}")
    missing = [v for v in ("GITHUB_TOKEN", "EXA_API_KEY") if not os.environ.get(v)]
    if missing:
        print(f"  note      not set: {', '.join(missing)}")
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    if workers > 1 and DB_URL.startswith("sqlite"):
        print("  ! SQLite does not take concurrent writers — using one worker")
        workers = 1
    uvicorn.run("rip.api:app", host=args.host, port=args.port,
                workers=workers, reload=False)


def load_env(path: str = ".env") -> int:
    """Read .env into the environment without adding a dependency.

    Every command needs the API keys, and requiring `set -a; source .env`
    before each one is a step people forget — the symptom is a connector that
    silently returns nothing. Values already in the environment win, so an
    explicit VAR=... on the command line still overrides the file.
    """
    import os

    loaded = 0
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(prog="rip", description="Resource Intelligence Platform")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create database tables")

    p_ingest = sub.add_parser("ingest", help="ingest one identifier from a source")
    p_ingest.add_argument("source", help="connector name (github, openalex, web, ...)")
    p_ingest.add_argument("identifier", help="username / author id / url")
    p_ingest.add_argument(
        "--no-enrich", action="store_true",
        help="do not follow identity signals into other sources",
    )

    p_search = sub.add_parser("search-openalex", help="find OpenAlex author IDs for a name")
    p_search.add_argument("name")

    p_search_dblp = sub.add_parser("search-dblp", help="find dblp PIDs for a name")
    p_search_dblp.add_argument("name")

    p_search_s2 = sub.add_parser("search-s2", help="find Semantic Scholar author IDs for a name")
    p_search_s2.add_argument("name")

    p_refresh = sub.add_parser("refresh", help="re-fetch stale source records")
    p_refresh.add_argument("--older-than-hours", type=float, default=24.0)
    p_refresh.add_argument("--processes", type=int, default=1,
                           help="refresh in N processes, each on its share of the records")

    p_discover = sub.add_parser("discover", help="mine stored records for new people (leads)")
    p_discover.add_argument(
        "--github-contributors", action="store_true",
        help="also fetch contributors of known repos (live API calls)",
    )
    p_discover.add_argument("--max-repos", type=int, default=3)

    p_leads = sub.add_parser("ingest-leads", help="ingest pending discovery leads")
    p_leads.add_argument("--limit", type=int, default=25)
    p_leads.add_argument("--source", default=None)
    p_leads.add_argument("--no-enrich", action="store_true")
    p_leads.add_argument("--processes", type=int, default=1,
                         help="drain in N processes; each claims its own leads")

    p_review = sub.add_parser("review", help="review suspicious merges and duplicates")
    p_review.add_argument(
        "action", choices=["list", "triage", "approve", "split", "merge", "dismiss"])
    p_review.add_argument(
        "link_id", nargs="?", type=int,
        help="identity link id (approve/split) or merge-candidate id (merge/dismiss)",
    )
    p_review.add_argument(
        "--yes", action="store_true",
        help="triage: carry out the decisions instead of only reporting them",
    )

    p_reparse = sub.add_parser("reparse", help="re-normalize stored raw payloads (no network)")
    p_reparse.add_argument("--source", default=None)
    p_reparse.add_argument("--verbose", action="store_true")

    p_harvest = sub.add_parser("harvest", help="page a source's whole index into JSONL")
    p_harvest.add_argument("source", help="currently: openalex")
    p_harvest.add_argument("--out", required=True, help="output .jsonl or .jsonl.gz")
    p_harvest.add_argument("--limit", type=int, default=10000)
    p_harvest.add_argument("--filter", default=None, help="OpenAlex filter, e.g. works_count:>50")
    p_harvest.add_argument("--cursor", default="*", help="resume from a previous next_cursor")
    p_harvest.add_argument("--india", action="store_true",
                           help="restrict to India-affiliated/located people")

    p_bulk = sub.add_parser("bulk-ingest", help="stream a JSONL(.gz) dump into the graph")
    p_bulk.add_argument("source", help="connector name the dump belongs to")
    p_bulk.add_argument("--file", required=True)
    p_bulk.add_argument("--batch-size", type=int, default=500)
    p_bulk.add_argument("--limit", type=int, default=None)
    p_bulk.add_argument("--no-enrich", action="store_true", default=True,
                        help="(default) skip the enrichment chain during bulk load")

    p_home = sub.add_parser("find-homepages",
                            help="search the web for people's homepages and read them")
    p_home.add_argument("--limit", type=int, default=20, help="people to process")
    p_home.add_argument("--candidates", type=int, default=2, help="URLs to try per person")
    p_home.add_argument("--min-sources", type=int, default=1,
                        help="only people already corroborated across N sources")

    p_hooks = sub.add_parser("deliver-webhooks", help="POST queued webhook deliveries")
    p_hooks.add_argument("--limit", type=int, default=100)
    p_hooks.add_argument("--fail-threshold", type=int, default=0,
                         help="exit non-zero when failures exceed this")

    p_qstats = sub.add_parser("queue-stats", help="discovery-lead backlog and drain estimate")
    p_qstats.add_argument("--batch-size", type=int, default=100)

    sub.add_parser("check-db", help="engine, journal mode, queue depths, credentials")

    p_prot = sub.add_parser("audit-protected",
                            help="find protected-attribute material in stored free text")
    p_prot.add_argument("--yes", action="store_true", help="redact in place; otherwise dry-run")

    p_orgs = sub.add_parser("merge-orgs",
                            help="fold organizations that are one company spelled two ways")
    p_orgs.add_argument("--yes", action="store_true", help="actually merge; otherwise dry-run")

    p_purge = sub.add_parser("purge-nonpersons",
                             help="remove index entities stored as people (labs, conferences)")
    p_purge.add_argument("--yes", action="store_true", help="actually delete; otherwise dry-run")

    p_dedupe = sub.add_parser("dedupe", help="merge people stored twice, on evidence only")
    p_dedupe.add_argument("--yes", action="store_true", help="actually merge; otherwise dry-run")

    p_reindex = sub.add_parser("reindex", help="rebuild the search index from the tables")
    p_reindex.add_argument("--batch", type=int, default=1000)

    p_worker = sub.add_parser("worker", help="continuously drain the discovery-lead queue")
    p_worker.add_argument("--poll-interval", type=float, default=30.0)
    p_worker.add_argument("--limit", type=int, default=25)
    p_worker.add_argument("--source", default=None)
    p_worker.add_argument("--no-enrich", action="store_true")
    p_worker.add_argument("--once", action="store_true", help="single pass then exit")
    p_worker.add_argument("--processes", type=int, default=1,
                          help="run N workers; --limit is each one's batch size")

    p_serve = sub.add_parser("serve", help="run the API and UI (full build)")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="0.0.0.0 to accept connections from other machines")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--workers", type=int, default=1,
                         help="worker processes; >1 needs Postgres, not SQLite")

    args = parser.parse_args()
    if args.command == "init-db":
        init_db()
        print("database initialized")
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "search-openalex":
        cmd_search_openalex(args)
    elif args.command == "search-dblp":
        cmd_search_dblp(args)
    elif args.command == "search-s2":
        cmd_search_s2(args)
    elif args.command == "refresh":
        cmd_refresh(args)
    elif args.command == "discover":
        cmd_discover(args)
    elif args.command == "ingest-leads":
        cmd_ingest_leads(args)
    elif args.command == "review":
        cmd_review(args)
    elif args.command == "reparse":
        cmd_reparse(args)
    elif args.command == "harvest":
        cmd_harvest(args)
    elif args.command == "bulk-ingest":
        cmd_bulk_ingest(args)
    elif args.command == "find-homepages":
        cmd_find_homepages(args)
    elif args.command == "deliver-webhooks":
        cmd_deliver_webhooks(args)
    elif args.command == "queue-stats":
        cmd_queue_stats(args)
    elif args.command == "check-db":
        cmd_check_db(args)
    elif args.command == "purge-nonpersons":
        cmd_purge_nonpersons(args)
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "dedupe":
        cmd_dedupe(args)
    elif args.command == "audit-protected":
        cmd_audit_protected(args)
    elif args.command == "merge-orgs":
        cmd_merge_orgs(args)
    elif args.command == "worker":
        cmd_worker(args)
    elif args.command == "serve":
        cmd_serve(args)


if __name__ == "__main__":
    main()
