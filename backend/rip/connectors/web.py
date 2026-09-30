"""Web page connector — a public homepage and a few pages it links to.

Identifier: a URL. Used mainly by the enrichment chain to pick up a person's
homepage from a GitHub blog field or a dblp/ORCID researcher URL.

Not a crawler: besides the page itself, at most MAX_SUBPAGES pages on the
same site are read, and only ones the page links to as About, CV,
Publications or Research — where academic sites keep the ORCID, the CV link
and the profile links a landing page often leaves out. Every page is checked
against robots.txt, and subpages are fetched directly, never through a paid
renderer.

Extracts only what a page publishes about itself: title, JSON-LD Person /
ProfilePage blocks, Open Graph metadata, emails the page displays, and links
to known profile hosts (which become strong resolution keys). No login, no
paywall, no anti-bot circumvention; robots.txt is honored before fetching.
"""

import json
import logging
import os
import re
import urllib.robotparser
from html import unescape
from urllib.parse import urljoin, urlparse

from ..normalize import EvidenceItem, NormalizedProfile, OrgAffiliation, normalize_url
from .base import BaseConnector

PROFILE_HOSTS = (
    "orcid.org", "github.com", "dblp.org", "scholar.google.com",
    "openalex.org", "semanticscholar.org", "huggingface.co",
    "stackoverflow.com", "linkedin.com",
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# A document only counts as a CV when the page itself says so — either in the
# link text or the file name. We never guess a URL or construct one.
# word-boundary match: "cv" must be its own word, or "CVPR 2021" reads as a CV
CV_LABEL_RE = re.compile(r"\b(cv|resum[eé]s?|curriculum\s*vitae)\b", re.I)
CV_HREF_RE = re.compile(r"(^|[/_\-.])(cv|resume|resume?s|curriculum[-_]?vitae)([/_\-.]|$)", re.I)
ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
ORCID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4}-\d{3}[\dX])\b")
TAG_RE = re.compile(r"<[^>]+>")
MAX_HTML = 400_000
MAX_SUBPAGES = int(os.environ.get("RIP_WEB_MAX_SUBPAGES", "3"))
# What a subpage is called, in its link text or its last path segment, most
# useful first. Whole labels only: "About this dataset" is not an About page.
SUBPAGE_KINDS = (
    ("about", re.compile(r"^\s*(about(\s+me)?|bio(graphy)?)\s*$", re.I),
     re.compile(r"^(about|about-me|bio|biography)$", re.I)),
    ("cv", re.compile(r"^\s*(cv|curriculum\s+vitae|r[eé]sum[eé])\s*$", re.I),
     re.compile(r"^(cv|vita|resume|curriculum-vitae)$", re.I)),
    ("publications", re.compile(r"^\s*(publications?|papers|selected\s+publications)\s*$", re.I),
     re.compile(r"^(publications?|papers|pubs)$", re.I)),
    ("research", re.compile(r"^\s*research(\s+interests)?\s*$", re.I),
     re.compile(r"^research$", re.I)),
)
NOT_A_PAGE = (".pdf", ".doc", ".docx", ".ps", ".gz", ".zip", ".png", ".jpg", ".jpeg",
              ".gif", ".svg", ".bib", ".tex", ".ppt", ".pptx")

logger = logging.getLogger("rip.connectors.web")


class WebConnector(BaseConnector):
    source = "web"
    source_type = "personal_site"
    min_request_interval = 2.0

    def _robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        site = f"{parsed.scheme}://{parsed.netloc}"
        # one robots.txt per site per connector, not one per page
        cache: dict[str, urllib.robotparser.RobotFileParser | None] = (
            self.__dict__.setdefault("_robots_cache", {}))
        if site not in cache:
            parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                resp = self._client.get(f"{site}/robots.txt", timeout=10.0)
                if resp.status_code >= 400:
                    parser = None  # no robots.txt published -> crawling permitted
                elif parser is not None:
                    parser.parse(resp.text.splitlines())
            except Exception:
                parser = None
            cache[site] = parser
        cached = cache[site]
        if cached is None:
            return True
        return cached.can_fetch(self.default_headers()["User-Agent"], url)

    def fetch(self, identifier: str) -> NormalizedProfile:
        url = identifier if "://" in identifier else f"https://{identifier}"
        # robots.txt is checked FIRST and gates everything below. The render
        # services exist for pages that are technically hard (JavaScript,
        # bot-shields on public content) — never to reach a page whose owner
        # told us not to.
        if not self._robots_allows(url):
            raise PermissionError(f"robots.txt disallows fetching {url}")
        html = self._fetch_html(url)[:MAX_HTML]
        pages = []
        for sub_url in self.subpages(url, html):
            if not self._robots_allows(sub_url):
                continue
            try:
                sub_html = self.get_text(sub_url)
            except Exception as exc:  # a missing subpage costs the page nothing
                logger.info("subpage %s not fetched: %s", sub_url, exc)
                continue
            if sub_html:
                pages.append({"url": sub_url, "html": sub_html[:MAX_HTML]})
        return self.normalize(url, html, pages)

    @staticmethod
    def subpages(url: str, html: str, limit: int | None = None) -> list[str]:
        """Same-site pages this page links to as About, CV, Publications or
        Research, most useful kind first."""
        limit = MAX_SUBPAGES if limit is None else limit
        if limit <= 0:
            return []
        home_host = (urlparse(url).netloc or "").lower().removeprefix("www.")
        here = normalize_url(url)
        best: dict[str, str] = {}
        for href, anchor_html in ANCHOR_RE.findall(html):
            absolute = urljoin(url, unescape(href)).split("#")[0]
            parts = urlparse(absolute)
            if parts.scheme not in ("http", "https"):
                continue
            if (parts.netloc or "").lower().removeprefix("www.") != home_host:
                continue  # another site is not this person's page
            if parts.path.lower().endswith(NOT_A_PAGE) or parts.query:
                continue
            if normalize_url(absolute) == here:
                continue
            text = unescape(TAG_RE.sub(" ", anchor_html))
            segment = parts.path.rstrip("/").rsplit("/", 1)[-1]
            segment = re.sub(r"\.(html?|php|aspx?)$", "", segment, flags=re.I)
            for kind, label_re, path_re in SUBPAGE_KINDS:
                if kind not in best and (label_re.match(text) or path_re.match(segment)):
                    best[kind] = absolute
                    break
        ordered = [best[kind] for kind, _, _ in SUBPAGE_KINDS if kind in best]
        return list(dict.fromkeys(ordered))[:limit]

    def _fetch_html(self, url: str) -> str:
        """Plain request first; fall back to a render service only if needed.

        Each fallback is skipped when its key is unset, so a default install
        makes no third-party calls. TinyFish Fetch is free (no credits). The
        others consume a monthly quota: Firecrawl 1,000, ZenRows 5,000,
        ScrapingBee 1,000 trial. TinyFish Agent/Browser are never called.
        """
        try:
            html = self.get_text(url)
            if html and len(html) > 500:
                return html
            logger.info("thin response from %s (%s bytes), trying a renderer", url, len(html or ""))
        except Exception as exc:
            logger.info("direct fetch failed for %s (%s), trying a renderer", url, exc)

        for name, fetcher in (
            ("tinyfish", self._via_tinyfish),
            ("firecrawl", self._via_firecrawl),
            ("zenrows", self._via_zenrows),
            ("scrapingbee", self._via_scrapingbee),
        ):
            if not os.environ.get(f"{name.upper()}_API_KEY"):
                continue
            try:
                html = fetcher(url)
                if html and len(html) > 200:
                    logger.info("fetched %s via %s", url, name)
                    return html
            except Exception as exc:
                logger.warning("%s failed for %s: %s", name, url, exc)
        raise RuntimeError(f"could not fetch {url} by any available method")

    def _via_tinyfish(self, url: str) -> str:
        from ..tinyfish import fetch_html

        return fetch_html(url, client=self._client)

    def _via_firecrawl(self, url: str) -> str:
        resp = self._client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"},
            json={"url": url, "formats": ["html"]},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        return data.get("html") or data.get("rawHtml") or ""

    def _via_zenrows(self, url: str) -> str:
        resp = self._client.get(
            "https://api.zenrows.com/v1/",
            params={"apikey": os.environ["ZENROWS_API_KEY"], "url": url},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.text

    def _via_scrapingbee(self, url: str) -> str:
        resp = self._client.get(
            "https://app.scrapingbee.com/api/v1/",
            params={"api_key": os.environ["SCRAPINGBEE_API_KEY"], "url": url},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.text

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(raw["url"], raw["html"], raw.get("pages"))

    def _json_ld(self, html: str) -> list[dict]:
        blocks = []
        for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I,
        ):
            try:
                data = json.loads(match.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                continue
            blocks.extend(data if isinstance(data, list) else [data])
        return [b for b in blocks if isinstance(b, dict)]

    def normalize(self, url: str, html: str, pages: list[dict] | None = None) -> NormalizedProfile:
        """The page, plus what its subpages add. The page speaks for the
        person first: a subpage only fills what the page left out (name,
        summary, ORCID) and adds links, CV documents and declared skills."""
        profile = self._normalize_page(url, html)
        for page in pages or []:
            sub = self._normalize_page(page["url"], page["html"])
            profile.name = profile.name or sub.name
            profile.summary = profile.summary or sub.summary
            profile.orcid = profile.orcid or sub.orcid
            profile.emails = list(dict.fromkeys([*profile.emails, *sub.emails]))[:3]
            profile.linked_urls = list(dict.fromkeys([*profile.linked_urls, *sub.linked_urls]))[:25]
            known_orgs = {o.name.lower() for o in profile.organizations}
            profile.organizations += [o for o in sub.organizations
                                      if o.name.lower() not in known_orgs]
            have = {(e.attribute_type, e.value) for e in profile.evidence}
            has_bio = any(e.attribute_type == "bio" for e in profile.evidence)
            for item in sub.evidence:
                if (item.attribute_type, item.value) in have:
                    continue
                if item.attribute_type == "bio" and has_bio:
                    continue
                profile.evidence.append(item)
                have.add((item.attribute_type, item.value))
        if pages:
            profile.raw = {"url": url, "html": html, "pages": list(pages)}
        return profile

    def _normalize_page(self, url: str, html: str) -> NormalizedProfile:
        external_id = normalize_url(url) or url

        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = unescape(TAG_RE.sub("", title_match.group(1))).strip() if title_match else None

        def meta(prop: str) -> str | None:
            m = re.search(
                rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
                html, re.I,
            )
            return unescape(m.group(1)).strip() if m else None

        name = None
        summary = meta("og:description") or meta("description")
        organizations: list[OrgAffiliation] = []
        evidence: list[EvidenceItem] = []

        for block in self._json_ld(html):
            btype = block.get("@type")
            types = btype if isinstance(btype, list) else [btype]
            if "Person" not in types:
                continue
            name = name or block.get("name")
            summary = summary or block.get("description")
            affiliation = block.get("affiliation") or block.get("worksFor")
            if isinstance(affiliation, dict) and affiliation.get("name"):
                organizations.append(
                    OrgAffiliation(name=affiliation["name"], is_current=True, url=url)
                )
            elif isinstance(affiliation, str) and affiliation:
                organizations.append(OrgAffiliation(name=affiliation, is_current=True, url=url))
            job = block.get("jobTitle")
            if job and organizations:
                organizations[0].role = job if isinstance(job, str) else None
            for skill in block.get("knowsAbout") or []:
                if isinstance(skill, str):
                    evidence.append(
                        EvidenceItem(
                            attribute_type="skill", value=skill[:512],
                            extracted_info="declared on personal site (JSON-LD knowsAbout)",
                            url=url, confidence=0.55,
                        )
                    )

        name = name or meta("og:title") or title

        linked: list[str] = []
        for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
            absolute = urljoin(url, href)
            host = (urlparse(absolute).netloc or "").lower().removeprefix("www.")
            if any(host == h or host.endswith("." + h) for h in PROFILE_HOSTS):
                linked.append(absolute.split("?")[0])
        linked = list(dict.fromkeys(linked))[:25]

        orcid = None
        orcid_match = ORCID_RE.search(html)
        if orcid_match:
            orcid = orcid_match.group(1)

        emails = list(dict.fromkeys(
            e for e in EMAIL_RE.findall(html)
            if not e.lower().endswith((".png", ".jpg", ".gif", ".svg"))
        ))[:3]

        if summary:
            evidence.append(
                EvidenceItem(
                    attribute_type="bio", value=summary[:512], url=url, confidence=0.5
                )
            )

        # CV / resume links this page actually publishes. Recorded as evidence
        # so the link keeps its provenance: the page it was found on, and the
        # anchor text that identified it. Nothing is inferred or synthesised.
        for href, anchor_html in ANCHOR_RE.findall(html):
            text = unescape(TAG_RE.sub(" ", anchor_html)).strip()
            absolute = urljoin(url, href)
            if not absolute.lower().startswith(("http://", "https://")):
                continue
            path = urlparse(absolute).path
            label_hit = CV_LABEL_RE.search(text) is not None
            href_hit = CV_HREF_RE.search(path or "") is not None
            if not (label_hit or href_hit):
                continue
            evidence.append(
                EvidenceItem(
                    attribute_type="cv_url",
                    value=absolute[:512],
                    extracted_info=(
                        f'linked as "{text[:80]}" on {url}' if text
                        else f"CV-named file linked on {url}"
                    ),
                    url=url,
                    confidence=0.8 if label_hit else 0.6,
                )
            )

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=external_id,
            url=url,
            raw={"url": url, "html": html},
            name=name[:255] if name else None,
            summary=summary,
            orcid=orcid,
            emails=emails,
            websites=[url],
            linked_urls=linked,
            organizations=organizations,
            evidence=evidence,
        )
