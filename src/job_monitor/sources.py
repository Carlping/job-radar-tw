from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import AtsType, CompanyConfig, RawJob

logger = logging.getLogger(__name__)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _html_text(value: str | None) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def _usable_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _item_summary(item: Any) -> str:
    if not isinstance(item, dict):
        return repr(item)[:300]
    keys = (
        "id",
        "title",
        "text",
        "name",
        "externalPath",
        "absolute_url",
        "hostedUrl",
        "applyUrl",
        "jobUrl",
        "locationsText",
        "postedOn",
        "bulletFields",
    )
    return repr({key: item[key] for key in keys if key in item})[:500]


def _warn_skipped_item(source: str, company_slug: str, reason: str, item: Any) -> None:
    logger.warning(
        "Skipping malformed %s posting for %s (%s): %s",
        source,
        company_slug,
        reason,
        _item_summary(item),
    )


def _append_raw_job(
    jobs: list[RawJob],
    source: str,
    company_slug: str,
    item: Any,
    **fields: Any,
) -> None:
    try:
        jobs.append(RawJob(**fields))
    except ValidationError:
        _warn_skipped_item(source, company_slug, "invalid fields", item)


class SourceError(RuntimeError):
    pass


class WorkdayBlocked(SourceError):
    """A Workday request blocked by rate limits or an anti-bot challenge."""

    def __init__(
        self,
        status_code: int,
        method: str,
        kind: str,
        retry_after: float | None = None,
    ):
        self.kind = kind
        self.retry_after = retry_after
        retry_hint = f"; retry_after={retry_after:g}s" if retry_after is not None else ""
        super().__init__(f"Workday {method} HTTP {status_code} ({kind}){retry_hint}")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - datetime.now(UTC)).total_seconds())


def source_error_kind(exc: BaseException) -> str:
    """Stable, low-cardinality error category used for alert deduplication."""
    if isinstance(exc, WorkdayBlocked):
        return f"workday_{exc.kind}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "network"
    if isinstance(exc, SourceError):
        return "invalid_source_response"
    return type(exc).__name__.lower()[:80]


class JobSource(ABC):
    def __init__(self, company: CompanyConfig, client: httpx.AsyncClient):
        self.company = company
        self.client = client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.HTTPError, SourceError)),
        reraise=True,
    )
    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

    @abstractmethod
    async def fetch(self) -> list[RawJob]: ...


class GreenhouseSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        token = self.company.ats_config["board_token"]
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        payload = await self.get_json(url)
        jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            if not isinstance(item, dict):
                _warn_skipped_item("Greenhouse", self.company.slug, "not an object", item)
                continue
            title = item.get("title")
            item_url = item.get("absolute_url")
            if not _usable_text(title):
                _warn_skipped_item("Greenhouse", self.company.slug, "missing title", item)
                continue
            if not _usable_text(item_url):
                _warn_skipped_item("Greenhouse", self.company.slug, "missing URL", item)
                continue
            location = item.get("location")
            location_name = location.get("name", "") if isinstance(location, dict) else ""
            _append_raw_job(
                jobs,
                "Greenhouse",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=(str(item.get("id")) if item.get("id") is not None else None),
                title=title,
                location_raw=location_name,
                description_raw=_html_text(item.get("content")),
                posted_at=_parse_datetime(item.get("updated_at")),
                url=item_url,
                metadata={"departments": item.get("departments", [])},
            )
        return jobs


class LeverSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        site = self.company.ats_config["site"]
        payload = await self.get_json(f"https://api.lever.co/v0/postings/{site}?mode=json")
        jobs: list[RawJob] = []
        for item in payload:
            if not isinstance(item, dict):
                _warn_skipped_item("Lever", self.company.slug, "not an object", item)
                continue
            title = item.get("text")
            item_url = item.get("hostedUrl") or item.get("applyUrl")
            if not _usable_text(title):
                _warn_skipped_item("Lever", self.company.slug, "missing title", item)
                continue
            if not _usable_text(item_url):
                _warn_skipped_item("Lever", self.company.slug, "missing URL", item)
                continue
            categories = item.get("categories")
            categories = categories if isinstance(categories, dict) else {}
            _append_raw_job(
                jobs,
                "Lever",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=str(item.get("id")) if item.get("id") is not None else None,
                title=title,
                location_raw=categories.get("location", ""),
                description_raw=_html_text(item.get("descriptionPlain") or item.get("description")),
                posted_at=_parse_datetime(item.get("createdAt")),
                url=item_url,
                metadata={"categories": categories},
            )
        return jobs


class AshbySource(JobSource):
    async def fetch(self) -> list[RawJob]:
        board = self.company.ats_config["board_name"]
        payload = await self.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
        jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            if not isinstance(item, dict):
                _warn_skipped_item("Ashby", self.company.slug, "not an object", item)
                continue
            title = item.get("title")
            item_url = item.get("jobUrl") or item.get("applyUrl")
            if not _usable_text(title):
                _warn_skipped_item("Ashby", self.company.slug, "missing title", item)
                continue
            if not _usable_text(item_url):
                _warn_skipped_item("Ashby", self.company.slug, "missing URL", item)
                continue
            _append_raw_job(
                jobs,
                "Ashby",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=str(item.get("id") or item_url),
                title=title,
                location_raw=item.get("location", ""),
                description_raw=_html_text(
                    item.get("descriptionHtml") or item.get("descriptionPlain")
                ),
                posted_at=_parse_datetime(item.get("publishedAt")),
                url=item_url,
                metadata={"department": item.get("department")},
            )
        return jobs


class SmartRecruitersSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        identifier = self.company.ats_config["company_identifier"]
        base = f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings"
        offset = 0
        jobs: list[RawJob] = []
        while True:
            payload = await self.get_json(base, params={"limit": 100, "offset": offset})
            content = payload.get("content", [])
            for item in content:
                if not isinstance(item, dict):
                    _warn_skipped_item("SmartRecruiters", self.company.slug, "not an object", item)
                    continue
                item_id = item.get("id")
                title = item.get("name")
                if item_id is None:
                    _warn_skipped_item("SmartRecruiters", self.company.slug, "missing ID", item)
                    continue
                if not _usable_text(title):
                    _warn_skipped_item("SmartRecruiters", self.company.slug, "missing title", item)
                    continue
                detail = await self.get_json(f"{base}/{item_id}")
                location = item.get("location")
                location = location if isinstance(location, dict) else {}
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                description = " ".join(
                    _html_text(section.get("text"))
                    for section in sections.values()
                    if isinstance(section, dict)
                )
                _append_raw_job(
                    jobs,
                    "SmartRecruiters",
                    self.company.slug,
                    item,
                    source_company=self.company.slug,
                    external_job_id=str(item_id),
                    title=title,
                    location_raw=", ".join(
                        str(location.get(key, ""))
                        for key in ("city", "region", "country")
                        if location.get(key)
                    ),
                    description_raw=description,
                    posted_at=_parse_datetime(item.get("releasedDate")),
                    url=item.get("ref")
                    or f"https://jobs.smartrecruiters.com/{identifier}/{item_id}",
                )
            offset += len(content)
            if not content or offset >= int(payload.get("totalFound", offset)):
                break
        return jobs


class WorkdaySource(JobSource):
    # Be polite to the Workday tenant. Do not attempt to defeat challenges.
    REQUEST_SPACING_SECONDS = 1.0
    RATE_LIMIT_RETRIES = 1
    MAX_RETRY_AFTER_SECONDS = 30.0

    def __init__(self, company: CompanyConfig, client: httpx.AsyncClient):
        super().__init__(company, client)
        self._next_request_at = 0.0

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.RATE_LIMIT_RETRIES + 1):
            loop = asyncio.get_running_loop()
            delay = self._next_request_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = loop.time() + self.REQUEST_SPACING_SECONDS
            response = await self.client.request(method, url, **kwargs)
            challenge = response.headers.get("cf-mitigated", "").lower() == "challenge"
            if challenge or response.status_code == 429:
                kind = "cloudflare_challenge" if challenge else "rate_limited"
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                blocked = WorkdayBlocked(response.status_code, method, kind, retry_after)
                logger.warning(
                    "Workday blocked %s for %s: kind=%s status=%d retry_after=%s content_type=%s",
                    method,
                    self.company.slug,
                    kind,
                    response.status_code,
                    retry_after,
                    response.headers.get("content-type", ""),
                )
                if (
                    challenge
                    or attempt >= self.RATE_LIMIT_RETRIES
                    or (retry_after is not None and retry_after > self.MAX_RETRY_AFTER_SECONDS)
                ):
                    raise blocked
                await asyncio.sleep(retry_after if retry_after is not None else 5.0)
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable Workday retry state")

    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        endpoint = cfg["endpoint"]
        site = cfg["site"]
        limit = int(cfg.get("limit", 20))
        jobs: list[RawJob] = []
        seen: set[str] = set()
        detail_blocked = False
        search_texts = cfg.get("search_texts") or [""]
        for search_text in search_texts:
            offset = 0
            while True:
                response = await self._request(
                    "POST",
                    endpoint,
                    json={
                        "appliedFacets": {},
                        "limit": limit,
                        "offset": offset,
                        "searchText": search_text,
                    },
                )
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("jobPostings"), list
                ):
                    raise SourceError(
                        f"Workday response for {self.company.slug} has no valid jobPostings list"
                    )
                postings = payload["jobPostings"]
                for item in postings:
                    if not isinstance(item, dict):
                        _warn_skipped_item("Workday", self.company.slug, "not an object", item)
                        continue
                    external_path = item.get("externalPath", "")
                    title = item.get("title")
                    if not _usable_text(title):
                        _warn_skipped_item("Workday", self.company.slug, "missing title", item)
                        continue
                    if not _usable_text(external_path):
                        _warn_skipped_item("Workday", self.company.slug, "missing URL", item)
                        continue
                    if external_path in seen:
                        continue
                    seen.add(external_path)
                    detail_url = cfg.get("detail_base_url", "").rstrip("/") + external_path
                    bullet_fields = item.get("bulletFields") or []
                    if isinstance(bullet_fields, list):
                        description = " ".join(str(value) for value in bullet_fields)
                    else:
                        description = str(bullet_fields)
                    if cfg.get("detail_api_base") and not detail_blocked:
                        try:
                            detail_response = await self._request(
                                "GET",
                                cfg["detail_api_base"].rstrip("/") + external_path,
                            )
                        except WorkdayBlocked as exc:
                            # Search listings are complete; retain the bullet-field
                            # summaries instead of dropping the whole company.
                            detail_blocked = True
                            logger.warning(
                                "Using Workday listing summaries for %s "
                                "after detail requests were blocked (%s)",
                                self.company.slug,
                                exc.kind,
                            )
                        else:
                            detail = detail_response.json().get("jobPostingInfo", {})
                            description = _html_text(detail.get("jobDescription") or description)
                    _append_raw_job(
                        jobs,
                        "Workday",
                        self.company.slug,
                        item,
                        source_company=self.company.slug,
                        external_job_id=external_path,
                        title=title,
                        location_raw=item.get("locationsText", ""),
                        description_raw=description,
                        posted_at=_parse_datetime(item.get("postedOn")),
                        url=detail_url or f"https://{site}{external_path}",
                        metadata={"workday": item},
                    )
                offset += len(postings)
                if not postings or offset >= int(payload.get("total", offset)):
                    break
        return jobs


class EightfoldSource(JobSource):
    """Incremental official Eightfold search. This is not a full vacancy snapshot."""

    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        endpoint = str(cfg["search_url"])
        base = str(self.company.careers_url).rstrip("/")
        domain = str(cfg["domain"])
        terms = cfg.get("search_texts") or [""]
        max_pages = min(max(int(cfg.get("max_pages", 2)), 1), 5)
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for term in terms[:5]:
            for page in range(max_pages):
                if jobs or page:
                    await asyncio.sleep(0.5)
                response = await self.client.get(
                    endpoint,
                    params={
                        "domain": domain,
                        "query": str(term),
                        "location": "",
                        "start": page * 10,
                        "sort_by": "timestamp",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
                    raise SourceError(
                        f"Eightfold response for {self.company.slug} has no positions list"
                    )
                positions = data["positions"]
                if not positions and page == 0 and int(data.get("count") or 0):
                    raise SourceError(
                        f"Eightfold returned no positions for {self.company.slug} despite count"
                    )
                for item in positions:
                    if not isinstance(item, dict):
                        continue
                    job_id = item.get("id")
                    title = item.get("name")
                    if not job_id or not _usable_text(title):
                        _warn_skipped_item("Eightfold", self.company.slug, "invalid id/title", item)
                        continue
                    stable_id = str(job_id)
                    if stable_id in seen:
                        continue
                    seen.add(stable_id)
                    locations = item.get("locations") or []
                    location = (
                        "; ".join(str(value) for value in locations)
                        if isinstance(locations, list)
                        else str(locations)
                    )
                    # Use the official careers route and stable Eightfold position id.
                    job_url = urljoin(base + "/", "/careers/job/" + stable_id)
                    _append_raw_job(
                        jobs,
                        "Eightfold",
                        self.company.slug,
                        item,
                        source_company=self.company.slug,
                        external_job_id="eightfold:" + stable_id,
                        title=title,
                        location_raw=location,
                        description_raw=str(item.get("department") or ""),
                        posted_at=_parse_datetime(item.get("postedTs")),
                        url=job_url,
                        metadata={"eightfold": item},
                    )
                if len(positions) < 10:
                    break
        return jobs


class CareerHtmlSource(JobSource):
    """Bounded public HTML listing; discoveries never imply a vacancy has closed."""

    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        listing_url = str(cfg["listing_url"])
        response = await self.client.get(listing_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select(".search-results-list__item")
        if not cards:
            raise SourceError(
                f"Career listing for {self.company.slug} has no recognizable job cards"
            )
        seen: set[str] = set()
        jobs: list[RawJob] = []
        expected_host = urlsplit(listing_url).hostname
        fetch_details = bool(cfg.get("fetch_details", False))
        for card in cards[:30]:
            link = card.select_one('a[href*="/job/"]')
            if link is None:
                continue
            title = link.get_text(" ", strip=True)
            job_url = urljoin(listing_url, link.get("href", ""))
            parts = urlsplit(job_url)
            if not title or parts.hostname != expected_host or "/job/" not in parts.path:
                continue
            job_id = parts.path.rstrip("/").split("/")[-1]
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            segments = list(card.stripped_strings)
            remaining = [value for value in segments if value != title]
            date_text = next(
                (value for value in remaining if _parse_career_date(value)),
                None,
            )
            location = next(
                (value for value in remaining if value != date_text),
                "",
            )
            description = " ".join(remaining)
            if fetch_details:
                await asyncio.sleep(0.5)
                try:
                    detail = await self.client.get(job_url)
                    detail.raise_for_status()
                    body = BeautifulSoup(detail.text, "html.parser")
                    section = body.select_one("[class*='job-description'], [id*='job-description']")
                    if section:
                        description = section.get_text(" ", strip=True)[:15000]
                except httpx.HTTPError:
                    logger.warning(
                        "Career detail unavailable for %s; retaining listing summary",
                        self.company.slug,
                    )
            _append_raw_job(
                jobs,
                "Career HTML",
                self.company.slug,
                {"title": title, "url": job_url},
                source_company=self.company.slug,
                external_job_id="careerhtml:" + job_id,
                title=title,
                location_raw=location,
                description_raw=description,
                posted_at=_parse_career_date(date_text),
                url=job_url,
                metadata={"career_html": {"listing_url": listing_url}},
            )
        if not jobs:
            raise SourceError(f"Career listing for {self.company.slug} yielded no valid jobs")
        return jobs


def _parse_career_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").replace(tzinfo=UTC)
    except ValueError:
        return None


class JsonLdSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        response = await self.client.get(str(self.company.careers_url))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        found: list[dict[str, Any]] = []
        for node in soup.select('script[type="application/ld+json"]'):
            try:
                value = json.loads(node.string or "null")
            except json.JSONDecodeError:
                continue
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                    found.append(candidate)
                elif isinstance(candidate, dict) and isinstance(candidate.get("@graph"), list):
                    found.extend(
                        x
                        for x in candidate["@graph"]
                        if isinstance(x, dict) and x.get("@type") == "JobPosting"
                    )
        jobs = []
        for item in found:
            title = item.get("title")
            if not _usable_text(title):
                _warn_skipped_item("JSON-LD", self.company.slug, "missing title", item)
                continue
            location = item.get("jobLocation") or item.get("applicantLocationRequirements") or ""
            if isinstance(location, (dict, list)):
                location = json.dumps(location, ensure_ascii=False)
            identifier = item.get("identifier")
            identifier_value = identifier.get("value") if isinstance(identifier, dict) else None
            _append_raw_job(
                jobs,
                "JSON-LD",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=str(identifier_value or item.get("url", "")),
                title=title,
                location_raw=str(location),
                description_raw=_html_text(item.get("description")),
                posted_at=_parse_datetime(item.get("datePosted")),
                url=item.get("url") or str(self.company.careers_url),
                metadata={"jsonld": item},
            )
        return jobs


SOURCE_CLASSES: dict[AtsType, type[JobSource]] = {
    AtsType.GREENHOUSE: GreenhouseSource,
    AtsType.LEVER: LeverSource,
    AtsType.ASHBY: AshbySource,
    AtsType.SMARTRECRUITERS: SmartRecruitersSource,
    AtsType.WORKDAY: WorkdaySource,
    AtsType.EIGHTFOLD: EightfoldSource,
    AtsType.CAREER_HTML: CareerHtmlSource,
    AtsType.JSONLD: JsonLdSource,
}


class SourceRunner:
    def __init__(self, client: httpx.AsyncClient, max_concurrency: int = 5):
        self.client = client
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.workday_lock = asyncio.Lock()
        self.domain_locks: dict[str, asyncio.Lock] = {}

    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        domain = httpx.URL(str(company.careers_url)).host or company.slug
        lock = self.domain_locks.setdefault(domain, asyncio.Lock())
        if company.ats_type == AtsType.WORKDAY:
            # Wait outside the global semaphore so blocked Workday tenants
            # do not starve Greenhouse, Lever, or other healthy ATS sources.
            async with self.workday_lock:
                async with self.semaphore, lock:
                    return await WorkdaySource(company, self.client).fetch()
        async with self.semaphore, lock:
            return await SOURCE_CLASSES[company.ats_type](company, self.client).fetch()
