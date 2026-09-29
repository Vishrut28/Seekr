"""A one-person dossier: what Seekr knows, and where each part came from.

The shape follows the recruitment screener's report, because that is what the
work is for — an artifact a human reads before an interview. What it does not
follow is the screener's indirect rubric. Nothing here infers temperament,
character or personality from someone's public behaviour; every line is an
attribute a named source stated, with a link back to the source that stated
it. Judgement is the reader's job.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Affiliation,
    Authorship,
    Evidence,
    IdentityLink,
    Organization,
    Person,
    PersonKey,
    Publication,
    SourceRecord,
)


def _mark() -> str:
    """The logo's path data, or "" when the built frontend is not there."""
    svg = Path(__file__).resolve().parents[2] / "frontend/assets/mark.svg"
    if not svg.exists():
        return ""
    found = re.search(r'd="([^"]+)"', svg.read_text())
    return found.group(1) if found else ""


MARK = _mark()


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


TAG_CORROBORATED = "<span class='tag'>corroborated</span>"


def _link(url: str | None, text: str) -> str:
    """An anchor, or the plain text when there is nowhere to go.

    This existed as an escaped conditional inside an f-string, which needs
    Python 3.12 -- backslashes in f-strings are a syntax error before that,
    and pyproject says this project supports 3.10. The module did not import
    at all on the version it claims to run on; nothing noticed because nobody
    runs 3.10 here, and a linter found it in its first pass.
    """
    return f"<a href='{_esc(url)}'>{text}</a>" if url else text


def collect(session: Session, person: Person) -> dict:
    """Everything the dossier renders, gathered in one place."""
    evidence = session.execute(
        select(Evidence).where(Evidence.person_id == person.id)
    ).scalars().all()

    sources = sorted({e.source for e in evidence if e.source})
    corroborated = [e for e in evidence if e.verification_state == "corroborated"]

    # How completely do we know this person? The screener reports an evidence
    # coverage figure and it is the most useful number on the page, because it
    # tells the reader how much weight the rest of it can carry.
    dimensions = {
        "identity": bool(person.canonical_name),
        "affiliation": any(e.attribute_type == "affiliation" for e in evidence),
        "role": any(e.attribute_type == "role" for e in evidence),
        "location": bool(person.location or person.country),
        "expertise": any(e.attribute_type in ("skill", "research_interest") for e in evidence),
        "output": (session.scalar(
            select(func.count()).select_from(Authorship).where(Authorship.person_id == person.id)
        ) or 0) > 0,
        "profiles": bool(person.profile_urls),
        "corroboration": bool(corroborated),
    }
    coverage = round(100 * sum(dimensions.values()) / len(dimensions))

    affiliations = session.execute(
        select(Organization.name, Affiliation.role, Affiliation.relation,
               Affiliation.start_date, Affiliation.end_date)
        .join(Affiliation, Affiliation.organization_id == Organization.id)
        .where(Affiliation.person_id == person.id)
    ).all()

    works = session.execute(
        select(Publication.title, Publication.venue, Publication.published_date,
               Publication.citations, Publication.url)
        .join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person.id)
        .order_by(Publication.citations.desc().nullslast())
        .limit(8)
    ).all()

    records = session.execute(
        select(SourceRecord.source, SourceRecord.url, SourceRecord.last_observed)
        .join(IdentityLink, IdentityLink.source_record_id == SourceRecord.id)
        .where(IdentityLink.person_id == person.id)
        .order_by(SourceRecord.last_observed.desc())
    ).all()

    # The strongest evidence: claims more than one source agrees on, then the
    # rest by confidence. Same idea as the screener's evidence table.
    ranked = sorted(
        evidence,
        key=lambda e: (e.verification_state != "corroborated", -(e.confidence or 0)),
    )
    seen, strongest = set(), []
    for e in ranked:
        key = (e.attribute_type, str(e.value)[:60])
        if key in seen:
            continue
        seen.add(key)
        strongest.append(e)
        if len(strongest) >= 12:
            break

    strong_keys = session.execute(
        select(PersonKey.key_type, PersonKey.key_value)
        .where(PersonKey.person_id == person.id)
    ).all()
    orcid = next((v for k, v in strong_keys if k == "orcid"), None)

    topics = [e.value for e in evidence
              if e.attribute_type in ("research_interest", "skill", "specialization")]
    latest = max((e.published_at or e.observed_at for e in evidence
                  if (e.published_at or e.observed_at)), default=None)
    years = [str(d)[:4] for _t, _v, d, _c, _u in works if d]
    citations = sum(c or 0 for _t, _v, _d, c, _u in works)

    return {
        "person": person, "evidence": evidence, "sources": sources,
        "coverage": coverage, "dimensions": dimensions,
        "corroborated": len(corroborated), "affiliations": affiliations,
        "works": works, "records": records, "strongest": strongest,
        "topics": topics, "latest": latest, "citations": citations,
        "span": (min(years), max(years)) if years else None,
        "orcid": orcid, "keys": strong_keys,
        "rubric": _rubric(person, evidence, sources, corroborated, works,
                          affiliations, topics, latest, orcid),
        "unknowns": _unknowns(person, dimensions, affiliations, works, orcid),
        "narrative": _narrative(person, evidence, sources, works, affiliations,
                                topics, years),
    }


def _rubric(person, evidence, sources, corroborated, works, affiliations,
            topics, latest, orcid) -> list[dict]:
    """A scored table of how well this record is *evidenced*.

    The screener's rubric grades the person — original work, ownership,
    communication, career pattern. Seekr cannot see any of that, and guessing
    at it from public traces is the part of that design worth not copying.
    What Seekr can grade precisely is its own evidence: how many independent
    sources agree, how recent it is, how much of the person it covers. Same
    table, defensible contents.
    """
    from datetime import datetime, timezone

    def band(value, thresholds, weight, note):
        earned = 0.0
        for i, t in enumerate(thresholds):
            if value >= t:
                earned = weight * (i + 1) / len(thresholds)
        return {"points": round(earned, 1), "weight": weight, "note": note}

    months_old = None
    if latest:
        dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
        months_old = (datetime.now(timezone.utc) - dt).days / 30.4

    rows = [
        ("Identity strength", band(
            len(sources), [1, 2, 3], 12,
            f"Resolved across {len(sources)} source{'' if len(sources)==1 else 's'}"
            + ("; ORCID on file" if orcid else "; no ORCID"))),
        ("Corroboration", band(
            len(corroborated), [1, 3, 8], 12,
            f"{len(corroborated)} claim{'' if len(corroborated)==1 else 's'} confirmed by "
            "more than one source")),
        ("Published output", band(
            len(works), [1, 4, 8], 12,
            f"{len(works)} publication{'' if len(works)==1 else 's'} on file")),
        ("Expertise specificity", band(
            len(set(topics)), [1, 5, 15], 10,
            f"{len(set(topics))} distinct topics or skills stated by sources")),
        ("Affiliation depth", band(
            len(affiliations), [1, 2, 4], 10,
            f"{len(affiliations)} affiliation{'' if len(affiliations)==1 else 's'} on record")),
        ("Evidence volume", band(
            len(evidence), [5, 25, 100], 8,
            f"{len(evidence)} individual claims")),
        ("Recency", band(
            -(months_old if months_old is not None else 999), [-36, -18, -6], 8,
            "no dated evidence" if months_old is None
            else f"most recent evidence {months_old:.0f} months old")),
        ("Public presence", band(
            len(person.profile_urls or []), [1, 2, 4], 6,
            f"{len(person.profile_urls or [])} published profile links")),
    ]
    return [{"category": c, **v} for c, v in rows]


def _unknowns(person, dimensions, affiliations, works, orcid) -> list[str]:
    """What this record does NOT establish. The screener names these too, and
    it is the most useful part of any dossier: it tells the reader where their
    own judgement is still required."""
    out = []
    if not dimensions["corroboration"]:
        out.append("No claim here is confirmed by a second independent source.")
    if not works:
        out.append("No published work is on file, which is not evidence that none exists.")
    if not affiliations:
        out.append("No employer or institution is recorded.")
    if not (person.location or person.country):
        out.append("Location is unknown.")
    if not dimensions["role"]:
        out.append("No job title has been stated by any source.")
    if not orcid:
        out.append("No ORCID, so scholarly identity rests on name matching.")
    out.append("Nothing here speaks to performance, seniority, or how this person works.")
    return out


def _narrative(person, evidence, sources, works, affiliations, topics, years) -> str:
    """A short factual opening, assembled from the record rather than inferred."""
    bits = []
    where = person.current_organization or (affiliations[0][0] if affiliations else None)
    role = person.current_role
    if role and where:
        bits.append(f"{person.canonical_name} is recorded as {role} at {where}")
    elif where:
        bits.append(f"{person.canonical_name} is affiliated with {where}")
    else:
        bits.append(f"{person.canonical_name} appears in {len(sources)} of Seekr's sources")
    if person.location or person.country:
        bits.append(f"based in {person.location or person.country}")
    text = ", ".join(bits) + "."
    top = list(dict.fromkeys(topics))[:3]
    if top:
        text += " Sources associate them with " + ", ".join(top[:2])
        text += f" and {top[2]}." if len(top) > 2 else "."
    if works:
        span = f" between {min(years)} and {max(years)}" if years else ""
        text += (f" {len(works)} publication{'' if len(works)==1 else 's'} "
                 f"are on file{span}.")
    text += (f" The record is assembled from {', '.join(sources)}"
             if sources else "")
    return text + ("." if sources else "")


CSS = """
@page { size: A4; margin: 12mm 12mm 14mm; }
* { box-sizing: border-box; }
body { margin:0; font-family:-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
  color:#111113; background:#fff; font-size:9.2pt; line-height:1.42; }
.head { display:flex; align-items:flex-start; gap:12px; border-bottom:2px solid #111113;
  padding-bottom:11px; margin-bottom:14px; }
.mark { width:30px; height:30px; flex:none; }
h1 { font-size:19pt; margin:0; letter-spacing:-.02em; }
.sub { color:#6d6a66; font-size:9pt; margin-top:2px; }
.brandmark { margin-left:auto; text-align:right; font-size:8pt; color:#6d6a66; line-height:1.35; }
h2 { font-size:8pt; letter-spacing:.15em; text-transform:uppercase; color:#175fff;
  margin:15px 0 5px; font-weight:700; }
h2::before { content:"// "; color:#9c9791; }
p { margin:0 0 6px; }
table { width:100%; border-collapse:collapse; margin-top:2px; }
th { text-align:left; font-size:7pt; letter-spacing:.08em; text-transform:uppercase;
  color:#6d6a66; border-bottom:1px solid #d9d3cb; padding:4px 7px 4px 0; font-weight:700; }
td { padding:4.5px 7px 4.5px 0; border-bottom:1px solid #f0ece6; vertical-align:top; }
td.n, th.n { text-align:right; white-space:nowrap; }
.hero { display:flex; gap:26px; align-items:flex-start;
  background:#f6f3f0; border:1px solid #e2ddd6; border-radius:6px; padding:11px 14px; }
.hero .big { font-size:25pt; font-weight:600; letter-spacing:-.02em; line-height:1; }
.hero .cap { font-size:7pt; letter-spacing:.13em; text-transform:uppercase; color:#6d6a66;
  margin-bottom:3px; font-weight:700; }
.hero .col { min-width:96px; }
.bar { height:4px; background:#e2ddd6; border-radius:2px; overflow:hidden; margin-top:6px; }
.bar i { display:block; height:100%; background:#175fff; }
.pts { font-variant-numeric:tabular-nums; }
.tag { display:inline-block; background:#eef2ff; color:#175fff; border-radius:3px;
  padding:0 5px; font-size:7pt; font-weight:700; letter-spacing:.04em; }
.tag.warn { background:#fdf1e3; color:#a65b00; }
ul { margin:2px 0 0; padding-left:15px; }
li { margin-bottom:2.5px; }
a { color:#175fff; text-decoration:none; }
.foot { margin-top:16px; padding-top:8px; border-top:1px solid #e2ddd6;
  font-size:7.4pt; color:#6d6a66; line-height:1.45; }
"""


def render_html(data: dict) -> str:
    p = data["person"]
    rub = data["rubric"]
    earned = sum(r["points"] for r in rub)
    total = sum(r["weight"] for r in rub)
    normalized = round(100 * earned / total, 1) if total else 0

    strongest = "".join(
        f"<tr><td>{_esc(e.value)}</td><td>{_esc(e.attribute_type.replace('_',' '))}</td>"
        f"<td>{_esc(e.source)}</td>"
        f"<td>{TAG_CORROBORATED if e.verification_state == 'corroborated' else ''}</td>"
        f"<td class='n'>{_link(e.url, 'source') if e.url else ''}</td></tr>"
        for e in data["strongest"]
    )
    rubric = "".join(
        f"<tr><td>{_esc(r['category'])}</td>"
        f"<td class='n pts'>{r['points']} / {r['weight']}</td>"
        f"<td>{_esc(r['note'])}</td></tr>" for r in rub
    )
    affil = "".join(
        f"<tr><td>{_esc(o)}</td><td>{_esc(role or '—')}</td><td>{_esc(rel or '')}</td>"
        f"<td class='n'>{_esc(str(sd)[:10] if sd else '')}"
        f"{' – ' + str(ed)[:10] if ed else ''}</td></tr>"
        for o, role, rel, sd, ed in data["affiliations"][:8]
    )
    works = "".join(
        f"<tr><td>{_link(u, _esc(t))}</td>"
        f"<td>{_esc(v or '')}</td><td class='n'>{_esc(str(d)[:4] if d else '')}</td>"
        f"<td class='n'>{_esc(c if c is not None else '')}</td></tr>"
        for t, v, d, c, u in data["works"]
    )
    records = "".join(
        f"<tr><td>{_esc(src)}</td>"
        f"<td>{_link(u, _esc(u)[:74]) if u else ''}</td>"
        f"<td class='n'>{_esc(str(seen)[:10] if seen else '')}</td></tr>"
        for src, u, seen in data["records"][:14]
    )
    unknowns = "".join(f"<li>{_esc(u)}</li>" for u in data["unknowns"])
    profiles = " ".join(
        f"<a href='{_esc(u)}'>{_esc(u.split('//')[-1].split('/')[0])}</a>"
        for u in (p.profile_urls or [])[:8]
    )
    mark = (f'<svg class="mark" viewBox="0 0 512 512"><path d="{MARK}" fill="#111113" '
            f'fill-rule="evenodd"/></svg>') if MARK else ""
    subtitle = " · ".join(x for x in (p.current_role, p.current_organization,
                                      p.location or p.country) if x)
    conf = ("High" if data["coverage"] >= 75 else
            "Moderate" if data["coverage"] >= 45 else "Low")

    return f"""<!doctype html><meta charset="utf-8"><title>{_esc(p.canonical_name)} — Seekr</title>
<style>{CSS}</style>
<div class="head">{mark}
  <div><h1>{_esc(p.canonical_name)}</h1><div class="sub">{_esc(subtitle) or '&nbsp;'}</div></div>
  <div class="brandmark">Seekr · Deccan AI<br>Evidence dossier<br>{datetime.now(timezone.utc):%d %B %Y}</div>
</div>

<h2>Overview</h2>
<p>{_esc(data['narrative'])}</p>

<h2>Executive Summary</h2>
<div class="hero">
  <div class="col"><div class="cap">Evidence score</div><div class="big">{normalized}</div>
    <div class="bar"><i style="width:{normalized}%"></i></div></div>
  <div class="col"><div class="cap">Coverage</div><div class="big">{data['coverage']}%</div></div>
  <div class="col"><div class="cap">Confidence</div><div class="big">{conf}</div></div>
  <div style="flex:1">
    <div class="cap">What this record rests on</div>
    {len(data['evidence'])} claims from {len(data['sources'])} source{'' if len(data['sources'])==1 else 's'}
    ({', '.join(data['sources']) or 'none'}),
    {data['corroborated']} confirmed by more than one.
    {len(data['works'])} publications, {data['citations']:,} citations on file.
    <div style="margin-top:5px">This is a record of what public sources state, scored on how
    well evidenced it is — not an assessment of the person.</div>
  </div>
</div>

<h2>Evidence Dimensions</h2>
<table><tr><th>Dimension</th><th class="n">Points</th><th>Basis</th></tr>{rubric}
<tr><td><b>Total</b></td><td class="n pts"><b>{round(earned,1)} / {total}</b></td>
<td><b>Normalized: {normalized}/100</b></td></tr></table>

<h2>Strongest Evidence</h2>
<table><tr><th>Claim</th><th>Type</th><th>Source</th><th>Standing</th><th class="n"></th></tr>{strongest}</table>

{'<h2>Affiliations</h2><table><tr><th>Organisation</th><th>Role</th><th>Relation</th><th class="n">Dates</th></tr>' + affil + '</table>' if affil else ''}

{'<h2>Selected Publications</h2><table><tr><th>Title</th><th>Venue</th><th class="n">Year</th><th class="n">Cites</th></tr>' + works + '</table>' if works else ''}

{'<h2>Public Profiles</h2><p>' + profiles + '</p>' if profiles else ''}

<h2>Unknowns</h2>
<ul>{unknowns}</ul>

<h2>Provenance</h2>
<table><tr><th>Source</th><th>Record</th><th class="n">Last seen</th></tr>{records}</table>

<div class="foot">
  Every line above is an attribute a named source stated, with the record it came from.
  The score measures how well evidenced this record is, not how good the person is:
  Seekr does not infer performance, temperament or personal circumstances, and
  protected attributes are redacted where they are collected. Judgement is the reader's.
</div>
"""
