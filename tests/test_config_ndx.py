from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from job_monitor.config import ProfileConfig, Settings, load_preferences
from job_monitor.models import CompanyConfig, Seniority
from job_monitor.ndx import NASDAQ_100_URL, fetch_ndx_constituents


def test_llm_requires_key_and_model():
    with pytest.raises(ValidationError):
        Settings(llm_enabled=True, openai_api_key=None, openai_model=None)
    assert not Settings(llm_enabled=False).llm_enabled


def test_blank_resume_path_is_unset():
    assert Settings(resume_path="").resume_path is None


def test_resume_text_can_be_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("RESUME_TEXT", "SQL and Python analytics")

    settings = Settings(_env_file=None)

    assert settings.resume_text is not None
    assert settings.resume_text.get_secret_value() == "SQL and Python analytics"


def test_load_preferences_accepts_custom_locations_remote_and_filters(tmp_path):
    path = Path(tmp_path) / "preferences.yml"
    path.write_text(
        """
preferences:
  location_terms: [Chicago, Toronto]
  include_remote: false
  exclude_citizenship_required: false
  exclude_clearance_required: false
  excluded_seniorities: [director_plus]
""".strip(),
        encoding="utf-8",
    )

    preferences = load_preferences(path)

    assert preferences.location_terms == ["Chicago", "Toronto"]
    assert not preferences.include_remote
    assert not preferences.exclude_citizenship_required
    assert not preferences.exclude_clearance_required
    assert preferences.excluded_seniorities == {Seniority.DIRECTOR}


def test_ats_config_contract():
    with pytest.raises(ValidationError):
        CompanyConfig(
            slug="broken",
            name="Broken",
            careers_url="https://example.com/jobs",
            ats_type="greenhouse",
            industry="tech",
            profiles=["tech"],
            ats_config={},
        )


def test_profile_name_must_fit_database_slug_contract():
    payload = {
        "threshold": 0.6,
        "strong_threshold": 0.8,
        "weights": {"title": 1.0},
        "title_terms": ["analyst"],
        "domain_terms": [],
        "skills": [],
    }
    with pytest.raises(ValidationError):
        ProfileConfig(name="contains spaces", **payload)
    with pytest.raises(ValidationError):
        ProfileConfig(name="x" * 41, **payload)
    with pytest.raises(ValidationError):
        ProfileConfig(name="", **payload)


def test_profile_weights_must_match_supported_scoring_dimensions():
    payload = {
        "name": "analytics",
        "threshold": 0.6,
        "strong_threshold": 0.8,
        "title_terms": ["analyst"],
        "domain_terms": [],
        "skills": [],
    }
    with pytest.raises(ValidationError):
        ProfileConfig(weights={"made_up": 1.0}, **payload)
    with pytest.raises(ValidationError):
        ProfileConfig(weights={"title": 1.1, "location": -0.1}, **payload)


def test_public_config_models_reject_misspelled_fields():
    with pytest.raises(ValidationError):
        ProfileConfig(
            name="analytics",
            weights={"title": 1.0},
            title_terms=["analyst"],
            domain_terms=[],
            skills=[],
            strong_treshold=0.8,
        )


@pytest.mark.asyncio
@respx.mock
async def test_ndx_snapshot_rejects_partial_response():
    respx.get(NASDAQ_100_URL).mock(
        return_value=httpx.Response(200, json={"data": {"data": {"rows": [{"symbol": "A"}]}}})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="preserving previous snapshot"):
            await fetch_ndx_constituents(client)
