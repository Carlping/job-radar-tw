"""Bounded, same-host, robots-aware discovery of public JobPosting pages.

Sitemap feeds are incremental hints, not complete job inventories. This
adapter deliberately never implies that absent postings are closed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from .models import CompanyConfig, RawJob

logger = logging.getLogger(__name__)

MAX_XML_BYTES = 2_000_000
MAX_JOB_PAGES = 40
MAX_SITEMAPS = 10
MAX_LISTED_URLS = 5_000


class SitemapError(RuntimeError):
    """A sitemap cannot provide trustworthy, accessible job data."""


def _public_url(value: str, allowed_host: str | None = None) -> bool:
    """Require an HTTPS public hostname; reject offsite and local destinations."""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        if url.scheme != "https" or not host or url.username or url.password:
            return False
        if url.port not in (None, 443) or url.fragment:
            return False
        if allowed_host is not None and host != allowed_host:
            return False
        if host in ("localhost", "localhost.localdomain") or host.endswith(
            (".local", ".localhost", ".internal", ".test")
        ):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return "." in host
        return False
    except ValueError:
        return False


def _robots_groups(text: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            if agents:
                groups.append((agents, rules))
                agents, rules = [], []
            continue
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ("allow", "disallow") and agents:
            rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    return groups


def robots_allowed(robots: str, url: str, user_agent: str = "JobRadarTW") -> bool:
    """RFC 9309 longest-matching rule with Allow winning equal-length ties.

    Handles wildcard and '$' end-of-path rules, which Python 3.12's
    RobotFileParser may interpret differently when Disallow: / is present.
    """
    groups = _robots_groups(robots)
    token = user_agent.lower().split("/", 1)[0]
    specific = [rules for agents, rules in groups if any(a != "*" and a in token for a in agents)]
    selected = specific or [rules for agents, rules in groups if "*" in agents]
    rules = [rule for group in selected for rule in group]
    target = urlsplit(url).path or "/"
    if urlsplit(url).query:
        target += "?" + urlsplit(url).query
    best_length = -1
    best_allow = True
    for kind, pattern in rules:
        if not pattern:
            continue
        end = pattern.endswith("$")
        literal = pattern[:-1] if end else pattern
        expression = "^" + re.escape(literal).replace(r"\*", ".*")
        if end:
            expression += "$"
        if not re.match(expression, target):
            continue
        length = len(literal.replace("*", ""))
        allow = kind == "allow"
        if length > best_length or (length == best_length and allow):
            best_length, best_allow = length, allow
    return best_allow


def _jobpostings(node: object) -> list[dict]:
    if isinstance(node, list):
        return [result for item in node for result in _jobpostings(item)]
    if not isinstance(node, dict):
        return []
    kinds = node.get("@type")
    is_job = kinds == "JobPosting" or (
        isinstance(kinds, list) and "JobPosting" in kinds
    )
    found = [node] if is_job else []
    if isinstance(node.get("@graph"), list):
        found.extend(_jobpostings(node["@graph"]))
    return found


def _location(value: object) -> str:
    if isinstance(value, list):
        return "; ".join(part for item in value if (part := _location(item)))
    if isinstance(value, dict):
        address = value.get("address", value)
        if isinstance(address, dict):
            parts = [
                address.get("streetAddress"),
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            return ", ".join(str(x) for x in parts if x)
        return str(address) if address else ""
    return str(value) if value else ""


def _date(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _xml_locations(data: bytes) -> tuple[str, list[tuple[str, str]]]:
    if len(data) > MAX_XML_BYTES:
        raise SitemapError("Sitemap XML exceeds the size limit")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise SitemapError("Invalid sitemap XML") from exc
    name = root.tag.rsplit("}", 1)[-1]
    if name not in ("sitemapindex", "urlset"):
        raise SitemapError("XML is not a sitemap index or URL set")
    entries = []
    for entry in root:
        tag = entry.tag.rsplit("}", 1)[-1]
        if tag not in ("sitemap", "url"):
            continue
        fields = {
            field.tag.rsplit("}", 1)[-1]: field.text
            for field in entry
            if field.text
        }
        loc = fields.get("loc")
        if loc:
            entries.append((loc.strip(), (fields.get("lastmod") or "").strip()))
        if len(entries) >= MAX_LISTED_URLS:
            break
    return name, entries


async def fetch_sitemap_jobs(
    company: CompanyConfig, client: httpx.AsyncClient
) -> list[RawJob]:
    config = company.ats_config
    root_url = str(company.careers_url)
    host = (urlsplit(root_url).hostname or "").lower()
    if not _public_url(root_url):
        raise SitemapError("Sitemap careers_url must be an HTTPS public hostname")
    origin = "https://" + host
    robots_url = origin + "/robots.txt"
    robots_text = ""
    # Follow no redirects: reject redirects before they can reach an offsite or private URL.
    robots_response = await client.get(robots_url, follow_redirects=False)
    if robots_response.status_code == 200:
        robots_text = robots_response.text
    elif robots_response.status_code != 404:
        raise SitemapError(
            f"robots.txt unavailable for {company.slug}: HTTP {robots_response.status_code}"
        )
    if not robots_allowed(robots_text, root_url):
        raise SitemapError("Official careers URL is disallowed by robots.txt")

    declared = []
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            declared.append(line.split(":", 1)[1].strip())
    explicit = config.get("sitemap_url")
    candidates = (
        [str(explicit)]
        if explicit
        else declared + [urljoin(origin + "/", "sitemap.xml")]
    )
    max_sitemaps = min(max(int(config.get("max_sitemaps", 5)), 1), MAX_SITEMAPS)
    max_pages = min(max(int(config.get("max_job_pages", 20)), 1), MAX_JOB_PAGES)
    hint = str(config.get("job_url_contains", "")).lower()
    pending = [(url, 0) for url in candidates if _public_url(url, host)]
    visited: set[str] = set()
    job_urls: dict[str, str] = {}

    while pending and len(visited) < max_sitemaps:
        sitemap_url, depth = pending.pop(0)
        if sitemap_url in visited or not robots_allowed(robots_text, sitemap_url):
            continue
        visited.add(sitemap_url)
        resp = await client.get(sitemap_url, follow_redirects=False)
        if resp.status_code != 200:
            logger.info("Skipping unavailable sitemap for %s: HTTP %d", company.slug, resp.status_code)
            continue
        kind, entries = _xml_locations(resp.content)
        for loc, lastmod in entries:
            if not _public_url(loc, host):
                continue
            if kind == "sitemapindex":
                if depth < 2 and len(pending) + len(visited) < max_sitemaps:
                    pending.append((loc, depth + 1))
            else:
                path = urlsplit(loc).path.lower()
                is_job = (
                    hint in path if hint else any(
                        term in path for term in ("/job/", "/jobs/", "/career/", "/careers/", "/vacanc", "/position/")
                    )
                )
                if is_job and robots_allowed(robots_text, loc):
                    job_urls[loc] = lastmod
            if len(job_urls) >= MAX_LISTED_URLS:
                break

    if not job_urls:
        raise SitemapError("No permitted job-looking URLs found in official sitemaps")

    urls = sorted(job_urls, key=lambda url: (job_urls[url], url), reverse=True)[:max_pages]
    rows: list[RawJob] = []
    ids: set[str] = set()
    for i, url in enumerate(urls):
        if i:
            await asyncio.sleep(0.25)
        response = await client.get(url, follow_redirects=False)
        if response.status_code in (403, 429):
            raise SitemapError(
                f"Official job page access denied for {company.slug}: HTTP {response.status_code}"
            )
        if response.status_code != 200:
            continue
        if len(response.content) > MAX_XML_BYTES:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        postings: list[dict] = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                postings.extend(_jobpostings(json.loads(script.string or "null")))
            except json.JSONDecodeError:
                continue
        for item in postings:
            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            identifier = item.get("identifier")
            raw_id = identifier.get("value") if isinstance(identifier, dict) else None
            stable_id = str(raw_id or url)
            if stable_id in ids:
                continue
            ids.add(stable_id)
            try:
                rows.append(
                    RawJob(
                        source_company=company.slug,
                        external_job_id="sitemap:" + stable_id,
                        title=title,
                        location_raw=_location(
                            item.get("jobLocation") or item.get("applicantLocationRequirements")
                        ),
                        description_raw=BeautifulSoup(
                            str(item.get("description") or ""), "html.parser"
                        ).get_text(" ", strip=True),
                        posted_at=_date(item.get("datePosted")),
                        url=url,
                        metadata={"sitemap": {"sitemap_url": url, "lastmod": job_urls[url]}},
                    )
                )
            except ValueError:
                logger.warning("Skipping malformed sitemap JobPosting for %s", company.slug)
    if not rows:
        raise SitemapError("No valid JobPosting JSON-LD found among sampled sitemap pages")
    return rows
