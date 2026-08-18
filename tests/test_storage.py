import pytest
from sqlalchemy import event, func, select

from job_monitor.models import CompanyConfig, MatchResult, RawJob
from job_monitor.storage import (
    JobIndexRow,
    MatchDecision,
    Storage,
    job_versions,
    jobs,
    match_results,
    notification_outbox,
    source_runs,
)


def company():
    return CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.com/jobs",
        ats_type="jsonld",
        industry="tech",
        profiles=["tech"],
        source_verified=True,
    )


def raw(description="SQL"):
    return RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Phoenix, AZ",
        description_raw=description,
        url="https://example.com/jobs/1",
    )


def raw_job(external_job_id, description="SQL"):
    return RawJob(
        source_company="acme",
        external_job_id=external_job_id,
        title=f"Data Analyst {external_job_id}",
        location_raw="Phoenix, AZ",
        description_raw=description,
        url=f"https://example.com/jobs/{external_job_id}",
    )


def match_decisions():
    return [
        MatchDecision(
            profile_version="1",
            result=MatchResult(profile="tech", score=0.84, eligible=True, tier="strong"),
        )
    ]


def apply_jobs(db, company_id, run_id, raws, batched, decision_indexes=()):
    decision_indexes = set(decision_indexes)
    if not batched:
        return [
            db.persist_job_decisions(
                company_id,
                run_id,
                item,
                db.plan_job(company_id, item),
                match_decisions() if index in decision_indexes else [],
            )
            for index, item in enumerate(raws)
        ]
    index = db.prefetch_job_index(company_id)
    items = [
        (
            item,
            db.plan_job_from_index(item, index.get(item.stable_external_id)),
            match_decisions() if item_index in decision_indexes else [],
        )
        for item_index, item in enumerate(raws)
    ]
    return db.persist_job_decisions_batch(company_id, run_id, items)


def semantic_snapshot(db):
    with db.engine.connect() as conn:
        job_rows = conn.execute(
            select(
                jobs.c.external_job_id,
                jobs.c.title,
                jobs.c.description_raw,
                jobs.c.content_hash,
                jobs.c.status,
                jobs.c.missing_count,
            ).order_by(jobs.c.external_job_id)
        ).all()
        version_rows = conn.execute(
            select(
                jobs.c.external_job_id,
                job_versions.c.content_hash,
                job_versions.c.payload,
            )
            .join(job_versions, job_versions.c.job_id == jobs.c.id)
            .order_by(jobs.c.external_job_id, job_versions.c.content_hash)
        ).all()
        match_rows = conn.execute(
            select(
                jobs.c.external_job_id,
                match_results.c.profile,
                match_results.c.profile_version,
                match_results.c.content_hash,
                match_results.c.score,
                match_results.c.eligible,
                match_results.c.tier,
            )
            .join(match_results, match_results.c.job_id == jobs.c.id)
            .order_by(
                jobs.c.external_job_id,
                match_results.c.profile,
                match_results.c.content_hash,
            )
        ).all()
    return job_rows, version_rows, match_rows


def test_upsert_version_and_two_scan_close(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    assert db.schema_status() == []
    company_id = db.sync_company(company())
    run1 = db.start_run("run-1")
    job_id, is_new, changed, _ = db.upsert_job(company_id, run1, raw())
    assert is_new and changed
    assert db.mark_missing(company_id, run1) == 0

    run2 = db.start_run("run-2")
    assert db.mark_missing(company_id, run2) == 0
    run3 = db.start_run("run-3")
    assert db.mark_missing(company_id, run3) == 1


def test_fresh_running_run_key_is_rejected(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    assert db.start_run("same")
    assert db.start_run("same") is None


def test_stale_running_run_can_be_reclaimed(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    run_id = db.start_run("same")

    assert run_id
    assert db.start_run("same") is None
    reclaimed = db.start_run("same", stale_after_minutes=0)
    assert reclaimed
    assert reclaimed != run_id

    assert not db.finish_run(run_id, {"sources_succeeded": 1}, [])
    with db.engine.connect() as conn:
        row = conn.execute(select(source_runs)).mappings().one()
    assert row["id"] == reclaimed
    assert row["status"] == "running"


def test_reclaimed_run_fences_old_job_transaction(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    old_run_id = db.start_run("same")
    assert old_run_id
    plan = db.plan_job(company_id, raw())

    new_run_id = db.start_run("same", stale_after_minutes=0)
    assert new_run_id and new_run_id != old_run_id

    with pytest.raises(RuntimeError, match="claim is no longer active"):
        db.persist_job_decisions(company_id, old_run_id, raw(), plan, [])

    with db.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(jobs)).scalar_one() == 0


@pytest.mark.parametrize(
    "stats, errors",
    [
        ({"sources_succeeded": 0}, [{"company": "acme", "error": "timeout"}]),
        ({"sources_succeeded": 1}, [{"company": "beta", "error": "timeout"}]),
    ],
    ids=["failed", "partial"],
)
def test_failed_or_partial_run_can_be_reclaimed(tmp_path, stats, errors):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    run_id = db.start_run("same")
    assert run_id
    db.finish_run(run_id, stats, errors)

    reclaimed = db.start_run("same")
    assert reclaimed
    assert reclaimed != run_id


def test_successful_run_cannot_be_reclaimed(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    run_id = db.start_run("same")
    assert run_id
    db.finish_run(run_id, {"sources_succeeded": 1}, [])

    assert db.start_run("same") is None


def test_reverted_job_content_reuses_existing_version(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())

    run1 = db.start_run("run-1")
    job_id, is_new, changed, _ = db.upsert_job(company_id, run1, raw("SQL"))
    assert is_new and changed

    run2 = db.start_run("run-2")
    same_job_id, is_new, changed, _ = db.upsert_job(company_id, run2, raw("Python"))
    assert same_job_id == job_id
    assert not is_new and changed

    run3 = db.start_run("run-3")
    same_job_id, is_new, changed, _ = db.upsert_job(company_id, run3, raw("SQL"))
    assert same_job_id == job_id
    assert not is_new and changed

    with db.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(job_versions)).scalar_one() == 2


def test_notification_lookup_supports_idempotency(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("run")
    job_id, *_ = db.upsert_job(company_id, run_id, raw())
    assert not db.was_notified(job_id, "tech", raw().content_hash)
    db.record_notification(job_id, "tech", raw().content_hash)
    assert db.was_notified(job_id, "tech", raw().content_hash)


def test_notification_outbox_survives_until_delivery_is_recorded(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("run")
    job_id, *_ = db.upsert_job(company_id, run_id, raw())

    assert db.queue_notification(job_id, "tech", raw().content_hash, 0.84, "message")
    assert not db.queue_notification(job_id, "tech", raw().content_hash, 0.84, "message")
    assert db.pending_notification_count() == 1
    queued = db.list_pending_notifications(5)
    assert queued[0]["message"] == "message"
    assert not db.was_notified(job_id, "tech", raw().content_hash)

    claimed = db.claim_pending_notifications(run_id, 5)
    assert len(claimed) == 1
    assert db.list_pending_notifications(5) == []
    competing_run_id = db.start_run("competing-run")
    assert competing_run_id
    assert db.claim_pending_notifications(competing_run_id, 5) == []
    assert not db.mark_notification_sent(run_id, claimed[0]["id"], "wrong-token")
    assert db.mark_notification_sent(
        run_id,
        claimed[0]["id"],
        claimed[0]["claim_token"],
    )
    assert db.pending_notification_count() == 0
    assert db.was_notified(job_id, "tech", raw().content_hash)
    assert not db.mark_notification_sent(
        run_id,
        claimed[0]["id"],
        claimed[0]["claim_token"],
    )


def test_next_run_recovers_outbox_claim_abandoned_by_failed_run(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    first_run = db.start_run("run-1")
    assert first_run
    job_id, *_ = db.upsert_job(company_id, first_run, raw())
    assert db.queue_notification(job_id, "tech", raw().content_hash, 0.84, "message")
    abandoned = db.claim_pending_notifications(first_run, 1)
    assert len(abandoned) == 1
    assert db.finish_run(first_run, {"sources_succeeded": 0}, [{"error": "crash"}])

    next_run = db.start_run("run-2")
    assert next_run
    recovered = db.claim_pending_notifications(next_run, 1)

    assert len(recovered) == 1
    assert recovered[0]["id"] == abandoned[0]["id"]
    assert recovered[0]["claim_token"] != abandoned[0]["claim_token"]
    assert db.mark_notification_sent(
        next_run,
        recovered[0]["id"],
        recovered[0]["claim_token"],
    )


def test_job_match_and_outbox_roll_back_together_at_crash_boundary(tmp_path, monkeypatch):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("run")
    assert run_id
    plan = db.plan_job(company_id, raw())
    result = MatchResult(profile="tech", score=0.84, eligible=True, tier="strong")
    decision = MatchDecision("1", result, notification_message="message")

    def crash_before_outbox(*args, **kwargs):
        raise RuntimeError("simulated outbox crash")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_queue_notification", crash_before_outbox)
        with pytest.raises(RuntimeError, match="simulated outbox crash"):
            db.persist_job_decisions(company_id, run_id, raw(), plan, [decision])

    with db.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(jobs)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(job_versions)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(match_results)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(notification_outbox)).scalar_one() == 0

    persisted = db.persist_job_decisions(company_id, run_id, raw(), plan, [decision])
    assert persisted.is_new
    assert persisted.notifications_enqueued == 1
    with db.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(jobs)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(match_results)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(notification_outbox)).scalar_one() == 1


def test_content_change_removes_superseded_pending_notification(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    first_run = db.start_run("run-1")
    assert first_run
    job_id, *_ = db.upsert_job(company_id, first_run, raw("SQL"))
    assert db.queue_notification(job_id, "tech", raw("SQL").content_hash, 0.84, "old")

    second_run = db.start_run("run-2")
    assert second_run
    updated = raw("Python")
    plan = db.plan_job(company_id, updated)
    db.persist_job_decisions(company_id, second_run, updated, plan, [])

    assert db.pending_notification_count() == 0
    assert db.claim_pending_notifications(second_run, 5) == []


def test_closing_job_removes_pending_notification(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    first_run = db.start_run("run-1")
    assert first_run
    job_id, *_ = db.upsert_job(company_id, first_run, raw())
    assert db.queue_notification(job_id, "tech", raw().content_hash, 0.84, "message")

    second_run = db.start_run("run-2")
    third_run = db.start_run("run-3")
    assert second_run and third_run
    assert db.mark_missing(company_id, second_run) == 0
    assert db.mark_missing(company_id, third_run) == 1

    assert db.pending_notification_count() == 0


def test_dashboard_counts_application_pipeline(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("run")
    job_id, *_ = db.upsert_job(company_id, run_id, raw())
    db.record_match(
        job_id,
        "1",
        raw().content_hash,
        MatchResult(profile="tech", score=0.84, eligible=True, tier="strong"),
    )

    before = db.dashboard_snapshot(days=30)
    assert before["kpis"]["recommended"] == 1
    assert before["kpis"]["applied"] == 0

    db.set_application_stage(job_id, "applied")
    applied = db.dashboard_snapshot(days=30)
    assert applied["kpis"]["applied"] == 1
    assert applied["kpis"]["interviews"] == 0

    db.set_application_stage(job_id, "interview")
    after = db.dashboard_snapshot(days=30)
    assert after["kpis"]["interviews"] == 1
    assert after["kpis"]["apply_rate"] == 1.0
    assert after["kpis"]["interview_rate"] == 1.0

    jobs = db.list_dashboard_jobs(days=30)
    assert jobs[0]["stage"] == "interview"
    assert jobs[0]["score"] == 0.84


def test_batched_persistence_matches_single_job_rows_and_counters(tmp_path):
    raws_first = [
        raw_job("1", "SQL"),
        raw_job("2", "Python"),
        raw_job("3", "R"),
    ]
    raws_second = [
        raw_job("1", "SQL changed"),
        raw_job("2", "Python"),
        raw_job("3", "R changed"),
    ]

    snapshots = []
    result_flags = []
    for batched in (False, True):
        db = Storage(f"sqlite:///{tmp_path / f'{batched}.db'}", create_schema=True)
        company_id = db.sync_company(company())
        first_run = db.start_run(f"first-{batched}")
        second_run = db.start_run(f"second-{batched}")
        assert first_run and second_run

        first_results = apply_jobs(
            db,
            company_id,
            first_run,
            raws_first,
            batched,
            decision_indexes={0, 1, 2},
        )
        second_results = apply_jobs(
            db,
            company_id,
            second_run,
            raws_second,
            batched,
            decision_indexes={0, 1, 2},
        )
        result_flags.append(
            [
                [(item.is_new, item.changed, item.notifications_enqueued) for item in results]
                for results in (first_results, second_results)
            ]
        )
        snapshots.append(semantic_snapshot(db))

    assert result_flags[0] == result_flags[1]
    assert snapshots[0] == snapshots[1]


def test_batched_snapshot_mismatch_falls_back_to_single_job_persistence(tmp_path, monkeypatch):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("batch")
    racing_run_id = db.start_run("racing")
    assert run_id and racing_run_id

    item = raw_job("raced")
    stale_plan = db.plan_job(company_id, item)
    db.upsert_job(company_id, racing_run_id, item)

    fallback_calls = []
    original_persist = db.persist_job_decisions

    def fallback(*args, **kwargs):
        fallback_calls.append(True)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(db, "persist_job_decisions", fallback)
    with pytest.raises(RuntimeError, match="job changed while its match decision"):
        db.persist_job_decisions_batch(company_id, run_id, [(item, stale_plan, [])])
    assert fallback_calls == [True]


def test_batched_persistence_sql_scales_by_chunk(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("batch")
    assert run_id
    raws = [raw_job(str(index)) for index in range(50)]
    statements = []
    event.listen(
        db.engine,
        "before_cursor_execute",
        lambda *args: statements.append(args[2]),
    )

    apply_jobs(db, company_id, run_id, raws, batched=True)

    assert len(statements) < len(raws) * 5


def _duplicate_items(db, company_id, raws):
    index = db.prefetch_job_index(company_id)
    items = []
    for item in raws:
        plan = db.plan_job_from_index(item, index.get(item.stable_external_id))
        index[item.stable_external_id] = JobIndexRow(
            job_id=plan.job_id,
            content_hash=item.content_hash,
            first_seen_at=plan.first_seen_at,
        )
        items.append((item, plan, []))
    return items


def test_batched_duplicate_external_id_with_identical_content(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("batch")
    assert run_id
    item = raw_job("duplicate")

    results = db.persist_job_decisions_batch(
        company_id,
        run_id,
        _duplicate_items(db, company_id, [item, item]),
    )

    assert [(result.is_new, result.changed) for result in results] == [
        (True, True),
        (False, False),
    ]
    with db.engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(jobs).where(jobs.c.company_id == company_id)
            ).scalar_one()
            == 1
        )


def test_batched_duplicate_external_id_with_changed_second_content(tmp_path, monkeypatch):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("batch")
    assert run_id
    first = raw_job("duplicate", "first")
    second = raw_job("duplicate", "second")
    fallback_calls = []
    original_persist = db.persist_job_decisions

    def fallback(*args, **kwargs):
        fallback_calls.append(True)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(db, "persist_job_decisions", fallback)

    results = db.persist_job_decisions_batch(
        company_id,
        run_id,
        _duplicate_items(db, company_id, [first, second]),
    )

    assert [(result.is_new, result.changed) for result in results] == [
        (True, True),
        (False, True),
    ]
    assert fallback_calls == [True]
    plan = db.plan_job(company_id, second)
    assert plan.is_new is False
    assert plan.changed is False
