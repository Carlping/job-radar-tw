import httpx
import pytest
import respx

from job_monitor.models import CompanyConfig
from job_monitor.sources import (
    WorkdayBlocked,
    WorkdaySource,
    source_error_kind,
)

ENDPOINT = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
DETAIL_BASE = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External"


def company(*, include_details: bool = False) -> CompanyConfig:
    cfg = {
        "endpoint": ENDPOINT,
        "site": "acme.wd1.myworkdayjobs.com",
        "detail_base_url": "https://acme.wd1.myworkdayjobs.com/en-US/External",
    }
    if include_details:
        cfg["detail_api_base"] = DETAIL_BASE
    return CompanyConfig(
        slug="acme",
        name="Acme",
        careers_url="https://acme.wd1.myworkdayjobs.com/External",
        ats_type="workday",
        ats_config=cfg,
        industry="healthcare",
        profiles=["healthcare"],
        source_verified=True,
    )


@pytest.mark.asyncio
@respx.mock
async def test_cloudflare_challenge_stops_immediately(monkeypatch):
    monkeypatch.setattr(WorkdaySource, "REQUEST_SPACING_SECONDS", 0)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            headers={
                "cf-mitigated": "challenge",
                "retry-after": "0",
                "content-type": "text/html",
            },
            text="<html>Challenge</html>",
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(WorkdayBlocked) as exc_info:
            await WorkdaySource(company(), client).fetch()
    assert exc_info.value.kind == "cloudflare_challenge"
    assert source_error_kind(exc_info.value) == "workday_cloudflare_challenge"
    assert route.call_count == 1
    assert "Challenge</html>" not in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_ordinary_429_retries_once_when_retry_after_is_short(monkeypatch):
    monkeypatch.setattr(WorkdaySource, "REQUEST_SPACING_SECONDS", 0)
    route = respx.post(ENDPOINT)
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(200, json={"total": 0, "jobPostings": []}),
    ]
    async with httpx.AsyncClient() as client:
        assert await WorkdaySource(company(), client).fetch() == []
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_long_retry_after_fails_without_hammering(monkeypatch):
    monkeypatch.setattr(WorkdaySource, "REQUEST_SPACING_SECONDS", 0)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(429, headers={"retry-after": "300"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(WorkdayBlocked) as exc_info:
            await WorkdaySource(company(), client).fetch()
    assert exc_info.value.kind == "rate_limited"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_blocked_details_keep_verified_search_results(monkeypatch):
    monkeypatch.setattr(WorkdaySource, "REQUEST_SPACING_SECONDS", 0)
    path = "/job/Austin/Senior-Analyst_R1"
    search = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Senior Analyst",
                        "externalPath": path,
                        "locationsText": "Austin, TX",
                        "bulletFields": ["Data analytics", "SQL"],
                    }
                ],
            },
        )
    )
    detail = respx.get(DETAIL_BASE + path).mock(
        return_value=httpx.Response(429, headers={"cf-mitigated": "challenge"})
    )
    async with httpx.AsyncClient() as client:
        rows = await WorkdaySource(company(include_details=True), client).fetch()
    assert len(rows) == 1
    assert rows[0].description_raw == "Data analytics SQL"
    assert rows[0].external_job_id == path
    assert search.call_count == 1
    assert detail.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_blocked_search_raises_instead_of_claiming_zero_jobs(monkeypatch):
    monkeypatch.setattr(WorkdaySource, "REQUEST_SPACING_SECONDS", 0)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(403, headers={"cf-mitigated": "challenge"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(WorkdayBlocked):
            await WorkdaySource(company(), client).fetch()
    assert route.call_count == 1
