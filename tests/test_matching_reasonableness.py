import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_monitor.config import SearchPreferences, Settings, load_profiles
from job_monitor.llm import LLMEnricher
from job_monitor.matching import _level_fit, _years_fit, match_job, parse_job
from job_monitor.models import (
    CandidateProfile,
    DegreeLevel,
    JobLevel,
    MatchResult,
    RawJob,
    Seniority,
)
from job_monitor.notifier import render_job_message
from job_monitor.pipeline import _qualifies_for_immediate_notification

PROFILES = load_profiles(Path("config/profiles.yml"))
PREFERENCES = SearchPreferences(location_terms=[], include_remote=True, excluded_seniorities=set())


def raw(title: str, description: str = "") -> RawJob:
    return RawJob(
        source_company="acme",
        title=title,
        location_raw="Remote US",
        description_raw=description,
        url="https://example.com/job",
    )


@pytest.mark.parametrize(
    ("title", "level", "seniority"),
    [
        ("Junior Data Analyst", JobLevel.ENTRY, Seniority.ENTRY),
        ("Director of Data", JobLevel.DIRECTOR_PLUS, Seniority.DIRECTOR),
        ("Principal Data Scientist", JobLevel.PRINCIPAL, Seniority.LEAD),
        ("Staff Data Scientist", JobLevel.STAFF, Seniority.LEAD),
        ("Lead Data Scientist", JobLevel.LEAD, Seniority.LEAD),
        ("Senior Data Scientist", JobLevel.SENIOR, Seniority.SENIOR),
        ("Data Scientist", JobLevel.MID, Seniority.MID),
    ],
)
def test_job_level_and_seniority(title, level, seniority):
    parsed = parse_job(raw(title))
    assert parsed.level == level
    assert parsed.seniority == seniority


def test_requirement_extraction():
    parsed = parse_job(
        raw(
            "Senior Data Scientist",
            "Requires 8+ years of experience. Master's degree required. Lead a team.",
        )
    )
    assert parsed.required_years_min == 8
    assert parsed.degree_required == DegreeLevel.MASTER
    assert parsed.people_management

    assert (
        parse_job(
            raw("Data Scientist", "PhD preferred; 10 years of company history.")
        ).degree_required
        == DegreeLevel.NONE
    )
    assert (
        parse_job(raw("Data Scientist", "5-8 years experience required.")).required_years_min == 5
    )
    assert (
        parse_job(
            raw(
                "Data Scientist",
                "5+ years of experience required; 10+ years of experience preferred",
            )
        ).required_years_min
        == 5
    )
    assert (
        parse_job(raw("Data Scientist", "Graduated within the last 2 years.")).required_years_min
        is None
    )


def test_legacy_level_terms_do_not_add_hard_filters():
    profile = PROFILES["tech"]
    for title in [
        "Data Analyst, Office of the Chief Data Officer",
        "Data Analyst Fellowship Program",
    ]:
        result = match_job(parse_job(raw(title)), profile, SearchPreferences())
        assert result.filtered_reason != "seniority"


def candidate(**updates) -> CandidateProfile:
    values = {"years_experience": 7, "current_level": JobLevel.SENIOR}
    values.update(updates)
    return CandidateProfile(**values)


def test_reach_buckets_and_filters():
    profile = PROFILES["tech"]
    target = match_job(
        parse_job(raw("Data Scientist", "8+ years of experience")),
        profile,
        PREFERENCES,
        candidate=candidate(),
    )
    stretch = match_job(
        parse_job(raw("Staff Data Scientist", "8+ years of experience")),
        profile,
        PREFERENCES,
        candidate=candidate(),
    )
    assert target.bucket == "target"
    assert stretch.bucket == "stretch"
    assert stretch.reach == 0.451
    assert not match_job(
        parse_job(raw("Principal Data Scientist")),
        profile,
        PREFERENCES,
        candidate=candidate(max_level_reach=0),
    ).eligible
    assert (
        match_job(
            parse_job(raw("Principal Data Scientist")),
            profile,
            PREFERENCES,
            candidate=candidate(max_level_reach=0),
        ).filtered_reason
        == "level_reach"
    )
    unrealistic = match_job(
        parse_job(raw("Staff Data Scientist", "20 years of experience")),
        profile,
        PREFERENCES,
        candidate=candidate(),
    )
    assert unrealistic.filtered_reason == "reach"


def test_level_fit_boundaries_and_unknown():
    current = candidate()
    assert _level_fit(parse_job(raw("Data Scientist")), current)[0] == 0.925
    assert _level_fit(parse_job(raw("Senior Data Scientist")), current)[0] == 1.0
    assert _level_fit(parse_job(raw("Staff Data Scientist")), current)[0] == 0.55
    assert _level_fit(parse_job(raw("Principal Data Scientist")), current)[0] == pytest.approx(0.1)
    assert _level_fit(parse_job(raw("Mystery Role")), current)[0] == 0.7


def test_years_fit_boundaries():
    current = candidate()
    assert _years_fit(parse_job(raw("Data Scientist")), current) == 1.0
    assert _years_fit(parse_job(raw("Data Scientist", "7 years of experience")), current) == 1.0
    assert _years_fit(
        parse_job(raw("Data Scientist", "8 years of experience")), current
    ) == pytest.approx(0.82)
    assert _years_fit(parse_job(raw("Data Scientist", "20 years of experience")), current) == 0.0


def test_ndx_company_scale_adjustment_changes_reach():
    profile = PROFILES["tech"]
    regular = match_job(
        parse_job(raw("Staff Data Scientist")),
        profile,
        PREFERENCES,
        candidate=candidate(),
    )
    adjusted = match_job(
        parse_job(raw("Staff Data Scientist")),
        profile,
        PREFERENCES,
        candidate=candidate(company_scale="small"),
        company_ndx_member=True,
    )
    assert regular.reach == 0.55
    assert adjusted.reach == 0.325
    assert regular.bucket == "stretch"
    assert adjusted.filtered_reason == "reach"


def test_penalty_and_legacy_score_without_candidate():
    parsed = parse_job(raw("Data Scientist", "PhD required. Manage a team."))
    profile = PROFILES["tech"]
    legacy = match_job(parsed, profile, PREFERENCES)
    candidate_result = match_job(parsed, profile, PREFERENCES, candidate=candidate())
    assert legacy.fit == 0.556
    assert candidate_result.score < legacy.score
    assert candidate_result.bucket == "target"


def test_penalty_is_capped_at_half():
    unpenalized = match_job(
        parse_job(raw("Data Scientist")),
        PROFILES["tech"],
        PREFERENCES,
        candidate=candidate(),
    )
    result = match_job(
        parse_job(raw("Data Scientist", "PhD required. Manage a team.")),
        PROFILES["tech"],
        PREFERENCES,
        candidate=candidate(),
    )
    assert result.score == 0.356
    assert result.score >= round(unpenalized.score * 0.5, 3)


@pytest.mark.parametrize(
    ("title", "description", "score", "tier", "eligible"),
    [
        ("Data Scientist", "SQL and Python.", 0.72, "match", True),
        ("Senior Data Analyst", "SQL and Tableau.", 0.72, "match", True),
        ("Data Scientist", "Cloud, SQL, Python, and machine learning.", 0.787, "strong", True),
    ],
)
def test_legacy_scores_are_unchanged_without_candidate(title, description, score, tier, eligible):
    result = match_job(
        parse_job(raw(title, description)),
        PROFILES["tech"],
        PREFERENCES,
    )
    assert result.score == score
    assert result.tier == tier
    assert result.eligible is eligible


def test_stretch_is_not_immediate():
    result = match_job(
        parse_job(raw("Staff Data Scientist", "8+ years of experience.")),
        PROFILES["tech"],
        PREFERENCES,
        candidate=candidate(),
    )
    assert not _qualifies_for_immediate_notification(
        parse_job(raw("Staff Data Scientist")), result, datetime.now(UTC), Settings(), is_new=True
    )


def test_stretch_message_uses_challenge_badge():
    result = MatchResult(
        profile="tech",
        score=0.7,
        eligible=True,
        tier="strong",
        bucket="stretch",
    )
    message = render_job_message(
        "Acme", parse_job(raw("Staff Data Scientist")), result, datetime.now(UTC)
    )
    assert "🪜 延伸挑戰" in message
    assert "🔥 強烈推薦" not in message


@pytest.mark.asyncio
async def test_llm_enrichment_updates_level_and_reach():
    class FakeResponses:
        async def create(self, **kwargs):
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "seniority": "lead",
                        "remote_type": "remote",
                        "requires_citizenship": False,
                        "requires_clearance": False,
                        "domain_terms": [],
                    }
                )
            )

    enricher = object.__new__(LLMEnricher)
    enricher.client = SimpleNamespace(responses=FakeResponses())
    enricher.model = "test"
    job = parse_job(raw("Mystery Data Scientist"))
    job.level = JobLevel.UNKNOWN
    job.seniority = Seniority.UNKNOWN
    before = match_job(job, PROFILES["tech"], PREFERENCES, candidate=candidate())

    enriched = await enricher.enrich(job)
    after = match_job(enriched, PROFILES["tech"], PREFERENCES, candidate=candidate())

    assert enriched.seniority == Seniority.LEAD
    assert enriched.level == JobLevel.LEAD
    assert before.reach == 0.7
    assert after.reach == 0.55
