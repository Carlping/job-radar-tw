import httpx
import pytest
import respx

from job_monitor.models import AtsType, CompanyConfig
from job_monitor.sources import CareerHtmlSource, EightfoldSource, SourceError


def _company(ats_type, config, careers_url):
    return CompanyConfig(
        slug="example",
        name="Example",
        careers_url=careers_url,
        ats_type=ats_type,
        ats_config=config,
        industry="technology",
        profiles=["tech"],
        source_verified=True,
    )


@pytest.mark.asyncio
@respx.mock
async def test_eightfold_official_search_is_incremental_and_uses_stable_ids():
    api = "https://jobs.example.com/api/pcsx/search"
    route = respx.get(api).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": 200,
                "data": {
                    "count": 200,
                    "positions": [
                        {
                            "id": 893398847910,
                            "name": "Senior Data Analyst",
                            "locations": ["Austin, TX", "Taipei, Taiwan"],
                            "department": "Analytics",
                            "postedTs": 1795219200,
                        }
                    ],
                },
            },
        )
    )
    cfg = _company(
        "eightfold",
        {"search_url": api, "domain": "example.com", "search_texts": ["data"], "max_details": 0},
        "https://jobs.example.com/careers",
    )
    async with httpx.AsyncClient() as client:
        found = await EightfoldSource(cfg, client).fetch()
    assert route.call_count == 1
    assert len(found) == 1
    assert found[0].external_job_id == "eightfold:893398847910"
    assert str(found[0].url) == "https://jobs.example.com/careers/job/893398847910"
    assert "Austin" in found[0].location_raw
    assert "Analytics" in found[0].description_raw
    assert route.calls[0].request.url.params["query"] == "data"
    assert route.calls[0].request.url.params["sort_by"] == "timestamp"


@pytest.mark.asyncio
@respx.mock
async def test_eightfold_rejects_valid_http_with_invalid_search_data():
    api = "https://jobs.example.com/api/pcsx/search"
    respx.get(api).mock(
        return_value=httpx.Response(200, json={"status": 200, "data": {"message": "error"}})
    )
    cfg = _company(
        "eightfold",
        {"search_url": api, "domain": "example.com", "max_details": 0},
        "https://jobs.example.com/careers",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="positions"):
            await EightfoldSource(cfg, client).fetch()


@pytest.mark.asyncio
@respx.mock
async def test_html_careers_reads_official_job_card_without_detail_requests():
    base = "https://jobs.example.com/en/search-jobs"
    route = respx.get(base).mock(
        return_value=httpx.Response(
            200,
            text="""
                <ul>
                  <li class="search-results-list__item">
                    <h2 class="search-results-list__job-title">
                      <a href="/en/job/austin/data-product-manager/95/103549965296">
                        Data Product Manager
                      </a>
                    </h2>
                    <span>Austin, TX</span><time>11/20/2026</time>
                    <span>Information Technology</span>
                  </li>
                </ul>
            """,
        )
    )
    cfg = _company(
        "career_html",
        {"listing_url": base},
        base,
    )
    async with httpx.AsyncClient() as client:
        rows = await CareerHtmlSource(cfg, client).fetch()
    assert route.call_count == 1
    assert len(rows) == 1
    assert rows[0].external_job_id == "careerhtml:103549965296"
    assert rows[0].title == "Data Product Manager"
    assert rows[0].location_raw == "Austin, TX"
    assert rows[0].posted_at.year == 2026


@pytest.mark.asyncio
@respx.mock
async def test_html_careers_fails_closed_on_missing_cards():
    base = "https://jobs.example.com/en/search-jobs"
    respx.get(base).mock(return_value=httpx.Response(200, text="<html>Challenge</html>"))
    cfg = _company("career_html", {"listing_url": base}, base)
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="job cards"):
            await CareerHtmlSource(cfg, client).fetch()


def test_incremental_ats_types_exist():
    assert AtsType("eightfold") is AtsType.EIGHTFOLD
    assert AtsType("career_html") is AtsType.CAREER_HTML



@pytest.mark.asyncio
@respx.mock
async def test_eightfold_enriches_job_with_official_description():
    api = "https://jobs.example.com/api/pcsx/search"
    details = "https://jobs.example.com/api/pcsx/position_details"
    search = respx.get(api).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": 200,
                "data": {
                    "count": 1,
                    "positions": [
                        {
                            "id": 893398847910,
                            "name": "Senior Data Analyst",
                            "locations": ["Austin, TX"],
                            "department": "Analytics",
                        }
                    ],
                },
            },
        )
    )
    detail = respx.get(details).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": 200,
                "data": {"jobDescription": "<p>SQL, Python, Tableau and analytics</p>"},
            },
        )
    )
    cfg = _company(
        "eightfold",
        {"search_url": api, "domain": "example.com", "max_details": 1},
        "https://jobs.example.com/careers",
    )
    async with httpx.AsyncClient() as client:
        found = await EightfoldSource(cfg, client).fetch()
    assert search.call_count == 1
    assert detail.call_count == 1
    assert detail.calls[0].request.url.params["position_id"] == "893398847910"
    assert found[0].description_raw == "SQL, Python, Tableau and analytics"
