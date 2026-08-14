from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

import httpx
import typer

from .config import Settings, load_candidate, load_companies, load_preferences, load_profiles
from .ndx import fetch_ndx_constituents
from .notifier import TelegramNotifier, render_job_message
from .onboarding import (
    company_inventory,
    filter_companies,
    json_dump,
    load_source_candidates,
    promote_companies_in_config,
    render_candidates,
    render_inventory,
    render_verifications,
    verify_companies,
)
from .pipeline import RunReport, run_pipeline
from .resume import load_resume
from .schedule import scheduled_run_key
from .storage import Storage

app = typer.Typer(no_args_is_help=True, help="Job Radar TW official career-site monitor")
sources_app = typer.Typer(no_args_is_help=True, help="Inspect and onboard career-site sources")
app.add_typer(sources_app, name="sources")


def _load(selected: str | None = None):
    settings = Settings()
    companies = load_companies(settings.companies_config)
    profiles = load_profiles(settings.profiles_config)
    preferences = load_preferences(settings.preferences_config)
    candidate = load_candidate(settings.candidate_config)
    unknown_profiles = sorted(
        {
            profile
            for company in companies
            for profile in company.profiles
            if profile not in profiles
        }
    )
    if unknown_profiles:
        raise ValueError("companies reference unknown profiles: " + ", ".join(unknown_profiles))
    if selected:
        companies = [company for company in companies if company.slug == selected]
        if not companies:
            raise typer.BadParameter(f"Unknown company slug: {selected}")
    return settings, companies, profiles, preferences, candidate


def _print_report(report) -> None:
    payload = {"run_key": report.run_key, **report.stats(), "errors": report.errors}
    if report.skipped_reason:
        payload["skipped_reason"] = report.skipped_reason
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _selected_slugs(company: list[str] | None) -> set[str] | None:
    return set(company) if company else None


async def _send_telegram_test(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        notifier = TelegramNotifier(
            settings.telegram_bot_token or "",
            settings.telegram_chat_id or "",
            client,
        )
        await notifier.send("✅ Job Radar TW（職缺雷達）已連上這個 Telegram 對話。")


@app.command("run")
def run_command(
    company: str | None = typer.Option(None, help="Run only one company slug"),
    backfill: bool = typer.Option(
        False,
        help="Notify eligible existing matches that have not already been sent",
    ),
    scheduled: bool = typer.Option(
        False,
        help="Use the configured schedule window and daily idempotency key",
    ),
    run_key: str | None = typer.Option(None, help="Override idempotency key"),
) -> None:
    """Run the monitor and persist results."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings, companies, profiles, preferences, candidate = _load(company)
    if company and not companies[0].enabled:
        raise typer.BadParameter(
            f"{company} is disabled; verify and enable it before a persisted run"
        )
    if scheduled and run_key is None:
        run_key = scheduled_run_key(
            timezone=settings.monitor_timezone,
            hour=settings.monitor_hour,
            grace_hours=settings.schedule_grace_hours,
        )
        if run_key is None:
            report = RunReport(
                run_key="scheduled-outside-window", skipped_reason="outside_scheduled_window"
            )
            _print_report(report)
            raise typer.Exit()
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        raise typer.BadParameter(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required for monitor run notifications"
        )
    report = asyncio.run(
        run_pipeline(
            settings,
            companies,
            profiles,
            preferences,
            candidate,
            backfill=backfill,
            run_key=run_key,
        )
    )
    _print_report(report)
    if report.skipped_reason:
        raise typer.Exit()
    if any(item.get("company") == "telegram" for item in report.errors):
        raise typer.Exit(code=1)
    if not report.sources_succeeded and report.sources_attempted:
        raise typer.Exit(code=1)


@app.command("dry-run")
def dry_run_command(
    company: str | None = typer.Option(None, help="Run only one company slug"),
) -> None:
    """Fetch and score without database writes or Telegram messages."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings, companies, profiles, preferences, candidate = _load(company)
    if company:
        companies = [item.model_copy(update={"enabled": True}) for item in companies]
    report = asyncio.run(
        run_pipeline(settings, companies, profiles, preferences, candidate, dry_run=True)
    )
    _print_report(report)
    for item in report.dry_run_matches:
        typer.echo(
            "\n"
            + render_job_message(
                item.company_name,
                item.job,
                item.result,
                item.first_seen_at,
            )
        )
    if not report.sources_succeeded and report.sources_attempted:
        raise typer.Exit(code=1)


@app.command("validate-config")
def validate_config(
    strict: bool = typer.Option(
        False,
        help="Fail when an enabled source has not been live-verified",
    ),
) -> None:
    """Validate registry and matching profile files."""
    settings, companies, profiles, preferences, candidate = _load()
    resume = load_resume(
        settings.resume_path,
        settings.resume_text.get_secret_value() if settings.resume_text else None,
    )
    enabled = [company for company in companies if company.enabled]
    unverified = [company.slug for company in enabled if not company.source_verified]
    typer.echo(
        f"Valid companies: {len(companies)}; enabled: {len(enabled)}; profiles: {len(profiles)}"
    )
    typer.echo(
        f"Telegram configured: {'yes' if settings.telegram_bot_token and settings.telegram_chat_id else 'no'}"
    )
    typer.echo(
        f"Visa sponsorship required: {'yes' if settings.visa_sponsorship_required else 'no'}"
    )
    if candidate:
        typer.echo(
            f"Candidate: {candidate.years_experience} years; level {candidate.current_level.value}; "
            f"reach +{candidate.max_level_reach}"
        )
    else:
        typer.echo("Candidate: unset; no level/experience reasonableness check applied")
    typer.echo(
        f"Locations: {len(preferences.location_terms)} terms; "
        f"remote: {'included' if preferences.include_remote else 'excluded'}"
    )
    if resume:
        typer.echo(
            f"Resume terms: {len(resume.keywords)}; skills: {', '.join(resume.skills) or 'none'}"
        )
    if unverified:
        typer.echo("WARNING enabled but not live-verified: " + ", ".join(unverified))
        if strict:
            raise typer.Exit(code=1)
    if not enabled:
        raise typer.Exit(code=1)


@sources_app.command("list")
def list_sources(
    status: str = typer.Option("disabled", help="One of: disabled, enabled, all"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """List source registry entries by onboarding state."""
    settings, companies, _, _, _ = _load()
    try:
        selected = filter_companies(companies, status=status)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    items = company_inventory(selected)
    if json_output:
        typer.echo(json_dump(items))
    else:
        typer.echo(render_inventory(items))
        typer.echo(f"\nTotal: {len(items)} from {settings.companies_config}")


@sources_app.command("verify")
def verify_sources(
    company: Annotated[
        list[str] | None,
        typer.Option("--company", "-c", help="Company slug to verify; repeat for multiple slugs"),
    ] = None,
    status: str = typer.Option("disabled", help="One of: disabled, enabled, all"),
    min_jobs: int = typer.Option(1, min=0, help="Minimum fetched jobs required before --promote"),
    promote: bool = typer.Option(
        False, help="Enable and mark source_verified for passing disabled sources"
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Live-check official source endpoints, including disabled registry entries."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings, companies, _, _, _ = _load()
    try:
        selected = filter_companies(
            companies,
            status=status,
            selected_slugs=_selected_slugs(company),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if company:
        found = {item.slug for item in selected}
        missing = set(company) - found
        if missing:
            raise typer.BadParameter(
                "Unknown or status-filtered company slug(s): " + ", ".join(sorted(missing))
            )
    if not selected:
        typer.echo("No sources matched.")
        raise typer.Exit()

    results = asyncio.run(verify_companies(selected, settings, min_jobs=min_jobs))
    if json_output:
        typer.echo(json_dump([result.as_dict() for result in results]))
    else:
        typer.echo(render_verifications(results))

    if promote:
        ready = [result.slug for result in results if result.ready_to_enable]
        if not ready:
            typer.echo("\nNo sources met the promotion criteria.")
            return
        promoted = promote_companies_in_config(settings.companies_config, ready)
        typer.echo("\nPromoted: " + ", ".join(promoted))


@sources_app.command("candidates")
def list_candidates(
    category: str | None = typer.Option(None, help="Filter by candidate category"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """List seed companies and institutions that still need endpoint discovery."""
    settings = Settings()
    candidates = load_source_candidates(settings.source_candidates_config)
    if category:
        candidates = [item for item in candidates if item.get("category") == category]
    if json_output:
        typer.echo(json_dump(candidates))
    else:
        typer.echo(render_candidates(candidates))
        typer.echo(f"\nTotal: {len(candidates)} from {settings.source_candidates_config}")


@app.command("init-db")
def init_db() -> None:
    """Create the database schema and enable PostgreSQL row-level security."""
    settings = Settings()
    if not settings.database_url:
        raise typer.BadParameter("DATABASE_URL is required")
    storage = Storage(settings.database_url, create_schema=True)
    schema_issues = storage.schema_status()
    if schema_issues:
        raise typer.BadParameter("Database schema is incomplete: " + ", ".join(schema_issues))
    if storage.engine.dialect.name == "postgresql":
        typer.echo("Database schema initialized; PostgreSQL RLS enabled.")
    else:
        typer.echo("Database schema initialized.")


@app.command("telegram-test")
def telegram_test() -> None:
    """Send one Telegram message to verify the bot token and chat ID."""
    settings = Settings()
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        raise typer.BadParameter("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    try:
        asyncio.run(_send_telegram_test(settings))
    except Exception as exc:
        raise typer.BadParameter(
            f"Telegram check failed ({type(exc).__name__}); verify the bot token and chat ID"
        ) from None
    typer.echo("Telegram test message sent.")


@app.command("doctor")
def doctor(
    send_telegram: bool = typer.Option(
        False,
        "--send-telegram",
        help="Also send one Telegram test message",
    ),
) -> None:
    """Check runtime credentials, database connectivity, and schema."""
    settings, _, _, _, _ = _load()
    missing_secrets = [
        name
        for name, value in (
            ("DATABASE_URL", settings.database_url),
            ("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
            ("TELEGRAM_CHAT_ID", settings.telegram_chat_id),
        )
        if not value
    ]
    if missing_secrets:
        raise typer.BadParameter("Missing runtime settings: " + ", ".join(missing_secrets))
    try:
        schema_issues = Storage(settings.database_url or "").schema_status()
    except Exception as exc:
        raise typer.BadParameter(
            f"Database check failed ({type(exc).__name__}); verify DATABASE_URL"
        ) from None
    if schema_issues:
        raise typer.BadParameter(
            "Database schema is incomplete or unsafe; run monitor init-db. Issues: "
            + ", ".join(schema_issues)
        )
    typer.echo("Database connection and schema: ok")
    if send_telegram:
        try:
            asyncio.run(_send_telegram_test(settings))
        except Exception as exc:
            raise typer.BadParameter(
                f"Telegram check failed ({type(exc).__name__}); verify the bot token and chat ID"
            ) from None
        typer.echo("Telegram test message: sent")


@app.command("web")
def web_command(
    host: str = typer.Option("127.0.0.1", help="Host for the dashboard server"),
    port: int = typer.Option(8080, help="Port for the dashboard server"),
) -> None:
    """Run the local web dashboard."""
    settings = Settings()
    if not settings.database_url:
        raise typer.BadParameter("DATABASE_URL is required")
    import uvicorn

    uvicorn.run("job_monitor.web:app", host=host, port=port, reload=False)


@app.command("refresh-ndx")
def refresh_ndx() -> None:
    """Refresh the retained Nasdaq-100 membership snapshot."""
    settings = Settings()
    if not settings.database_url:
        raise typer.BadParameter("DATABASE_URL is required")

    async def refresh() -> tuple[list[dict], str]:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 JobRadarTW/0.1"},
        ) as client:
            return await fetch_ndx_constituents(client)

    rows, as_of = asyncio.run(refresh())
    Storage(settings.database_url).replace_ndx_snapshot(rows, as_of)
    typer.echo(f"Stored {len(rows)} Nasdaq-100 constituents as of {as_of}.")


if __name__ == "__main__":
    app()
