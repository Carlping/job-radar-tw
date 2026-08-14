from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_monitor.config import SearchPreferences, Settings, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import CandidateProfile, DegreeLevel, JobLevel, RawJob, Seniority
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


def candidate(**updates) -> CandidateProfile:
    values = {"years_experience": 7, "current_level": JobLevel.SENIOR, "max_level_reach": 2}
    values.update(updates)
    return CandidateProfile(**values)


def test_reach_buckets_and_filters():
    profile = PROFILES["tech"]
    target = match_job(
        parse_job(raw("Data Scientist")), profile, PREFERENCES, candidate=candidate()
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


def test_penalty_and_legacy_score_without_candidate():
    parsed = parse_job(raw("Data Scientist", "PhD required. Manage a team."))
    profile = PROFILES["tech"]
    legacy = match_job(parsed, profile, PREFERENCES)
    candidate_result = match_job(parsed, profile, PREFERENCES, candidate=candidate())
    assert legacy.fit == 0
    assert candidate_result.score < legacy.score
    assert candidate_result.bucket == "target"


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
