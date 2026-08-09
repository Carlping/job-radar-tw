import httpx
import pytest
import respx

from job_monitor.config import Settings
from job_monitor.models import CompanyConfig
from job_monitor.onboarding import (
    company_inventory,
    filter_companies,
    load_source_candidates,
    promote_companies_in_config,
    verify_companies,
)


def company(slug="acme", *, enabled=False, source_verified=False):
    return CompanyConfig(
        slug=slug,
        name=slug.title(),
        careers_url=f"https://boards.greenhouse.io/{slug}",
        ats_type="greenhouse",
        ats_config={"board_token": slug},
        industry="tech",
        profiles=["tech"],
        enabled=enabled,
        source_verified=source_verified,
    )


def test_filter_companies_and_inventory():
    companies = [company("disabled"), company("enabled", enabled=True, source_verified=True)]

    disabled = filter_companies(companies, status="disabled")
    enabled = filter_companies(companies, status="enabled")
    all_items = company_inventory(filter_companies(companies, status="all"))

    assert [item.slug for item in disabled] == ["disabled"]
    assert [item.slug for item in enabled] == ["enabled"]
    assert all_items[0]["ats_type"] == "greenhouse"


@pytest.mark.asyncio
@respx.mock
async def test_verify_companies_marks_disabled_source_ready_to_enable():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Data Analyst",
                        "location": {"name": "New York, NY"},
                        "content": "<p>SQL</p>",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    }
                ]
            },
        )
    )

    results = await verify_companies([company()], Settings(), min_jobs=1)

    assert len(results) == 1
    assert results[0].ok
    assert results[0].jobs_count == 1
    assert results[0].ready_to_enable
    assert results[0].sample_jobs[0]["title"] == "Data Analyst"


def test_promote_companies_in_config_updates_inline_entries(tmp_path):
    config = tmp_path / "companies.yml"
    config.write_text(
        "\n".join(
            [
                "companies:",
                '  - {slug: acme, name: Acme, careers_url: "https://boards.greenhouse.io/acme", ats_type: greenhouse, ats_config: {board_token: acme}, industry: tech, profiles: [tech], enabled: false, source_verified: false}',
                '  - {slug: beta, name: Beta, careers_url: "https://boards.greenhouse.io/beta", ats_type: greenhouse, ats_config: {board_token: beta}, industry: tech, profiles: [tech], enabled: false}',
            ]
        ),
        encoding="utf-8",
    )

    promoted = promote_companies_in_config(config, ["acme", "beta"])
    text = config.read_text(encoding="utf-8")

    assert promoted == ["acme", "beta"]
    assert "slug: acme" in text
    assert "enabled: true, source_verified: true" in text
    assert "slug: beta" in text
    assert "enabled: true, source_verified: true" in text


def test_load_source_candidates(tmp_path):
    path = tmp_path / "candidates.yml"
    path.write_text(
        "candidates:\n"
        '  - {slug: acme, name: Acme, category: tsmc_vendor_phoenix, careers_url: "https://example.com"}\n',
        encoding="utf-8",
    )

    candidates = load_source_candidates(path)

    assert candidates[0]["slug"] == "acme"
