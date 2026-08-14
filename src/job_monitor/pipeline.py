from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from .config import CandidateProfile, ProfileConfig, SearchPreferences, Settings
from .llm import LLMEnricher
from .matching import match_job, parse_job
from .models import CompanyConfig, MatchedJob, MatchResult, ParsedJob, RawJob
from .notifier import (
    TelegramNotifier,
    render_failure_alert,
    render_job_message,
    render_run_summary,
    source_age_days,
)
from .resume import load_resume
from .schedule import local_run_key
from .sources import SourceRunner
from .storage import JobPlan, MatchDecision, Storage

logger = logging.getLogger(__name__)


@dataclass
class RunReport:
    run_key: str
    sources_attempted: int = 0
    sources_succeeded: int = 0
    jobs_fetched: int = 0
    jobs_new: int = 0
    jobs_changed: int = 0
    matches: int = 0
    notifications: int = 0
    immediate_candidates: int = 0
    notifications_suppressed: int = 0
    notifications_pending: int = 0
    jobs_closed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    matched_jobs: list[MatchedJob] = field(default_factory=list)
    dry_run_matches: list[MatchedJob] = field(default_factory=list)
    zero_job_sources: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    def stats(self) -> dict[str, int]:
        return {key: value for key, value in self.__dict__.items() if isinstance(value, int)}


@dataclass(frozen=True)
class CompanyRunContext:
    company: CompanyConfig
    company_id: str
    baseline: bool


@dataclass(frozen=True)
class SourceFetchResult:
    context: CompanyRunContext
    raw_jobs: list[RawJob]
    error: Exception | None = None


async def _try_send_notification(
    notifier: TelegramNotifier | None,
    text: str,
    report: RunReport,
    *,
    context: str,
) -> bool:
    if notifier is None:
        return False
    try:
        await notifier.send(text)
        return True
    except Exception as exc:
        logger.exception("Telegram notification failed during %s", context)
        report.errors.append({"company": "telegram", "error": f"{context}: {str(exc)[:450]}"})
        return False


async def _fetch_company_source(
    runner: SourceRunner,
    context: CompanyRunContext,
) -> SourceFetchResult:
    try:
        return SourceFetchResult(context=context, raw_jobs=await runner.fetch(context.company))
    except Exception as exc:
        return SourceFetchResult(context=context, raw_jobs=[], error=exc)


def _qualifies_for_immediate_notification(
    parsed: ParsedJob,
    result: MatchResult,
    first_seen_at: datetime,
    settings: Settings,
    *,
    is_new: bool,
    backfill: bool = False,
) -> bool:
    if result.bucket != "target":
        return False
    if backfill:
        return True
    if not is_new:
        return False
    if result.tier != "strong" or result.score < settings.immediate_notification_min_score:
        return False
    age = source_age_days(parsed.raw.posted_at, first_seen_at)
    return age is None or age <= settings.immediate_notification_max_source_age_days


def _safe_error(exc: BaseException, settings: Settings) -> str:
    message = str(exc) or type(exc).__name__
    secret_values = [
        settings.database_url,
        settings.telegram_bot_token,
        settings.openai_api_key,
        settings.resume_text.get_secret_value() if settings.resume_text else None,
    ]
    for value in secret_values:
        if value:
            message = message.replace(value, "[redacted]")
    return message[:500]


async def run_pipeline(
    settings: Settings,
    companies: list[CompanyConfig],
    profiles: dict,
    preferences: SearchPreferences,
    candidate: CandidateProfile | None = None,
    *,
    dry_run: bool = False,
    backfill: bool = False,
    run_key: str | None = None,
) -> RunReport:
    key = run_key or local_run_key(timezone=settings.monitor_timezone)
    report = RunReport(run_key=key)
    if not dry_run and not settings.database_url:
        raise ValueError("DATABASE_URL is required outside dry-run")
    resume = load_resume(
        settings.resume_path,
        settings.resume_text.get_secret_value() if settings.resume_text else None,
    )
    storage = None if dry_run else Storage(settings.database_url or "", create_schema=False)
    run_id = "dry-run"
    if storage:
        claim = storage.claim_run(key)
        if claim is None:
            report.skipped_reason = "duplicate_run_key"
            logger.warning("Run %s already completed or is in progress; skipping fetch", key)
            return report
        run_id = claim.run_id

    try:
        timeout = httpx.Timeout(settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "JobRadarTW/0.1"},
        ) as client:
            runner = SourceRunner(client, settings.max_concurrency)
            notifier = None
            if settings.telegram_bot_token and settings.telegram_chat_id and not dry_run:
                notifier = TelegramNotifier(
                    settings.telegram_bot_token, settings.telegram_chat_id, client
                )
            enricher = None
            if settings.llm_enabled:
                enricher = LLMEnricher(settings.openai_api_key or "", settings.openai_model or "")
            source_contexts: list[CompanyRunContext] = []
            for company in companies:
                if not company.enabled:
                    continue
                report.sources_attempted += 1
                company_id = storage.sync_company(company) if storage else company.slug
                baseline = storage is not None and not storage.is_baseline_completed(company_id)
                source_contexts.append(CompanyRunContext(company, company_id, baseline))

            source_results = await asyncio.gather(
                *(_fetch_company_source(runner, context) for context in source_contexts)
            )

            for source_result in source_results:
                if storage:
                    storage.assert_active_run(run_id)
                company = source_result.context.company
                company_id = source_result.context.company_id
                baseline = source_result.context.baseline
                if source_result.error is not None:
                    logger.exception(
                        "Source failed: %s", company.slug, exc_info=source_result.error
                    )
                    report.errors.append(
                        {"company": company.slug, "error": str(source_result.error)[:500]}
                    )
                    failures = storage.source_failed(company_id, run_id) if storage else 1
                    if failures >= 3:
                        await _try_send_notification(
                            notifier,
                            render_failure_alert(company.name, failures, str(source_result.error)),
                            report,
                            context=f"source failure alert for {company.slug}",
                        )
                    continue

                raw_jobs = source_result.raw_jobs
                report.jobs_fetched += len(raw_jobs)
                if company.source_verified and not raw_jobs:
                    report.zero_job_sources.append(company.name)
                for raw in raw_jobs:
                    parsed = parse_job(raw)
                    observed_at = datetime.now(UTC)
                    plan = (
                        storage.plan_job(company_id, raw)
                        if storage
                        else JobPlan(
                            job_id=raw.stable_external_id,
                            is_new=True,
                            changed=True,
                            first_seen_at=observed_at,
                            previous_content_hash=None,
                        )
                    )
                    if not plan.changed and not backfill:
                        if storage:
                            storage.persist_job_decisions(
                                company_id,
                                run_id,
                                raw,
                                plan,
                                [],
                            )
                        continue

                    decisions: list[MatchDecision] = []
                    eligible_matches: list[MatchedJob] = []
                    for profile_name in company.profiles:
                        profile: ProfileConfig = profiles[profile_name]
                        result = match_job(
                            parsed,
                            profile,
                            preferences,
                            resume,
                            visa_sponsorship_required=settings.visa_sponsorship_required,
                            company_visa_support=company.visa_support,
                            candidate=candidate,
                            company_ndx_member=company.ndx_member,
                        )
                        if (
                            0.45 <= result.score < profile.strong_threshold
                            and parsed.ambiguities
                            and enricher
                        ):
                            try:
                                parsed = await enricher.enrich(parsed)
                                result = match_job(
                                    parsed,
                                    profile,
                                    preferences,
                                    resume,
                                    visa_sponsorship_required=settings.visa_sponsorship_required,
                                    company_visa_support=company.visa_support,
                                    candidate=candidate,
                                    company_ndx_member=company.ndx_member,
                                )
                                result.used_llm = True
                            except Exception as exc:
                                logger.warning("LLM fallback for %s: %s", raw.title, exc)
                        matched = None
                        notification_message = None
                        if result.eligible:
                            matched = MatchedJob(
                                company_name=company.name,
                                job=parsed,
                                result=result,
                                first_seen_at=plan.first_seen_at,
                                is_new=plan.is_new,
                                changed=plan.changed,
                            )
                            eligible_matches.append(matched)
                            should_notify = (
                                storage is not None
                                and notifier is not None
                                and (not baseline or backfill)
                                and _qualifies_for_immediate_notification(
                                    parsed,
                                    result,
                                    plan.first_seen_at,
                                    settings,
                                    is_new=plan.is_new,
                                    backfill=backfill,
                                )
                            )
                            if should_notify:
                                notification_message = render_job_message(
                                    matched.company_name,
                                    matched.job,
                                    matched.result,
                                    matched.first_seen_at,
                                )
                        decisions.append(
                            MatchDecision(
                                profile_version=profile.version,
                                result=result,
                                notification_message=notification_message,
                            )
                        )

                    if storage:
                        persisted = storage.persist_job_decisions(
                            company_id,
                            run_id,
                            raw,
                            plan,
                            decisions,
                        )
                        report.immediate_candidates += persisted.notifications_enqueued
                    report.jobs_new += int(plan.is_new)
                    report.jobs_changed += int(plan.changed and not plan.is_new)
                    report.matches += len(eligible_matches)
                    report.matched_jobs.extend(eligible_matches)
                    if dry_run:
                        report.dry_run_matches.extend(eligible_matches)
                if storage:
                    report.jobs_closed += storage.mark_missing(company_id, run_id)
                    storage.source_succeeded(company_id, run_id)
                report.sources_succeeded += 1

            if notifier and storage:
                pending_before_delivery = storage.pending_notification_count()
                queued = storage.claim_pending_notifications(
                    run_id, settings.immediate_notification_max_per_run
                )
                report.notifications_suppressed = max(
                    0,
                    pending_before_delivery - len(queued),
                )
                for item in queued:
                    sent = await _try_send_notification(
                        notifier,
                        item["message"],
                        report,
                        context=f"job alert for {item['job_id']}",
                    )
                    if not sent:
                        storage.release_notification_claim(
                            run_id,
                            item["id"],
                            item["claim_token"],
                            report.errors[-1]["error"],
                        )
                        continue
                    if storage.mark_notification_sent(
                        run_id,
                        item["id"],
                        item["claim_token"],
                    ):
                        report.notifications += 1
                report.notifications_pending = storage.pending_notification_count()

            if notifier and report.sources_attempted and not report.sources_succeeded:
                await _try_send_notification(
                    notifier,
                    "🚨 職缺雷達本次執行全部來源失敗\n"
                    + "\n".join(
                        f"{item['company']}: {item['error'][:200]}" for item in report.errors
                    ),
                    report,
                    context="all sources failed alert",
                )
            if notifier:
                await _try_send_notification(
                    notifier,
                    render_run_summary(
                        run_key=report.run_key,
                        stats=report.stats(),
                        errors=report.errors,
                        matched_jobs=report.matched_jobs,
                        zero_job_sources=report.zero_job_sources,
                        max_matches=settings.daily_summary_max_matches,
                    ),
                    report,
                    context="daily summary",
                )
    except BaseException as exc:
        report.errors.append({"company": "pipeline", "error": _safe_error(exc, settings)})
        raise
    finally:
        if storage:
            storage.finish_run(run_id, report.stats(), report.errors)
    return report
