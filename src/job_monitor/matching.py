from __future__ import annotations

from .config import ProfileConfig, SearchPreferences
from .models import (
    MatchResult,
    ParsedJob,
    RawJob,
    RemoteType,
    ResumeProfile,
    Seniority,
    VisaSupport,
)
from .resume import SKILL_ALIASES

REMOTE_TERMS = ["remote", "united states - remote", "remote us", "remote, us"]
VISA_SUPPORT_TERMS = [
    "will sponsor",
    "visa sponsorship is available",
    "sponsorship is available",
    "h-1b sponsorship",
    "h1b sponsorship",
    "tn sponsorship",
    "green card sponsorship",
]
VISA_REJECTION_TERMS = [
    "will not sponsor",
    "does not sponsor",
    "do not sponsor",
    "cannot sponsor",
    "unable to sponsor",
    "not able to sponsor",
    "sponsorship is not available",
    "without sponsorship",
    "now or in the future require sponsorship",
    "now or in future require sponsorship",
    "must be authorized to work in the united states without",
]
GREEN_CARD_ELIGIBLE_TERMS = [
    "green card",
    "green-card",
    "permanent resident",
    "lawful permanent resident",
    "u.s. person",
    "us person",
    "u.s. persons",
    "us persons",
]
CITIZENSHIP_ONLY_TERMS = [
    "citizenship required",
    "u.s. citizenship required",
    "us citizenship required",
    "united states citizenship required",
    "must be a u.s. citizen",
    "must be an u.s. citizen",
    "must be a us citizen",
    "must be an us citizen",
    "must be a united states citizen",
    "u.s. citizen only",
    "us citizen only",
    "united states citizen only",
    "only u.s. citizens",
    "only us citizens",
    "requires u.s. citizenship",
    "requires us citizenship",
]


def _contains(text: str, terms: list[str]) -> bool:
    lower = f" {text.lower()} "
    return any(term.lower() in lower for term in terms)


def _requires_citizenship(text: str) -> bool:
    if not _contains(text, ["u.s. citizen", "us citizen", "united states citizen", "citizenship"]):
        return False
    if _contains(text, GREEN_CARD_ELIGIBLE_TERMS):
        return False
    return _contains(text, CITIZENSHIP_ONLY_TERMS)


def parse_job(raw: RawJob) -> ParsedJob:
    title = raw.title.lower()
    text = f" {raw.title} {raw.location_raw} {raw.description_raw} ".lower()
    location_text = f" {raw.location_raw.lower()} "
    if _contains(title, ["intern", "entry level", "junior", "associate analyst"]):
        seniority = Seniority.ENTRY
    elif _contains(title, ["director", "vice president", "vp ", "head of"]):
        seniority = Seniority.DIRECTOR
    elif _contains(title, ["principal", "staff", "lead", "manager"]):
        seniority = Seniority.LEAD
    elif _contains(title, ["senior", " sr ", "sr."]):
        seniority = Seniority.SENIOR
    elif _contains(title, ["analyst", "engineer", "scientist", "developer"]):
        seniority = Seniority.MID
    else:
        seniority = Seniority.UNKNOWN

    if _contains(location_text, REMOTE_TERMS):
        remote_type = RemoteType.REMOTE
    elif "hybrid" in location_text:
        remote_type = RemoteType.HYBRID
    elif raw.location_raw:
        remote_type = RemoteType.ONSITE
    else:
        remote_type = RemoteType.UNKNOWN

    if _contains(title, ["analytics engineer", "data engineer"]):
        family = "analytics_engineering"
    elif "data scientist" in title:
        family = "data_science"
    elif _contains(title, ["bi analyst", "business intelligence", "bi developer"]):
        family = "business_intelligence"
    elif "analyst" in title:
        family = "data_analytics"
    else:
        family = "other"

    skills = {canonical for canonical, aliases in SKILL_ALIASES.items() if _contains(text, aliases)}
    citizenship = _requires_citizenship(text)
    clearance = _contains(text, ["security clearance", "secret clearance", "top secret", "ts/sci"])
    if _contains(text, VISA_REJECTION_TERMS):
        visa_support = VisaSupport.DOES_NOT_SUPPORT
    elif _contains(text, VISA_SUPPORT_TERMS):
        visa_support = VisaSupport.SUPPORTS
    else:
        visa_support = VisaSupport.UNKNOWN
    ambiguities = set()
    if remote_type == RemoteType.UNKNOWN:
        ambiguities.add("remote")
    if seniority == Seniority.UNKNOWN:
        ambiguities.add("seniority")
    if _contains(text, ["authorized to work", "sponsorship"]):
        ambiguities.add("visa")

    return ParsedJob(
        raw=raw,
        seniority=seniority,
        remote_type=remote_type,
        job_family=family,
        tech_keywords=skills,
        requires_citizenship=citizenship,
        requires_clearance=clearance,
        visa_support=visa_support,
        employment_type="contract"
        if "contract" in text
        else "full_time"
        if "full-time" in text
        else "unknown",
        ambiguities=ambiguities,
    )


def location_eligible(job: ParsedJob, preferences: SearchPreferences) -> bool:
    if job.remote_type == RemoteType.REMOTE:
        return preferences.include_remote
    if not preferences.location_terms:
        return job.remote_type != RemoteType.REMOTE
    return _contains(job.raw.location_raw, preferences.location_terms)


def _term_hits(text: str, terms: list[str]) -> set[str]:
    return {term.lower() for term in terms if _contains(text, [term])}


def _term_score(text: str, terms: list[str], target_hits: int = 5) -> tuple[float, set[str]]:
    hits = _term_hits(text, terms)
    return min(1.0, len(hits) / max(1, min(target_hits, len(terms)))), hits


def match_job(
    job: ParsedJob,
    profile: ProfileConfig,
    preferences: SearchPreferences,
    resume: ResumeProfile | None = None,
    *,
    visa_sponsorship_required: bool = False,
    company_visa_support: VisaSupport = VisaSupport.UNKNOWN,
) -> MatchResult:
    if (
        preferences.exclude_citizenship_required
        and job.requires_citizenship
        or preferences.exclude_clearance_required
        and job.requires_clearance
    ):
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="citizenship_or_clearance",
        )
    if visa_sponsorship_required and (
        job.visa_support == VisaSupport.DOES_NOT_SUPPORT
        or company_visa_support == VisaSupport.DOES_NOT_SUPPORT
    ):
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="visa_sponsorship",
        )
    if job.seniority in preferences.excluded_seniorities:
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="seniority",
        )
    if job.job_family == "other":
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="job_family",
        )
    if not location_eligible(job, preferences):
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="location",
        )

    title_text = job.raw.title.lower()
    title_desc = f"{job.raw.title} {job.raw.description_raw}".lower()
    title_score = 1.0 if any(term.lower() in title_text for term in profile.title_terms) else 0.0
    profile_domain_score = min(
        1.0, sum(term.lower() in title_desc for term in profile.domain_terms) / 3
    )
    desired_skills = {skill.lower() for skill in profile.skills}
    profile_skill_score = len({x.lower() for x in job.tech_keywords} & desired_skills) / max(
        1, min(5, len(desired_skills))
    )
    resume_hits: set[str] = set()
    resume_skill_score = 0.0
    resume_domain_score = 0.0
    if resume:
        resume_domain_score, resume_hits = _term_score(title_desc, resume.keywords)
        resume_skills = {skill.lower() for skill in resume.skills}
        if resume_skills:
            resume_skill_score = len({x.lower() for x in job.tech_keywords} & resume_skills) / max(
                1, min(5, len(resume_skills))
            )
    domain_score = max(profile_domain_score, resume_domain_score)
    skill_score = max(profile_skill_score, resume_skill_score)
    location_score = 1.0
    seniority_score = 1.0 if job.seniority in {Seniority.MID, Seniority.SENIOR} else 0.6

    dimensions = {
        "title": title_score,
        "domain": domain_score,
        "skills": min(skill_score, 1.0),
        "location": location_score,
        "seniority": seniority_score,
    }
    score = round(
        sum(dimensions.get(key, 0) * weight for key, weight in profile.weights.items()), 3
    )
    reasons = [f"{key}: {value:.0%}" for key, value in dimensions.items() if value >= 0.5]
    if resume_hits:
        reasons.append("resume: " + ", ".join(sorted(resume_hits)[:5]))
    gaps = []
    if visa_sponsorship_required and job.visa_support == VisaSupport.SUPPORTS:
        reasons.append("visa: sponsorship available")
    elif visa_sponsorship_required and company_visa_support in {
        VisaSupport.SUPPORTS,
        VisaSupport.LIKELY_SUPPORTS,
    }:
        reasons.append("visa: company likely supports sponsorship")
    elif visa_sponsorship_required and company_visa_support == VisaSupport.CASE_BY_CASE:
        gaps.append("visa sponsorship case-by-case")
    elif visa_sponsorship_required and job.visa_support == VisaSupport.UNKNOWN:
        gaps.append("visa sponsorship unknown")
    for label, terms in profile.gap_terms.items():
        if any(term.lower() in title_desc for term in terms):
            gaps.append(label)
    tier = (
        "strong"
        if score >= profile.strong_threshold
        else "match"
        if score >= profile.threshold
        else "below_threshold"
    )
    return MatchResult(
        profile=profile.name,
        score=score,
        eligible=score >= profile.threshold,
        tier=tier,
        reasons=reasons,
        gaps=gaps,
    )
