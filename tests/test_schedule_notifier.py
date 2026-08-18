from datetime import UTC, datetime

import httpx
import pytest

from job_monitor.config import Settings
from job_monitor.models import MatchedJob, MatchResult, ParsedJob, ProfileName, RawJob
from job_monitor.notifier import (
    TelegramNotifier,
    render_job_message,
    render_run_summary,
    source_age_days,
    split_message,
)
from job_monitor.pipeline import _qualifies_for_immediate_notification
from job_monitor.schedule import is_scheduled_window, local_run_key, scheduled_run_key


def test_et_schedule_handles_dst():
    assert is_scheduled_window(datetime(2026, 6, 18, 0, 0, tzinfo=UTC))
    assert is_scheduled_window(datetime(2026, 1, 18, 1, 0, tzinfo=UTC))
    assert is_scheduled_window(datetime(2026, 6, 18, 6, 0, tzinfo=UTC))


def test_run_key_uses_et_date():
    assert local_run_key(datetime(2026, 6, 18, 0, 0, tzinfo=UTC)) == "daily-2026-06-17"


def test_scheduled_run_key_handles_delayed_github_delivery():
    assert scheduled_run_key(datetime(2026, 6, 18, 0, 17, tzinfo=UTC)) == "daily-2026-06-17"
    assert scheduled_run_key(datetime(2026, 6, 18, 6, 6, tzinfo=UTC)) == "daily-2026-06-17"
    assert scheduled_run_key(datetime(2026, 6, 18, 23, 30, tzinfo=UTC)) is None
    assert scheduled_run_key(datetime(2026, 6, 19, 0, 30, tzinfo=UTC)) == "daily-2026-06-18"


def test_scheduled_run_key_skips_before_et_window():
    assert scheduled_run_key(datetime(2026, 1, 18, 0, 17, tzinfo=UTC)) is None
    assert scheduled_run_key(datetime(2026, 1, 18, 1, 17, tzinfo=UTC)) == "daily-2026-01-17"


def test_schedule_accepts_custom_timezone_hour_and_grace_period():
    within_window = datetime(2026, 6, 18, 1, 30, tzinfo=UTC)
    outside_window = datetime(2026, 6, 18, 3, 0, tzinfo=UTC)

    assert local_run_key(within_window, timezone="Asia/Taipei") == "daily-2026-06-18"
    assert (
        scheduled_run_key(
            within_window,
            timezone="Asia/Taipei",
            hour=9,
            grace_hours=2,
        )
        == "daily-2026-06-18"
    )
    assert not is_scheduled_window(
        outside_window,
        timezone="Asia/Taipei",
        hour=9,
        grace_hours=2,
    )


def test_message_split_respects_limit():
    chunks = split_message("line\n" * 100, limit=50)
    assert len(chunks) > 1
    assert all(len(chunk) <= 50 for chunk in chunks)


def test_run_summary_lists_matches_and_source_warnings():
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 25, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    job = ParsedJob(raw=raw)
    result = MatchResult(profile=ProfileName.TECH, score=0.82, eligible=True, tier="strong")
    matched = MatchedJob(
        company_name="Acme",
        job=job,
        result=result,
        first_seen_at=datetime(2026, 6, 26, tzinfo=UTC),
        is_new=True,
        changed=True,
    )

    summary = render_run_summary(
        run_key="daily-2026-06-26",
        stats={
            "sources_attempted": 2,
            "sources_succeeded": 1,
            "jobs_fetched": 10,
            "matches": 1,
            "immediate_candidates": 1,
        },
        errors=[{"company": "broken-source", "error": "timeout"}],
        matched_jobs=[matched],
        zero_job_sources=["Empty Verified Source"],
    )

    assert "Data Analyst" in summary
    assert "https://example.com/jobs/1" in summary
    assert "broken-source" in summary
    assert "Empty Verified Source" in summary
    assert "source 1d old" in summary
    assert "Job Radar TW" in summary
    assert "職缺雷達" in summary
    assert "逐筆候選" in summary


def test_job_message_includes_freshness():
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 25, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    job = ParsedJob(raw=raw)
    result = MatchResult(profile=ProfileName.TECH, score=0.84, eligible=True, tier="strong")

    message = render_job_message("Acme", job, result, datetime(2026, 6, 26, tzinfo=UTC))

    assert "新鮮度" in message
    assert "2026-06-25" in message
    assert source_age_days(raw.posted_at, datetime(2026, 6, 26, tzinfo=UTC)) == 1


def test_immediate_notification_gate_requires_fresh_new_strong_match():
    settings = Settings(
        immediate_notification_min_score=0.82,
        immediate_notification_max_source_age_days=14,
    )
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Senior Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 20, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    job = ParsedJob(raw=raw)
    result = MatchResult(profile=ProfileName.TECH, score=0.84, eligible=True, tier="strong")

    assert _qualifies_for_immediate_notification(
        job,
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=True,
    )
    assert not _qualifies_for_immediate_notification(
        job,
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=False,
    )
    assert not _qualifies_for_immediate_notification(
        job,
        result,
        datetime(2026, 7, 20, tzinfo=UTC),
        settings,
        is_new=True,
    )


def test_backfill_gate_allows_old_existing_strong_match_above_threshold():
    settings = Settings(
        immediate_notification_min_score=0.82,
        immediate_notification_max_source_age_days=14,
    )
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=datetime(2026, 6, 20, tzinfo=UTC),
        url="https://example.com/jobs/1",
    )
    result = MatchResult(profile="custom", score=0.9, eligible=True, tier="strong")

    assert _qualifies_for_immediate_notification(
        ParsedJob(raw=raw),
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=False,
        backfill=True,
    )


@pytest.mark.parametrize(
    ("result", "posted_at", "backfill"),
    [
        (
            MatchResult(profile="custom", score=0.81, eligible=True, tier="strong"),
            datetime(2026, 6, 20, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(profile="custom", score=0.9, eligible=True, tier="match"),
            datetime(2026, 6, 20, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(
                profile="custom",
                score=0.95,
                eligible=True,
                tier="strong",
                bucket="stretch",
            ),
            datetime(2026, 6, 20, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(profile="custom", score=0.9, eligible=True, tier="strong"),
            datetime(2025, 1, 1, tzinfo=UTC),
            True,
        ),
        (
            MatchResult(profile="custom", score=0.9, eligible=True, tier="strong"),
            datetime(2026, 6, 20, tzinfo=UTC),
            False,
        ),
    ],
)
def test_backfill_gate_keeps_score_tier_age_and_newness_gates(
    result,
    posted_at,
    backfill,
):
    settings = Settings(
        immediate_notification_min_score=0.82,
        immediate_notification_max_source_age_days=14,
    )
    raw = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Austin, TX",
        description_raw="SQL analytics",
        posted_at=posted_at,
        url="https://example.com/jobs/1",
    )

    assert not _qualifies_for_immediate_notification(
        ParsedJob(raw=raw),
        result,
        datetime(2026, 6, 26, tzinfo=UTC),
        settings,
        is_new=False,
        backfill=backfill,
    )


@pytest.mark.asyncio
async def test_telegram_http_error_does_not_expose_bot_token():
    token = "123456:super-secret-token"
    requests = 0

    def telegram_error(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, request=request, json={"ok": False})

    transport = httpx.MockTransport(telegram_error)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = TelegramNotifier(token, "chat-id", client)

        with pytest.raises(RuntimeError) as error:
            await notifier.send("hello")

    assert requests == 3
    assert token not in str(error.value)
