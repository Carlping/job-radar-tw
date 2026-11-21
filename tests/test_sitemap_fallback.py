import httpx
import pytest
import respx

from job_monitor.models import CompanyConfig
from job_monitor.sitemap import SitemapError, fetch_sitemap_jobs, robots_allowed
from job_monitor.sources import SourceRunner


def company(ats_type="sitemap", ats_config=None):
    return CompanyConfig(
        slug="my-company",
        name="My Company",
        careers_url="https://careers.example.com/jobs",
        ats_type=ats_type,
        ats_config=ats_config or {"sitemap_url": "https://careers.example.com/sitemap.xml"},
        industry="tech",
        profiles=["tech"],
        source_verified=True,
    )


ROBOTS = """
User-agent: *
Disallow: /
Allow: /jobs
Allow: /sitemap.xml
"""

XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
 <url><loc>https://careers.example.com/jobs/123</loc>
 <lastmod>2026-11-21</lastmod></url>
</urlset>
"""

JOB = """<html><head><script type="application/ld+json">{
  "@type":"JobPosting",
  "title":"Senior Data Analyst",
  "identifier":{"value":"req-123"},
  "jobLocation":{"address":{"addressLocality":"Austin","addressRegion":"TX"}},
  "datePosted":"2026-11-20",
  "description":"<p>SQL and Python</p>"
}</script></head></html>"""


def test_robots_longest_match_allow_and_denial():
    assert robots_allowed(ROBOTS, "https://careers.example.com/jobs/123")
    assert robots_allowed(ROBOTS, "https://careers.example.com/sitemap.xml")
    assert not robots_allowed(ROBOTS, "https://careers.example.com/admin")
    assert robots_allowed("User-agent: *\nDisallow: /\nAllow: /careers", "https://x.com/careers")
    assert not robots_allowed(
        "User-agent: *\nDisallow: /\nAllow: /api/pcsx",
        "https://x.com/api/sensitive",
    )


@pytest.mark.asyncio
@respx.mock
async def test_generic_sitemap_parses_jobposting_without_workday():
    respx.get("https://careers.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS)
    )
    respx.get("https://careers.example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=XML)
    )
    detail = respx.get("https://careers.example.com/jobs/123").mock(
        return_value=httpx.Response(200, text=JOB)
    )
    async with httpx.AsyncClient() as client:
        rows = await fetch_sitemap_jobs(company(), client)
    assert detail.call_count == 1
    assert len(rows) == 1
    assert rows[0].title == "Senior Data Analyst"
    assert rows[0].external_job_id == "sitemap:req-123"
    assert rows[0].location_raw == "Austin, TX"
    assert rows[0].description_raw == "SQL and Python"


@pytest.mark.asyncio
@respx.mock
async def test_generic_sitemap_respects_explicit_denial():
    respx.get("https://careers.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    sitemap = respx.get("https://careers.example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=XML)
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SitemapError, match="disallowed"):
            await fetch_sitemap_jobs(company(), client)
    assert sitemap.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_workday_429_uses_configured_official_sitemap_fallback():
    blocked = respx.post(
        "https://workday.example.com/wday/cxs/acme/External/jobs"
    ).mock(return_value=httpx.Response(429, headers={"cf-mitigated": "challenge"}))
    respx.get("https://careers.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=ROBOTS)
    )
    respx.get("https://careers.example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=XML)
    )
    respx.get("https://careers.example.com/jobs/123").mock(
        return_value=httpx.Response(200, text=JOB)
    )
    cfg = company(
        "workday",
        {
            "endpoint": "https://workday.example.com/wday/cxs/acme/External/jobs",
            "site": "workday.example.com",
            "detail_base_url": "https://workday.example.com/en-US/External",
            "fallback_sources": [
                {
                    "ats_type": "sitemap",
                    "careers_url": "https://careers.example.com/jobs",
                    "ats_config": {
                        "sitemap_url": "https://careers.example.com/sitemap.xml"
                    },
                }
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        rows = await SourceRunner(client).fetch(cfg)
    assert blocked.call_count == 1
    assert len(rows) == 1
    assert rows[0].metadata["_incremental_fallback"] is True
    assert rows[0].metadata["_fallback_ats"] == "sitemap"


@pytest.mark.asyncio
@respx.mock
async def test_workday_429_without_configured_fallback_stays_failed():
    endpoint = "https://workday.example.com/wday/cxs/acme/External/jobs"
    respx.post(endpoint).mock(
        return_value=httpx.Response(429, headers={"cf-mitigated": "challenge"})
    )
    cfg = company(
        "workday",
        {
            "endpoint": endpoint,
            "site": "workday.example.com",
            "detail_base_url": "https://workday.example.com/en-US/External",
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception, match="cloudflare_challenge"):
            await SourceRunner(client).fetch(cfg)


def test_workday_rejects_invalid_fallback_config():
    with pytest.raises(ValueError, match="fallback_sources"):
        company(
            "workday",
            {
                "endpoint": "https://workday.example.com/jobs",
                "site": "workday.example.com",
                "detail_base_url": "https://workday.example.com/en",
                "fallback_sources": [
                    {"ats_type": "workday", "careers_url": "https://careers.example.com/jobs"}
                ],
            },
        )
