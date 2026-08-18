from datetime import UTC, datetime

import pytest

from job_monitor import pipeline
from job_monitor.config import ProfileConfig, SearchPreferences, Settings
from job_monitor.models import CompanyConfig, RawJob
from job_monitor.storage import JobPersistResult, JobPlan, RunClaim, Storage


class FakeStorage:
    def __init__(self):
        self.notifications = []
        self.outbox = []
        self.matches = []
        self.finished = None
        self.baseline_completed = True

    def claim_run(self, run_key):
        return RunClaim("run-id", reclaimed=False)

    def assert_active_run(self, run_id):
        return None

    def sync_company(self, company):
        return "company-id"

    def is_baseline_completed(self, company_id):
        return self.baseline_completed

    def source_succeeded(self, company_id, run_id=None):
        self.baseline_completed = True
        return None

    def plan_job(self, company_id, raw):
        return JobPlan(
            job_id=raw.stable_external_id,
            is_new=False,
            changed=False,
            first_seen_at=datetime(2026, 6, 26, tzinfo=UTC),
            previous_content_hash=raw.content_hash,
        )

    def persist_job_decisions(self, company_id, run_id, raw, plan, decisions):
        enqueued = 0
        for decision in decisions:
            self.matches.append((plan.job_id, decision.result))
            if decision.notification_message is None or plan.job_id == "job-0":
                continue
            if any(item["job_id"] == plan.job_id for item in self.outbox):
                continue
            self.outbox.append(
                {
                    "id": f"outbox-{plan.job_id}",
                    "job_id": plan.job_id,
                    "profile": str(decision.result.profile),
                    "version_hash": raw.content_hash,
                    "score": decision.result.score,
                    "message": decision.notification_message,
                }
            )
            enqueued += 1
        return JobPersistResult(
            job_id=plan.job_id,
            is_new=plan.is_new,
            changed=plan.changed,
            first_seen_at=plan.first_seen_at,
            notifications_enqueued=enqueued,
        )

    def claim_pending_notifications(self, run_id, limit):
        claimed = sorted(self.outbox, key=lambda item: item["score"], reverse=True)[:limit]
        return [dict(item, claim_token=f"claim-{item['id']}") for item in claimed]

    def release_notification_claim(self, run_id, outbox_id, claim_token, error):
        return True

    def mark_notification_sent(self, run_id, outbox_id, claim_token):
        item = next(item for item in self.outbox if item["id"] == outbox_id)
        self.outbox.remove(item)
        self.notifications.append((item["job_id"], item["profile"], item["version_hash"]))
        return True

    def pending_notification_count(self):
        return len(self.outbox)

    def mark_missing(self, company_id, run_id):
        return 0

    def finish_run(self, run_id, stats, errors):
        self.finished = (run_id, stats, errors)


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_backfill_applies_notification_gates_before_cap(monkeypatch):
    raw_jobs = [
        RawJob(
            source_company="acme",
            external_job_id=f"job-{index}",
            title=f"Reporting Analyst {index}",
            location_raw="Austin, TX",
            description_raw="Operational reporting",
            posted_at=datetime(2025, 1, 1, tzinfo=UTC),
            url=f"https://example.com/jobs/{index}",
        )
        for index in range(4)
    ]
    storage = FakeStorage()
    notifier = FakeNotifier()

    class FakeSourceRunner:
        def __init__(self, client, max_concurrency):
            pass

        async def fetch(self, company):
            return raw_jobs

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: storage)
    monkeypatch.setattr(pipeline, "SourceRunner", FakeSourceRunner)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args, **kwargs: notifier)

    settings = Settings(
        database_url="sqlite:///unused.db",
        telegram_bot_token="token",
        telegram_chat_id="chat-id",
        immediate_notification_min_score=0.82,
        immediate_notification_max_source_age_days=14,
        immediate_notification_max_per_run=2,
    )
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.com/jobs",
        ats_type="jsonld",
        industry="analytics",
        profiles=["growth-analytics"],
        source_verified=True,
    )
    profile = ProfileConfig(
        name="growth-analytics",
        threshold=0.4,
        strong_threshold=0.9,
        weights={"title": 0.6, "location": 0.2, "seniority": 0.2},
        title_terms=["never matches"],
        domain_terms=[],
        skills=[],
    )
    preferences = SearchPreferences(
        location_terms=["Austin"],
        include_remote=False,
    )

    report = await pipeline.run_pipeline(
        settings,
        [company],
        {profile.name: profile},
        preferences,
        backfill=True,
        run_key="backfill-test",
    )

    assert report.matches == 4
    assert report.immediate_candidates == 0
    assert report.notifications == 0
    assert report.notifications_suppressed == 0
    assert report.notifications_pending == 0
    assert len(storage.matches) == 4
    assert len(storage.notifications) == 0
    assert len(storage.outbox) == 0
    assert storage.finished is not None


@pytest.mark.asyncio
async def test_partial_baseline_retry_does_not_enqueue_initial_jobs(tmp_path, monkeypatch):
    raw_jobs = [
        RawJob(
            source_company="acme",
            external_job_id=f"job-{index}",
            title=f"Reporting Analyst {index}",
            location_raw="Austin, TX",
            description_raw="SQL analytics",
            url=f"https://example.com/jobs/{index}",
        )
        for index in range(2)
    ]
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    storage = Storage(database_url, create_schema=True)
    notifier = FakeNotifier()

    class FakeSourceRunner:
        def __init__(self, client, max_concurrency):
            pass

        async def fetch(self, company):
            return raw_jobs

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: storage)
    monkeypatch.setattr(pipeline, "SourceRunner", FakeSourceRunner)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args, **kwargs: notifier)

    settings = Settings(
        database_url=database_url,
        telegram_bot_token="token",
        telegram_chat_id="chat-id",
        immediate_notification_max_per_run=5,
    )
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.com/jobs",
        ats_type="jsonld",
        industry="analytics",
        profiles=["growth-analytics"],
        source_verified=True,
    )
    profile = ProfileConfig(
        name="growth-analytics",
        threshold=0.4,
        strong_threshold=0.8,
        weights={"title": 0.6, "location": 0.2, "seniority": 0.2},
        title_terms=["reporting analyst"],
        domain_terms=[],
        skills=[],
    )
    preferences = SearchPreferences(location_terms=["Austin"], include_remote=False)

    original_batch = storage.persist_job_decisions_batch

    def crash_during_second_job(company_id, run_id, items):
        original_batch(company_id, run_id, items[:1])
        raise RuntimeError("simulated mid-baseline crash")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(storage, "persist_job_decisions_batch", crash_during_second_job)
        with pytest.raises(RuntimeError, match="simulated mid-baseline crash"):
            await pipeline.run_pipeline(
                settings,
                [company],
                {profile.name: profile},
                preferences,
                run_key="baseline-attempt-1",
            )

    company_id = storage.sync_company(company)
    assert storage.has_jobs(company_id)
    assert not storage.is_baseline_completed(company_id)
    assert storage.pending_notification_count() == 0

    retry = await pipeline.run_pipeline(
        settings,
        [company],
        {profile.name: profile},
        preferences,
        run_key="baseline-attempt-2",
    )

    assert retry.sources_succeeded == 1
    assert retry.notifications == 0
    assert retry.immediate_candidates == 0
    assert storage.is_baseline_completed(company_id)
    assert storage.pending_notification_count() == 0


@pytest.mark.asyncio
async def test_retry_drains_outbox_after_crash_following_atomic_job_commit(
    tmp_path,
    monkeypatch,
):
    raw_jobs = [
        RawJob(
            source_company="acme",
            external_job_id=f"job-{index}",
            title=f"Reporting Analyst {index}",
            location_raw="Austin, TX",
            description_raw="SQL analytics",
            url=f"https://example.com/jobs/{index}",
        )
        for index in range(2)
    ]
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    storage = Storage(database_url, create_schema=True)
    notifier = FakeNotifier()

    class FakeSourceRunner:
        def __init__(self, client, max_concurrency):
            pass

        async def fetch(self, company):
            return raw_jobs

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: storage)
    monkeypatch.setattr(pipeline, "SourceRunner", FakeSourceRunner)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args, **kwargs: notifier)

    settings = Settings(
        database_url=database_url,
        telegram_bot_token="token",
        telegram_chat_id="chat-id",
        immediate_notification_max_per_run=5,
    )
    company = CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.com/jobs",
        ats_type="jsonld",
        industry="analytics",
        profiles=["growth-analytics"],
        source_verified=True,
    )
    profile = ProfileConfig(
        name="growth-analytics",
        threshold=0.4,
        strong_threshold=0.8,
        weights={"title": 0.6, "location": 0.2, "seniority": 0.2},
        title_terms=["reporting analyst"],
        domain_terms=[],
        skills=[],
    )
    preferences = SearchPreferences(location_terms=["Austin"], include_remote=False)
    company_id = storage.sync_company(company)
    setup_run = storage.start_run("baseline-setup")
    assert setup_run
    storage.source_succeeded(company_id, setup_run)
    assert storage.finish_run(setup_run, {"sources_succeeded": 1}, [])

    original_batch = storage.persist_job_decisions_batch

    def crash_before_second_job(company_id, run_id, items):
        original_batch(company_id, run_id, items[:1])
        raise RuntimeError("simulated crash after first atomic job")

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(storage, "persist_job_decisions_batch", crash_before_second_job)
        with pytest.raises(RuntimeError, match="simulated crash after first atomic job"):
            await pipeline.run_pipeline(
                settings,
                [company],
                {profile.name: profile},
                preferences,
                run_key="delivery-attempt-1",
            )

    assert storage.pending_notification_count() == 1

    retry = await pipeline.run_pipeline(
        settings,
        [company],
        {profile.name: profile},
        preferences,
        run_key="delivery-attempt-2",
    )

    assert retry.immediate_candidates == 1
    assert retry.notifications == 2
    assert retry.notifications_pending == 0
    for raw_job in raw_jobs:
        job_plan = storage.plan_job(company_id, raw_job)
        assert storage.was_notified(
            job_plan.job_id,
            profile.name,
            raw_job.content_hash,
        )
