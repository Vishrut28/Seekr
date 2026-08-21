"""Web page connector — fetches ONE public page (no spidering).

Identifier: a URL. Used mainly by the enrichment chain to pick up a person's
homepage from a GitHub blog field or a dblp/ORCID researcher URL.

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

logger = logging.getLogger("rip.connectors.web")


class WebConnector(BaseConnector):
    source = "web"
    source_type = "personal_site"
    min_request_interval = 2.0

    def _robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = self._client.get(robots_url, timeout=10.0)
            if resp.status_code >= 400:
                return True  # no robots.txt published -> crawling permitted
            parser.parse(resp.text.splitlines())
        except Exception:
            return True
        return parser.can_fetch(self.default_headers()["User-Agent"], url)

    def fetch(self, identifier: str) -> NormalizedProfile:
        url = identifier if "://" in identifier else f"https://{identifier}"
        # robots.txt is checked FIRST and gates everything below. The render
        # services exist for pages that are technically hard (JavaScript,
        # bot-shields on public content) — never to reach a page whose owner
        # told us not to.
        if not self._robots_allows(url):
            raise PermissionError(f"robots.txt disallows fetching {url}")
        html = self._fetch_html(url)[:MAX_HTML]
        return self.normalize(url, html)

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
        return self.normalize(raw["url"], raw["html"])

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

    def normalize(self, url: str, html: str) -> NormalizedProfile:
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
