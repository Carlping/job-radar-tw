import httpx
import pytest
import respx

from job_monitor.models import CompanyConfig
from job_monitor.sources import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    SmartRecruitersSource,
    SourceError,
    WorkdaySource,
)


def company(ats_type, ats_config):
    return CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://example.com/jobs",
        ats_type=ats_type,
        ats_config=ats_config,
        industry="tech",
        profiles=["tech"],
        source_verified=True,
    )


@pytest.mark.asyncio
@respx.mock
async def test_greenhouse_adapter():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 7,
                        "title": "Senior Data Analyst",
                        "location": {"name": "Remote US"},
                        "content": "<p>SQL</p>",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                        "updated_at": "2026-06-17T12:00:00Z",
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await GreenhouseSource(
            company("greenhouse", {"board_token": "acme"}), client
        ).fetch()
    assert rows[0].external_job_id == "7"
    assert rows[0].description_raw == "SQL"


@pytest.mark.asyncio
@respx.mock
async def test_lever_adapter():
    respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "x",
                    "text": "Data Analyst",
                    "categories": {"location": "Phoenix, AZ"},
                    "descriptionPlain": "SQL",
                    "hostedUrl": "https://jobs.lever.co/acme/x",
                    "createdAt": 1700000000000,
                }
            ],
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await LeverSource(company("lever", {"site": "acme"}), client).fetch()
    assert len(rows) == 1


@pytest.mark.asyncio
@respx.mock
async def test_ashby_adapter():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "a",
                        "title": "Analytics Engineer",
                        "location": "Remote US",
                        "descriptionHtml": "<p>dbt</p>",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/a",
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await AshbySource(company("ashby", {"board_name": "acme"}), client).fetch()
    assert rows[0].description_raw == "dbt"


@pytest.mark.asyncio
@respx.mock
async def test_smartrecruiters_pagination():
    route = respx.get("https://api.smartrecruiters.com/v1/companies/acme/postings")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    {
                        "id": "s",
                        "name": "BI Analyst",
                        "location": {"city": "Dallas", "region": "TX"},
                        "ref": "https://jobs.smartrecruiters.com/acme/s",
                    }
                ],
            },
        ),
        httpx.Response(200, json={"totalFound": 1, "content": []}),
    ]
    respx.get("https://api.smartrecruiters.com/v1/companies/acme/postings/s").mock(
        return_value=httpx.Response(
            200, json={"jobAd": {"sections": {"jobDescription": {"text": "<p>SQL</p>"}}}}
        )
    )
    async with httpx.AsyncClient() as client:
        rows = await SmartRecruitersSource(
            company("smartrecruiters", {"company_identifier": "acme"}), client
        ).fetch()
    assert len(rows) == 1
    assert "Dallas" in rows[0].location_raw
    assert rows[0].description_raw == "SQL"


@pytest.mark.asyncio
@respx.mock
async def test_workday_searches_and_deduplicates():
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    route = respx.post(endpoint)
    posting = {
        "title": "Senior Data Analyst",
        "externalPath": "/job/Phoenix/Senior-Data-Analyst_R1",
        "locationsText": "Phoenix, AZ",
        "bulletFields": ["R1"],
        "postedOn": "2026-06-17T00:00:00Z",
    }
    route.side_effect = [
        httpx.Response(200, json={"total": 1, "jobPostings": [posting]}),
        httpx.Response(200, json={"total": 1, "jobPostings": [posting]}),
    ]
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
            "search_texts": ["data", "analytics"],
        },
    )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    assert len(rows) == 1
    assert rows[0].external_job_id.endswith("_R1")


@pytest.mark.asyncio
@respx.mock
async def test_workday_skips_posting_missing_title(caplog):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    respx.post(endpoint).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 3,
                "jobPostings": [
                    {
                        "title": "Data Analyst",
                        "externalPath": "/job/Phoenix/Data-Analyst_R1",
                    },
                    {"externalPath": "/job/Phoenix/Malformed_R2"},
                    {"title": "Missing URL"},
                ],
            },
        )
    )
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
        },
    )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(cfg, client).fetch()
    assert len(rows) == 1
    assert rows[0].title == "Data Analyst"
    assert "Skipping malformed Workday posting for acme" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"jobPostings": {}},
        {"jobPostings": None},
    ],
)
@respx.mock
async def test_workday_rejects_invalid_job_postings(payload):
    endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    respx.post(endpoint).mock(return_value=httpx.Response(200, json=payload))
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "acme.wd1.myworkdayjobs.com",
            "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="jobPostings"):
            await WorkdaySource(cfg, client).fetch()
