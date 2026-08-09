from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import AtsType, CompanyConfig, RawJob


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


class SourceError(RuntimeError):
    pass


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
        return [
            RawJob(
                source_company=self.company.slug,
                external_job_id=str(item["id"]),
                title=item["title"],
                location_raw=(item.get("location") or {}).get("name", ""),
                description_raw=_html_text(item.get("content")),
                posted_at=_parse_datetime(item.get("updated_at")),
                url=item["absolute_url"],
                metadata={"departments": item.get("departments", [])},
            )
            for item in payload.get("jobs", [])
        ]


class LeverSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        site = self.company.ats_config["site"]
        payload = await self.get_json(f"https://api.lever.co/v0/postings/{site}?mode=json")
        return [
            RawJob(
                source_company=self.company.slug,
                external_job_id=str(item["id"]),
                title=item["text"],
                location_raw=(item.get("categories") or {}).get("location", ""),
                description_raw=_html_text(item.get("descriptionPlain") or item.get("description")),
                posted_at=_parse_datetime(item.get("createdAt")),
                url=item.get("hostedUrl") or item["applyUrl"],
                metadata={"categories": item.get("categories", {})},
            )
            for item in payload
        ]


class AshbySource(JobSource):
    async def fetch(self) -> list[RawJob]:
        board = self.company.ats_config["board_name"]
        payload = await self.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
        return [
            RawJob(
                source_company=self.company.slug,
                external_job_id=str(item.get("id") or item.get("jobUrl", "")),
                title=item["title"],
                location_raw=item.get("location", ""),
                description_raw=_html_text(
                    item.get("descriptionHtml") or item.get("descriptionPlain")
                ),
                posted_at=_parse_datetime(item.get("publishedAt")),
                url=item.get("jobUrl") or item["applyUrl"],
                metadata={"department": item.get("department")},
            )
            for item in payload.get("jobs", [])
        ]


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
                detail = await self.get_json(f"{base}/{item['id']}")
                location = item.get("location") or {}
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                description = " ".join(
                    _html_text(section.get("text"))
                    for section in sections.values()
                    if isinstance(section, dict)
                )
                jobs.append(
                    RawJob(
                        source_company=self.company.slug,
                        external_job_id=str(item["id"]),
                        title=item["name"],
                        location_raw=", ".join(
                            str(location.get(key, ""))
                            for key in ("city", "region", "country")
                            if location.get(key)
                        ),
                        description_raw=description,
                        posted_at=_parse_datetime(item.get("releasedDate")),
                        url=item.get("ref")
                        or f"https://jobs.smartrecruiters.com/{identifier}/{item['id']}",
                    )
                )
            offset += len(content)
            if not content or offset >= int(payload.get("totalFound", offset)):
                break
        return jobs


class WorkdaySource(JobSource):
    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        endpoint = cfg["endpoint"]
        site = cfg["site"]
        limit = int(cfg.get("limit", 20))
        jobs: list[RawJob] = []
        seen: set[str] = set()
        search_texts = cfg.get("search_texts") or [""]
        for search_text in search_texts:
            offset = 0
            while True:
                response = await self.client.post(
                    endpoint,
                    json={
                        "appliedFacets": {},
                        "limit": limit,
                        "offset": offset,
                        "searchText": search_text,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                postings = payload.get("jobPostings", [])
                for item in postings:
                    external_path = item.get("externalPath", "")
                    if external_path in seen:
                        continue
                    seen.add(external_path)
                    detail_url = cfg.get("detail_base_url", "").rstrip("/") + external_path
                    description = " ".join(item.get("bulletFields", []))
                    if cfg.get("detail_api_base"):
                        detail_response = await self.client.get(
                            cfg["detail_api_base"].rstrip("/") + external_path
                        )
                        detail_response.raise_for_status()
                        detail = detail_response.json().get("jobPostingInfo", {})
                        description = _html_text(detail.get("jobDescription") or description)
                    jobs.append(
                        RawJob(
                            source_company=self.company.slug,
                            external_job_id=external_path,
                            title=item["title"],
                            location_raw=item.get("locationsText", ""),
                            description_raw=description,
                            posted_at=_parse_datetime(item.get("postedOn")),
                            url=detail_url or f"https://{site}{external_path}",
                            metadata={"workday": item},
                        )
                    )
                offset += len(postings)
                if not postings or offset >= int(payload.get("total", offset)):
                    break
        return jobs


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
                    found.extend(x for x in candidate["@graph"] if x.get("@type") == "JobPosting")
        jobs = []
        for item in found:
            location = item.get("jobLocation") or item.get("applicantLocationRequirements") or ""
            if isinstance(location, (dict, list)):
                location = json.dumps(location, ensure_ascii=False)
            jobs.append(
                RawJob(
                    source_company=self.company.slug,
                    external_job_id=str(
                        item.get("identifier", {}).get("value") or item.get("url", "")
                    ),
                    title=item["title"],
                    location_raw=str(location),
                    description_raw=_html_text(item.get("description")),
                    posted_at=_parse_datetime(item.get("datePosted")),
                    url=item.get("url") or str(self.company.careers_url),
                    metadata={"jsonld": item},
                )
            )
        return jobs


SOURCE_CLASSES: dict[AtsType, type[JobSource]] = {
    AtsType.GREENHOUSE: GreenhouseSource,
    AtsType.LEVER: LeverSource,
    AtsType.ASHBY: AshbySource,
    AtsType.SMARTRECRUITERS: SmartRecruitersSource,
    AtsType.WORKDAY: WorkdaySource,
    AtsType.JSONLD: JsonLdSource,
}


class SourceRunner:
    def __init__(self, client: httpx.AsyncClient, max_concurrency: int = 5):
        self.client = client
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.domain_locks: dict[str, asyncio.Lock] = {}

    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        domain = httpx.URL(str(company.careers_url)).host or company.slug
        lock = self.domain_locks.setdefault(domain, asyncio.Lock())
        async with self.semaphore, lock:
            source = SOURCE_CLASSES[company.ats_type](company, self.client)
            return await source.fetch()
