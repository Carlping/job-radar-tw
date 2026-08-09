from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import Settings
from .models import CompanyConfig, RawJob
from .sources import SourceRunner


@dataclass(frozen=True)
class SourceVerification:
    slug: str
    name: str
    ats_type: str
    enabled: bool
    source_verified: bool
    ok: bool
    jobs_count: int
    ready_to_enable: bool
    duration_seconds: float
    sample_jobs: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "ats_type": self.ats_type,
            "enabled": self.enabled,
            "source_verified": self.source_verified,
            "ok": self.ok,
            "jobs_count": self.jobs_count,
            "ready_to_enable": self.ready_to_enable,
            "duration_seconds": round(self.duration_seconds, 2),
            "sample_jobs": self.sample_jobs,
            "error": self.error,
        }


def filter_companies(
    companies: list[CompanyConfig],
    *,
    status: str = "disabled",
    selected_slugs: set[str] | None = None,
) -> list[CompanyConfig]:
    if status not in {"disabled", "enabled", "all"}:
        raise ValueError("status must be one of: disabled, enabled, all")
    selected = selected_slugs or set()
    result = []
    for company in companies:
        if selected and company.slug not in selected:
            continue
        if status == "disabled" and company.enabled:
            continue
        if status == "enabled" and not company.enabled:
            continue
        result.append(company)
    return result


def company_inventory(companies: list[CompanyConfig]) -> list[dict[str, Any]]:
    return [
        {
            "slug": company.slug,
            "name": company.name,
            "enabled": company.enabled,
            "source_verified": company.source_verified,
            "ats_type": company.ats_type.value,
            "careers_url": str(company.careers_url),
            "priority": company.priority,
            "profiles": [str(profile) for profile in company.profiles],
        }
        for company in companies
    ]


def _sample_jobs(jobs: list[RawJob], limit: int = 3) -> list[dict[str, str]]:
    return [
        {
            "title": job.title,
            "location": job.location_raw,
            "url": str(job.url),
        }
        for job in jobs[:limit]
    ]


async def verify_companies(
    companies: list[CompanyConfig],
    settings: Settings,
    *,
    min_jobs: int = 1,
) -> list[SourceVerification]:
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    results: list[SourceVerification] = []
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "JobRadarTW/0.1 (+source onboarding)"},
    ) as client:
        runner = SourceRunner(client, max_concurrency=1)
        for company in companies:
            started = time.monotonic()
            try:
                jobs = await runner.fetch(company)
                duration = time.monotonic() - started
                results.append(
                    SourceVerification(
                        slug=company.slug,
                        name=company.name,
                        ats_type=company.ats_type.value,
                        enabled=company.enabled,
                        source_verified=company.source_verified,
                        ok=True,
                        jobs_count=len(jobs),
                        ready_to_enable=not company.enabled and len(jobs) >= min_jobs,
                        duration_seconds=duration,
                        sample_jobs=_sample_jobs(jobs),
                    )
                )
            except Exception as exc:
                duration = time.monotonic() - started
                results.append(
                    SourceVerification(
                        slug=company.slug,
                        name=company.name,
                        ats_type=company.ats_type.value,
                        enabled=company.enabled,
                        source_verified=company.source_verified,
                        ok=False,
                        jobs_count=0,
                        ready_to_enable=False,
                        duration_seconds=duration,
                        error=str(exc)[:500],
                    )
                )
    return results


def render_inventory(items: list[dict[str, Any]]) -> str:
    lines = ["slug | status | verified | ats | priority | name"]
    lines.append("-" * 72)
    for item in items:
        status = "enabled" if item["enabled"] else "disabled"
        verified = "yes" if item["source_verified"] else "no"
        lines.append(
            f"{item['slug']} | {status} | {verified} | {item['ats_type']} | "
            f"{item['priority']} | {item['name']}"
        )
    return "\n".join(lines)


def render_verifications(results: list[SourceVerification]) -> str:
    lines = ["slug | result | jobs | promote | name"]
    lines.append("-" * 72)
    for result in results:
        status = "ok" if result.ok else "error"
        promote = "yes" if result.ready_to_enable else "no"
        lines.append(f"{result.slug} | {status} | {result.jobs_count} | {promote} | {result.name}")
        if result.error:
            lines.append(f"  error: {result.error}")
        elif result.sample_jobs:
            sample = "; ".join(item["title"] for item in result.sample_jobs)
            lines.append(f"  sample: {sample}")
    return "\n".join(lines)


def promote_companies_in_config(config_path: Path, slugs: list[str]) -> list[str]:
    text = config_path.read_text(encoding="utf-8")
    promoted: list[str] = []
    for slug in slugs:
        pattern = re.compile(
            rf"(?m)^(\s*-\s*\{{[^\n]*slug:\s*{re.escape(slug)}(?=,|\s|\}})[^\n]*\}})\s*$"
        )
        match = pattern.search(text)
        if not match:
            raise ValueError(f"Could not find one-line company entry for slug: {slug}")
        line = match.group(1)
        updated = _set_inline_yaml_bool(line, "enabled", True)
        updated = _set_inline_yaml_bool(updated, "source_verified", True)
        if updated != line:
            text = text[: match.start(1)] + updated + text[match.end(1) :]
            promoted.append(slug)
    config_path.write_text(text, encoding="utf-8")
    return promoted


def _set_inline_yaml_bool(line: str, key: str, value: bool) -> str:
    text_value = "true" if value else "false"
    if re.search(rf"\b{re.escape(key)}:\s*(true|false)\b", line):
        return re.sub(
            rf"\b{re.escape(key)}:\s*(true|false)\b", f"{key}: {text_value}", line, count=1
        )
    return line[:-1] + f", {key}: {text_value}" + line[-1:]


def load_source_candidates(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return list(payload.get("candidates", []))


def render_candidates(candidates: list[dict[str, Any]]) -> str:
    lines = ["category | priority | name | careers_url"]
    lines.append("-" * 88)
    for item in candidates:
        lines.append(
            f"{item.get('category', '')} | {item.get('priority', '')} | "
            f"{item.get('name', '')} | {item.get('careers_url', '')}"
        )
    return "\n".join(lines)


def json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
