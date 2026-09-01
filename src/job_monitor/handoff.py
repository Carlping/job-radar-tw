"""Export the current job queue as a handoff document for an external agent."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

HANDOFF_SCHEMA_VERSION = 1
BUCKET_LABELS = {"target": "🎯 對位", "stretch": "🪜 延伸挑戰"}
COVERAGE_KEYS = ("sources_attempted", "sources_succeeded", "jobs_fetched")


def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _job_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "title": job["title"],
        "company": job["company"],
        "company_slug": job["company_slug"],
        "industry": job["industry"],
        "location": job["location"],
        "url": job["url"],
        "profile": job["profile"],
        "score": round(float(job["score"]), 4),
        "tier": job["tier"],
        "bucket": job["bucket"],
        "fit": job.get("fit"),
        "reach": job.get("reach"),
        "level": job.get("level"),
        "required_years_min": job.get("required_years_min"),
        "reasons": list(job.get("reasons") or []),
        "gaps": list(job.get("gaps") or []),
        "first_seen_at": _iso(job.get("first_seen_at")),
        "source_posted_at": _iso(job.get("source_posted_at")),
        "content_hash": job["content_hash"],
    }


def build_handoff(
    jobs: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any] | None,
    *,
    window_days: int,
    buckets: Sequence[str],
) -> dict[str, Any]:
    """Build the handoff payload.

    The payload is a pure function of the database state: every timestamp comes from the
    exported rows or from the monitor run that produced them, never from the current time,
    so re-exporting unchanged data yields an identical `content_hash`.
    """
    stats = run.get("stats") if run else None
    coverage = stats if isinstance(stats, Mapping) else {}
    payload: dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "window_days": window_days,
        "buckets": list(buckets),
        "source_run": {
            "run_key": run["run_key"] if run else None,
            "status": run["status"] if run else None,
            "finished_at": _iso(run.get("finished_at")) if run else None,
            "coverage": {key: coverage.get(key) for key in COVERAGE_KEYS},
        },
        "counts": {
            "total": len(jobs),
            **{bucket: sum(1 for job in jobs if job["bucket"] == bucket) for bucket in buckets},
        },
        "jobs": [_job_payload(job) for job in jobs],
    }
    payload["content_hash"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def render_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_markdown(payload: Mapping[str, Any]) -> str:
    run = payload["source_run"]
    coverage = run["coverage"]
    counts = payload["counts"]
    lines = [
        "# Job Radar handoff",
        "",
        f"- schema_version: {payload['schema_version']}",
        f"- content_hash: `{payload['content_hash']}`",
        f"- source_run: `{run['run_key']}` ({run['status']}) finished {run['finished_at']}",
        (
            f"- source coverage: {coverage['sources_succeeded']}/{coverage['sources_attempted']}"
            f" sources, {coverage['jobs_fetched']} postings fetched"
        ),
        f"- window: last {payload['window_days']} days, {counts['total']} open candidates",
        "",
        "職缺依分數排序。分數是本 repo 的 matching pipeline 對「職缺當前版本」的評分，",
        "`gaps` 是尚未滿足的硬性條件，請在深入研究前先確認這些條件。",
        "",
    ]
    if not payload["jobs"]:
        lines.append("_No open candidates in this window._")
        return "\n".join(lines) + "\n"

    for index, job in enumerate(payload["jobs"], start=1):
        label = BUCKET_LABELS.get(job["bucket"], job["bucket"])
        lines.append(f"## {index}. {job['title']} — {job['company']}")
        lines.append("")
        lines.append(f"- {label} | score {job['score']:.2f} | tier {job['tier']}")
        lines.append(f"- profile: {job['profile']} | industry: {job['industry']}")
        lines.append(f"- location: {job['location']}")
        lines.append(f"- url: {job['url']}")
        lines.append(f"- first_seen_at: {job['first_seen_at']} | posted: {job['source_posted_at']}")
        if job["level"] or job["required_years_min"] is not None:
            lines.append(
                f"- level: {job['level']} | required_years_min: {job['required_years_min']}"
                f" | fit: {job['fit']} | reach: {job['reach']}"
            )
        if job["reasons"]:
            lines.append(f"- matched: {', '.join(job['reasons'])}")
        if job["gaps"]:
            lines.append(f"- gaps: {', '.join(job['gaps'])}")
        lines.append(f"- job_id: `{job['job_id']}`")
        lines.append("")
    return "\n".join(lines) + "\n"
