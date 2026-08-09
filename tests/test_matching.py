from pathlib import Path

from job_monitor.config import SearchPreferences, load_profiles
from job_monitor.matching import match_job, parse_job
from job_monitor.models import ProfileName, RawJob, Seniority, VisaSupport
from job_monitor.resume import build_resume_profile, load_resume

PROFILES = load_profiles(Path("config/profiles.yml"))
PREFERENCES = SearchPreferences(
    location_terms=[
        "Phoenix",
        "New York",
        "Washington",
        "Cambridge",
        "San Diego",
        "Los Angeles",
        "Bellevue",
    ],
    include_remote=False,
)


def job(title="Senior Healthcare Data Analyst", location="Phoenix, AZ", description=""):
    return RawJob(
        source_company="payer",
        external_job_id="1",
        title=title,
        location_raw=location,
        description_raw=description,
        url="https://example.com/jobs/1",
    )


def test_healthcare_resume_anchor_scores_as_match():
    parsed = parse_job(
        job(
            description="Analyze Medicaid claims, eligibility, utilization and CMS data using SQL, Python, Tableau and dbt."
        )
    )
    result = match_job(parsed, PROFILES[ProfileName.HEALTHCARE], PREFERENCES)
    assert result.eligible
    assert result.score >= 0.78


def test_citizenship_is_hard_filter():
    parsed = parse_job(
        job(description="Must be a U.S. citizen and hold a security clearance. SQL required.")
    )
    result = match_job(parsed, PROFILES[ProfileName.HEALTHCARE], PREFERENCES)
    assert not result.eligible
    assert result.filtered_reason == "citizenship_or_clearance"


def test_citizen_only_role_is_filtered_for_green_card_profile():
    parsed = parse_job(
        job(
            description="Must be a U.S. citizen. Healthcare analytics using SQL, Python, Tableau and dbt."
        )
    )
    result = match_job(parsed, PROFILES[ProfileName.HEALTHCARE], PREFERENCES)
    assert result.filtered_reason == "citizenship_or_clearance"


def test_citizen_or_permanent_resident_role_is_allowed():
    parsed = parse_job(
        job(
            description=(
                "Must be a U.S. citizen or lawful permanent resident. "
                "Healthcare analytics using SQL, Python, Tableau and dbt."
            )
        )
    )
    result = match_job(parsed, PROFILES[ProfileName.HEALTHCARE], PREFERENCES)
    assert result.eligible


def test_out_of_scope_location_is_filtered():
    parsed = parse_job(
        job(location="Chicago, IL", description="Onsite Medicaid claims role using SQL.")
    )
    result = match_job(parsed, PROFILES[ProfileName.HEALTHCARE], PREFERENCES)
    assert result.filtered_reason == "location"


def test_hedis_gap_is_exposed():
    parsed = parse_job(
        job(
            description="HEDIS certification required. Medicaid claims analytics with SQL and Tableau."
        )
    )
    result = match_job(parsed, PROFILES[ProfileName.HEALTHCARE], PREFERENCES)
    assert "formal HEDIS experience" in result.gaps


def test_foreign_location_is_not_made_remote_by_description():
    parsed = parse_job(
        job(
            title="Senior Data Analyst",
            location="Hyderabad, India",
            description="Our US team works remotely. SQL and Tableau.",
        )
    )
    result = match_job(parsed, PROFILES[ProfileName.TECH], PREFERENCES)
    assert result.filtered_reason == "location"


def test_non_us_remote_location_is_filtered():
    parsed = parse_job(
        job(title="Senior Data Analyst", location="Remote Canada", description="SQL and Tableau.")
    )
    result = match_job(parsed, PROFILES[ProfileName.TECH], PREFERENCES)
    assert result.filtered_reason == "location"


def test_remote_only_location_is_filtered():
    parsed = parse_job(
        job(title="Senior Data Analyst", location="Remote US", description="SQL and Tableau.")
    )
    result = match_job(parsed, PROFILES[ProfileName.TECH], PREFERENCES)
    assert result.filtered_reason == "location"


def test_target_metros_are_location_eligible():
    for location in [
        "New York, NY",
        "Washington, DC",
        "Cambridge, MA",
        "San Diego, CA",
        "Los Angeles, CA",
        "Bellevue, WA",
    ]:
        parsed = parse_job(
            job(title="Senior Data Analyst", location=location, description="SQL and Tableau.")
        )
        result = match_job(parsed, PROFILES[ProfileName.TECH], PREFERENCES)
        assert result.filtered_reason != "location"


def test_non_analytics_role_is_filtered_even_with_data_terms():
    parsed = parse_job(
        job(
            title="Lead Software Engineer - Data Platform",
            location="Remote",
            description="SQL data platform and cloud infrastructure.",
        )
    )
    result = match_job(parsed, PROFILES[ProfileName.TECH], PREFERENCES)
    assert result.filtered_reason == "job_family"


def test_custom_location_terms_replace_builtin_location_assumptions():
    preferences = SearchPreferences(location_terms=["Chicago"], include_remote=False)
    chicago = parse_job(
        job(
            location="Chicago, IL",
            description="Healthcare analytics using SQL, Python, Tableau and dbt.",
        )
    )
    phoenix = parse_job(
        job(
            location="Phoenix, AZ",
            description="Healthcare analytics using SQL, Python, Tableau and dbt.",
        )
    )

    assert match_job(chicago, PROFILES[ProfileName.HEALTHCARE], preferences).eligible
    assert (
        match_job(phoenix, PROFILES[ProfileName.HEALTHCARE], preferences).filtered_reason
        == "location"
    )


def test_remote_preference_can_include_or_exclude_remote_jobs():
    parsed = parse_job(
        job(
            location="Remote - Chicago, IL",
            description="Healthcare analytics using SQL, Python, Tableau and dbt.",
        )
    )
    included = SearchPreferences(location_terms=["Chicago"], include_remote=True)
    excluded = SearchPreferences(location_terms=["Chicago"], include_remote=False)

    assert match_job(parsed, PROFILES[ProfileName.HEALTHCARE], included).eligible
    assert (
        match_job(parsed, PROFILES[ProfileName.HEALTHCARE], excluded).filtered_reason == "location"
    )


def test_hard_filters_are_controlled_by_preferences():
    parsed = parse_job(
        job(
            description=(
                "Must be a U.S. citizen and hold a security clearance. "
                "Healthcare analytics using SQL, Python, Tableau and dbt."
            )
        )
    )
    permissive = SearchPreferences(
        location_terms=["Phoenix"],
        include_remote=False,
        exclude_citizenship_required=False,
        exclude_clearance_required=False,
        excluded_seniorities=set(),
    )

    assert match_job(parsed, PROFILES[ProfileName.HEALTHCARE], permissive).eligible

    seniority_filter = permissive.model_copy(update={"excluded_seniorities": {Seniority.SENIOR}})
    result = match_job(parsed, PROFILES[ProfileName.HEALTHCARE], seniority_filter)
    assert result.filtered_reason == "seniority"


def test_match_result_preserves_arbitrary_profile_slug():
    profile = PROFILES[ProfileName.TECH].model_copy(update={"name": "growth-analytics"})
    parsed = parse_job(
        job(
            title="Senior Data Analyst",
            description="Product analytics using SQL, Python, Tableau and dbt.",
        )
    )

    result = match_job(parsed, profile, PREFERENCES)

    assert result.eligible
    assert result.profile == "growth-analytics"


def test_visa_sponsorship_rejection_is_filtered_when_required():
    parsed = parse_job(
        job(
            description=(
                "Healthcare analytics role using SQL, Python, Tableau and dbt. "
                "We are unable to sponsor applicants for work visas now or in the future."
            )
        )
    )

    result = match_job(
        parsed,
        PROFILES[ProfileName.HEALTHCARE],
        PREFERENCES,
        visa_sponsorship_required=True,
    )

    assert not result.eligible
    assert result.filtered_reason == "visa_sponsorship"


def test_unknown_visa_support_is_exposed_as_gap_when_required():
    parsed = parse_job(
        job(description="Analyze Medicaid claims using SQL, Python, Tableau and dbt.")
    )

    result = match_job(
        parsed,
        PROFILES[ProfileName.HEALTHCARE],
        PREFERENCES,
        visa_sponsorship_required=True,
    )

    assert result.eligible
    assert "visa sponsorship unknown" in result.gaps


def test_company_level_visa_support_is_exposed_as_reason():
    parsed = parse_job(
        job(description="Analyze Medicaid claims using SQL, Python, Tableau and dbt.")
    )

    result = match_job(
        parsed,
        PROFILES[ProfileName.HEALTHCARE],
        PREFERENCES,
        visa_sponsorship_required=True,
        company_visa_support=VisaSupport.LIKELY_SUPPORTS,
    )

    assert result.eligible
    assert "visa: company likely supports sponsorship" in result.reasons
    assert "visa sponsorship unknown" not in result.gaps


def test_resume_terms_can_lift_relevant_job_score():
    resume = build_resume_profile(
        """
        Analytics resume
        Snowflake, Looker, experimentation, customer segmentation, retention analytics.
        Built product funnels and dashboards for lifecycle marketing teams.
        """
    )
    parsed = parse_job(
        job(
            title="Business Analyst",
            description="Own Snowflake models, Looker dashboards, experimentation reads and customer segmentation.",
        )
    )

    without_resume = match_job(parsed, PROFILES[ProfileName.TECH], PREFERENCES)
    with_resume = match_job(parsed, PROFILES[ProfileName.TECH], PREFERENCES, resume)

    assert without_resume.score < PROFILES[ProfileName.TECH].threshold
    assert with_resume.eligible
    assert with_resume.score > without_resume.score
    assert any(reason.startswith("resume:") for reason in with_resume.reasons)


def test_load_resume_extracts_dynamic_terms_and_known_skills(tmp_path):
    resume_path = tmp_path / "resume.md"
    resume_path.write_text(
        "Python and SQL analytics for Medicaid claims operations.", encoding="utf-8"
    )

    resume = load_resume(resume_path)

    assert resume is not None
    assert resume.source_path == str(resume_path)
    assert {"python", "sql"} <= set(resume.skills)
    assert "medicaid" in resume.keywords


def test_load_resume_accepts_inline_resume_text():
    resume = load_resume(None, "Python and SQL analytics for Medicaid claims operations.")

    assert resume is not None
    assert resume.source_path == "RESUME_TEXT"
    assert {"python", "sql"} <= set(resume.skills)
