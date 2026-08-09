import httpx
import pytest

from job_monitor.models import CompanyConfig, MatchResult, RawJob
from job_monitor.storage import Storage
from job_monitor.web import create_app


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


def raw():
    return RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        location_raw="Phoenix, AZ",
        description_raw="SQL and dashboards",
        url="https://example.com/jobs/1",
    )


@pytest.mark.asyncio
async def test_dashboard_api_uses_storage_dependency(tmp_path):
    db = Storage(f"sqlite:///{tmp_path / 'test.db'}", create_schema=True)
    company_id = db.sync_company(company())
    run_id = db.start_run("run")
    job_id, *_ = db.upsert_job(company_id, run_id, raw())
    db.record_match(
        job_id,
        "1",
        raw().content_hash,
        MatchResult(profile="tech", score=0.82, eligible=True, tier="strong"),
    )

    app = create_app(storage=db)
    assert app.title == "Job Radar TW｜職缺雷達"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        index = await client.get("/")
        assert index.status_code == 200
        assert "Job Radar TW" in index.text
        assert "職缺雷達" in index.text

        response = await client.get("/api/dashboard?days=30")
        assert response.status_code == 200
        assert response.json()["kpis"]["recommended"] == 1

        update = await client.patch(f"/api/jobs/{job_id}/application", json={"stage": "applied"})
        assert update.status_code == 200
        assert update.json()["application"]["stage"] == "applied"
