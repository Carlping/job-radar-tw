import pytest
from pydantic import ValidationError

from job_monitor.models import CompanyConfig, RawJob


def test_fallback_id_and_canonical_url_are_stable():
    first = RawJob(
        source_company="acme",
        title="Data Analyst",
        location_raw="Phoenix, AZ",
        url="https://example.com/jobs/1?ref=foo",
    )
    second = RawJob(
        source_company="acme",
        title="Data Analyst",
        location_raw="Phoenix, AZ",
        url="https://example.com/jobs/1?ref=bar",
    )
    assert first.stable_external_id == second.stable_external_id
    assert first.canonical_url == "https://example.com/jobs/1"


def test_description_change_changes_content_hash():
    first = RawJob(
        source_company="acme",
        external_job_id="1",
        title="Data Analyst",
        description_raw="SQL",
        url="https://example.com/1",
    )
    second = first.model_copy(update={"description_raw": "SQL and Python"})
    assert first.content_hash != second.content_hash


def test_company_rejects_duplicate_profiles():
    with pytest.raises(ValidationError):
        CompanyConfig(
            slug="acme",
            name="Acme",
            careers_url="https://example.com/jobs",
            ats_type="jsonld",
            industry="technology",
            profiles=["tech", "tech"],
        )
