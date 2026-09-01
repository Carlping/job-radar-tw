from datetime import UTC, datetime, timedelta

from job_monitor.handoff import build_handoff, render_json, render_markdown
from job_monitor.models import CompanyConfig, MatchResult, RawJob
from job_monitor.storage import MatchDecision, Storage


def company(slug="acme", name="Acme"):
    return CompanyConfig(
        slug=slug,
        name=name,
        careers_url="https://example.com/jobs",
        ats_type="jsonld",
        industry="tech",
        profiles=["tech"],
        source_verified=True,
    )


def raw(external_job_id="1", description="SQL and Python", title="Data Analyst"):
    return RawJob(
        source_company="acme",
        external_job_id=external_job_id,
        title=title,
        location_raw="Phoenix, AZ",
        description_raw=description,
        url=f"https://example.com/jobs/{external_job_id}",
    )


def decision(profile="tech", score=0.84, bucket="target", eligible=True, tier="strong"):
    return MatchDecision(
        profile_version="1",
        result=MatchResult(
            profile=profile,
            score=score,
            eligible=eligible,
            tier=tier,
            bucket=bucket,
            reasons=["title: data analyst"],
            gaps=["要求碩士學位"],
        ),
    )


def seed(tmp_path, items, run_key="run-1"):
    db = Storage(f"sqlite:///{tmp_path / 'handoff.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run(run_key)
    for item, decisions in items:
        db.persist_job_decisions(company_id, run_id, item, db.plan_job(company_id, item), decisions)
    db.finish_run(run_id, {"sources_attempted": 1, "sources_succeeded": 1, "jobs_fetched": 1}, [])
    return db


def test_handoff_excludes_scores_from_an_older_job_version(tmp_path):
    db = seed(tmp_path, [(raw(description="old"), [decision()])])
    assert len(db.list_handoff_jobs()) == 1

    company_id = db.sync_company(company())
    run2 = db.start_run("run-2")
    changed = raw(description="rewritten description")
    db.persist_job_decisions(company_id, run2, changed, db.plan_job(company_id, changed), [])
    db.finish_run(run2, {"sources_attempted": 1, "sources_succeeded": 1, "jobs_fetched": 1}, [])

    assert db.list_handoff_jobs() == []


def test_handoff_keeps_the_highest_scoring_current_match_per_job(tmp_path):
    db = seed(
        tmp_path,
        [
            (
                raw(),
                [
                    decision(profile="tech", score=0.84),
                    decision(profile="healthcare", score=0.91),
                ],
            )
        ],
    )
    rows = db.list_handoff_jobs()
    assert [(row["profile"], row["score"]) for row in rows] == [("healthcare", 0.91)]


def test_handoff_filters_ineligible_stretch_and_actioned_jobs(tmp_path):
    db = seed(
        tmp_path,
        [
            (raw("1"), [decision(score=0.9)]),
            (raw("2"), [decision(score=0.5, bucket="stretch")]),
            (raw("3"), [decision(score=0.2, eligible=False, tier="below_threshold")]),
            (raw("4"), [decision(score=0.8)]),
        ],
    )
    applied = next(row for row in db.list_handoff_jobs() if row["url"].endswith("/4"))
    db.set_application_stage(applied["job_id"], "applied", notes="secret private note")

    assert [row["url"].rsplit("/", 1)[-1] for row in db.list_handoff_jobs()] == ["1", "2"]
    assert [row["url"].rsplit("/", 1)[-1] for row in db.list_handoff_jobs(buckets=("target",))] == [
        "1"
    ]
    assert db.list_handoff_jobs(min_score=0.95) == []


def test_handoff_window_is_measured_from_the_reference_time(tmp_path):
    db = seed(tmp_path, [(raw(), [decision()])])
    run = db.latest_run()
    assert run is not None and run["run_key"] == "run-1"

    reference = datetime.now(UTC)
    assert len(db.list_handoff_jobs(days=7, now=reference)) == 1
    assert db.list_handoff_jobs(days=7, now=reference + timedelta(days=30)) == []


def test_handoff_payload_is_deterministic_and_change_sensitive(tmp_path):
    db = seed(tmp_path, [(raw(), [decision()])])
    run = db.latest_run()
    jobs = db.list_handoff_jobs(now=datetime.now(UTC))

    first = build_handoff(jobs, run, window_days=7, buckets=("target", "stretch"))
    second = build_handoff(
        db.list_handoff_jobs(now=datetime.now(UTC) + timedelta(hours=3)),
        run,
        window_days=7,
        buckets=("target", "stretch"),
    )
    assert first["content_hash"] == second["content_hash"]
    assert render_markdown(first) == render_markdown(second)
    assert render_json(first) == render_json(second)

    narrower = build_handoff(jobs, run, window_days=3, buckets=("target", "stretch"))
    assert narrower["content_hash"] != first["content_hash"]
    assert build_handoff([], run, window_days=7, buckets=("target",))["counts"]["total"] == 0


def test_handoff_reports_source_coverage_and_run_key(tmp_path):
    db = seed(tmp_path, [(raw(), [decision()])])
    payload = build_handoff(
        db.list_handoff_jobs(), db.latest_run(), window_days=7, buckets=("target", "stretch")
    )
    assert payload["schema_version"] == 1
    assert payload["source_run"]["run_key"] == "run-1"
    assert payload["source_run"]["status"] == "success"
    assert payload["source_run"]["finished_at"] is not None
    assert payload["source_run"]["coverage"] == {
        "sources_attempted": 1,
        "sources_succeeded": 1,
        "jobs_fetched": 1,
    }
    markdown = render_markdown(payload)
    assert "run-1" in markdown
    assert "1/1 sources" in markdown


def test_handoff_never_exports_descriptions_or_application_notes(tmp_path):
    db = seed(tmp_path, [(raw(description="CONFIDENTIAL DESCRIPTION"), [decision()])])
    rows = db.list_handoff_jobs()
    db.set_application_stage(rows[0]["job_id"], "recommended", notes="PRIVATE NOTE")

    payload = build_handoff(
        db.list_handoff_jobs(), db.latest_run(), window_days=7, buckets=("target", "stretch")
    )
    rendered = render_markdown(payload) + render_json(payload)
    assert "CONFIDENTIAL DESCRIPTION" not in rendered
    assert "PRIVATE NOTE" not in rendered
    assert set(payload["jobs"][0]) == {
        "job_id",
        "title",
        "company",
        "company_slug",
        "industry",
        "location",
        "url",
        "profile",
        "score",
        "tier",
        "bucket",
        "fit",
        "reach",
        "level",
        "required_years_min",
        "reasons",
        "gaps",
        "first_seen_at",
        "source_posted_at",
        "content_hash",
    }
