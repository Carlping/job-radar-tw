from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    bindparam,
    case,
    create_engine,
    delete,
    exists,
    func,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .models import CompanyConfig, MatchResult, RawJob

logger = logging.getLogger(__name__)

metadata = MetaData()

companies = Table(
    "companies",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("slug", String(120), nullable=False),
    Column("name", String(255), nullable=False),
    Column("careers_url", Text, nullable=False),
    Column("ats_type", String(50), nullable=False),
    Column("industry", String(80), nullable=False),
    Column("profiles", JSON, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("source_verified", Boolean, nullable=False, default=False),
    Column("config", JSON, nullable=False),
    Column("consecutive_failures", Integer, nullable=False, default=0),
    Column("last_success_at", DateTime(timezone=True)),
    Column("baseline_completed", Boolean, nullable=False, default=False),
    UniqueConstraint("slug", name="uq_companies_slug"),
)

source_runs = Table(
    "source_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("run_key", String(100), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("status", String(30), nullable=False),
    Column("stats", JSON, nullable=False, default=dict),
    Column("errors", JSON, nullable=False, default=list),
    UniqueConstraint("run_key", name="uq_source_runs_run_key"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("company_id", String(36), ForeignKey("companies.id"), nullable=False),
    Column("external_job_id", String(255), nullable=False),
    Column("title", Text, nullable=False),
    Column("location_raw", Text, nullable=False),
    Column("description_raw", Text, nullable=False),
    Column("canonical_url", Text, nullable=False),
    Column("source_posted_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_run_id", String(36), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("status", String(20), nullable=False, default="active"),
    Column("missing_count", Integer, nullable=False, default=0),
    UniqueConstraint("company_id", "external_job_id", name="uq_job_company_external"),
)

job_versions = Table(
    "job_versions",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), ForeignKey("jobs.id"), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("job_id", "content_hash", name="uq_job_version_hash"),
)

match_results = Table(
    "match_results",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), ForeignKey("jobs.id"), nullable=False),
    Column("profile", String(40), nullable=False),
    Column("profile_version", String(30), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("score", Float, nullable=False),
    Column("eligible", Boolean, nullable=False),
    Column("tier", String(30), nullable=False),
    Column("details", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "job_id", "profile", "profile_version", "content_hash", name="uq_match_version"
    ),
)

notifications = Table(
    "notifications",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), ForeignKey("jobs.id"), nullable=False),
    Column("profile", String(40), nullable=False),
    Column("channel", String(30), nullable=False),
    Column("version_hash", String(64), nullable=False),
    Column("sent_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("job_id", "profile", "channel", "version_hash", name="uq_notification_dedupe"),
)

notification_outbox = Table(
    "notification_outbox",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), ForeignKey("jobs.id"), nullable=False),
    Column("profile", String(40), nullable=False),
    Column("channel", String(30), nullable=False, default="telegram"),
    Column("version_hash", String(64), nullable=False),
    Column("score", Float, nullable=False),
    Column("message", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("claim_token", String(36)),
    Column("claimed_by_run_id", String(36)),
    Column("claimed_at", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("last_error", Text),
    Column("next_attempt_at", DateTime(timezone=True)),
    UniqueConstraint(
        "job_id",
        "profile",
        "channel",
        "version_hash",
        name="uq_notification_outbox_dedupe",
    ),
)

ndx_constituents = Table(
    "ndx_constituents",
    metadata,
    Column("symbol", String(20), primary_key=True),
    Column("company_name", String(255), nullable=False),
    Column("as_of_date", String(10), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("payload", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

applications = Table(
    "applications",
    metadata,
    Column("job_id", String(36), ForeignKey("jobs.id"), primary_key=True),
    Column("stage", String(30), nullable=False, default="recommended"),
    Column("notes", Text),
    Column("first_saved_at", DateTime(timezone=True)),
    Column("first_applied_at", DateTime(timezone=True)),
    Column("first_interview_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Index("ix_jobs_status", jobs.c.status)
Index("ix_jobs_first_seen", jobs.c.first_seen_at.desc())
Index(
    "ix_notification_outbox_pending",
    notification_outbox.c.channel,
    notification_outbox.c.claim_token,
    notification_outbox.c.score.desc(),
    notification_outbox.c.created_at,
)

APPLICATION_STAGES = {
    "recommended",
    "saved",
    "applied",
    "interview",
    "offer",
    "rejected",
    "archived",
}

PERSIST_CHUNK_SIZE = 200


def _handoff_rank(item: Mapping[str, Any]) -> tuple[float, str]:
    return (-float(item["score"]), str(item["profile"]))


def _handoff_order(item: Mapping[str, Any]) -> tuple[float, str, str]:
    return (-float(item["score"]), str(item["company_slug"]), str(item["job_id"]))


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    reclaimed: bool


@dataclass(frozen=True)
class JobPlan:
    job_id: str
    is_new: bool
    changed: bool
    first_seen_at: datetime
    previous_content_hash: str | None


@dataclass(frozen=True)
class JobIndexRow:
    job_id: str
    content_hash: str
    first_seen_at: datetime


@dataclass(frozen=True)
class MatchDecision:
    profile_version: str
    result: MatchResult
    notification_message: str | None = None


@dataclass(frozen=True)
class JobPersistResult:
    job_id: str
    is_new: bool
    changed: bool
    first_seen_at: datetime
    notifications_enqueued: int


class Storage:
    def __init__(self, database_url: str, create_schema: bool = False):
        self.engine: Engine = create_engine(
            normalize_database_url(database_url), pool_pre_ping=True
        )
        if create_schema:
            metadata.create_all(self.engine)
            self._upgrade_postgres_schema()
            self._enable_postgres_rls()

    def _upgrade_postgres_schema(self) -> None:
        """Apply the small idempotent upgrades needed by pre-template databases."""
        if self.engine.dialect.name != "postgresql":
            return
        statements = (
            "ALTER TABLE companies ADD COLUMN IF NOT EXISTS "
            "baseline_completed boolean NOT NULL DEFAULT false",
            "ALTER TABLE notification_outbox ADD COLUMN IF NOT EXISTS claim_token varchar(36)",
            "ALTER TABLE notification_outbox ADD COLUMN IF NOT EXISTS "
            "claimed_by_run_id varchar(36)",
            "ALTER TABLE notification_outbox ADD COLUMN IF NOT EXISTS claimed_at timestamptz",
            "ALTER TABLE notification_outbox ADD COLUMN IF NOT EXISTS "
            "attempt_count integer NOT NULL DEFAULT 0",
            "ALTER TABLE notification_outbox ADD COLUMN IF NOT EXISTS last_error text",
            "ALTER TABLE notification_outbox ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz",
            "CREATE INDEX IF NOT EXISTS ix_notification_outbox_pending "
            "ON notification_outbox(channel, claim_token, score DESC, created_at)",
            "UPDATE companies AS company SET baseline_completed = true "
            "WHERE baseline_completed = false AND EXISTS "
            "(SELECT 1 FROM jobs WHERE jobs.company_id = company.id)",
        )
        with self.engine.begin() as conn:
            for statement in statements:
                conn.exec_driver_sql(statement)

    def _enable_postgres_rls(self) -> None:
        if self.engine.dialect.name != "postgresql":
            return
        with self.engine.begin() as conn:
            for table_name in metadata.tables:
                conn.exec_driver_sql(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')

    def schema_status(self) -> list[str]:
        inspector = inspect(self.engine)
        with self.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        existing = set(inspector.get_table_names())
        issues = [f"missing table: {name}" for name in sorted(set(metadata.tables) - existing)]
        for table_name, table in metadata.tables.items():
            if table_name not in existing:
                continue
            inspected_columns = {
                column["name"]: column for column in inspector.get_columns(table_name)
            }
            actual_columns = set(inspected_columns)
            for column_name in sorted(set(table.columns.keys()) - actual_columns):
                issues.append(f"missing column: {table_name}.{column_name}")
            for column in table.columns:
                inspected = inspected_columns.get(column.name)
                if inspected is not None and bool(inspected["nullable"]) != bool(column.nullable):
                    issues.append(f"wrong nullability: {table_name}.{column.name}")

            expected_pk = {column.name for column in table.primary_key.columns}
            actual_pk = set(
                inspector.get_pk_constraint(table_name).get("constrained_columns") or []
            )
            if expected_pk != actual_pk:
                issues.append(f"wrong primary key: {table_name}")

            expected_foreign_keys = {
                (foreign_key.parent.name, foreign_key.column.table.name, foreign_key.column.name)
                for foreign_key in table.foreign_keys
            }
            actual_foreign_keys: set[tuple[str, str, str]] = set()
            for foreign_key in inspector.get_foreign_keys(table_name):
                referred_table = foreign_key.get("referred_table")
                for local, remote in zip(
                    foreign_key.get("constrained_columns") or [],
                    foreign_key.get("referred_columns") or [],
                    strict=True,
                ):
                    actual_foreign_keys.add((local, referred_table, remote))
            for local, referred_table, remote in sorted(
                expected_foreign_keys - actual_foreign_keys
            ):
                issues.append(
                    f"missing foreign key: {table_name}.{local} -> {referred_table}.{remote}"
                )

            actual_indexes = {
                item["name"] for item in inspector.get_indexes(table_name) if item.get("name")
            }
            for index_name in sorted(
                {index.name for index in table.indexes if index.name} - actual_indexes
            ):
                issues.append(f"missing index: {index_name}")
            actual_unique_columns = {
                tuple(item.get("column_names") or [])
                for item in inspector.get_unique_constraints(table_name)
            }
            expected_unique = {
                tuple(column.name for column in constraint.columns): constraint.name
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint) and constraint.name
            }
            for columns, constraint_name in sorted(expected_unique.items()):
                if columns not in actual_unique_columns:
                    issues.append(f"missing constraint: {constraint_name}")

        if self.engine.dialect.name == "postgresql":
            with self.engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    """
                    SELECT c.relname, c.relrowsecurity
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema() AND c.relkind = 'r'
                    """
                ).all()
            rls = {name: enabled for name, enabled in rows}
            for table_name in sorted(set(metadata.tables) & existing):
                if not rls.get(table_name, False):
                    issues.append(f"RLS disabled: {table_name}")
            with self.engine.connect() as conn:
                policies = conn.exec_driver_sql(
                    """
                    SELECT tablename, policyname, roles::text
                    FROM pg_policies
                    WHERE schemaname = current_schema()
                      AND roles && ARRAY['public', 'anon', 'authenticated']::name[]
                    """
                ).all()
            protected_tables = set(metadata.tables) & existing
            for table_name, policy_name, roles in policies:
                if table_name in protected_tables:
                    issues.append(
                        f"unexpected RLS policy: {table_name}.{policy_name} roles={roles}"
                    )
        return issues

    def sync_company(self, company: CompanyConfig) -> str:
        payload = {
            "name": company.name,
            "careers_url": str(company.careers_url),
            "ats_type": company.ats_type.value,
            "industry": company.industry,
            "profiles": [str(profile) for profile in company.profiles],
            "priority": company.priority,
            "enabled": company.enabled,
            "source_verified": company.source_verified,
            "config": {
                **company.ats_config,
                "visa_support": company.visa_support.value,
                "visa_notes": company.visa_notes,
            },
        }
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(companies.c.id).where(companies.c.slug == company.slug)
            ).scalar_one_or_none()
            if existing:
                conn.execute(update(companies).where(companies.c.id == existing).values(**payload))
                return existing
            company_id = str(uuid.uuid4())
            conn.execute(
                insert(companies).values(
                    id=company_id,
                    slug=company.slug,
                    consecutive_failures=0,
                    baseline_completed=False,
                    **payload,
                )
            )
            return company_id

    def start_run(self, run_key: str, *, stale_after_minutes: int = 55) -> str | None:
        claim = self.claim_run(run_key, stale_after_minutes=stale_after_minutes)
        return claim.run_id if claim else None

    def claim_run(self, run_key: str, *, stale_after_minutes: int = 55) -> RunClaim | None:
        now = datetime.now(UTC)
        new_run_id = str(uuid.uuid4())
        try:
            with self.engine.begin() as conn:
                existing = (
                    conn.execute(
                        select(source_runs)
                        .where(source_runs.c.run_key == run_key)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    conn.execute(
                        insert(source_runs).values(
                            id=new_run_id,
                            run_key=run_key,
                            started_at=now,
                            status="running",
                            stats={},
                            errors=[],
                        )
                    )
                    return RunClaim(new_run_id, reclaimed=False)

                if existing["status"] == "success":
                    return None
                started_at = existing["started_at"]
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                stale = now - started_at >= timedelta(minutes=stale_after_minutes)
                if existing["status"] == "running" and not stale:
                    return None

                claimed = conn.execute(
                    update(source_runs)
                    .where(
                        source_runs.c.id == existing["id"],
                        source_runs.c.status == existing["status"],
                        source_runs.c.started_at == existing["started_at"],
                    )
                    .values(
                        id=new_run_id,
                        run_key=run_key,
                        started_at=now,
                        finished_at=None,
                        status="running",
                        stats={},
                        errors=[],
                    )
                )
                return RunClaim(new_run_id, reclaimed=True) if claimed.rowcount == 1 else None
        except IntegrityError:
            return None

    def assert_active_run(self, run_id: str) -> None:
        with self.engine.begin() as conn:
            self._assert_active_run(conn, run_id)

    @staticmethod
    def _assert_active_run(conn, run_id: str) -> None:
        active = conn.execute(
            select(source_runs.c.id)
            .where(
                source_runs.c.id == run_id,
                source_runs.c.status == "running",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if active is None:
            raise RuntimeError("run claim is no longer active")

    def finish_run(
        self,
        run_id: str,
        stats: dict[str, int],
        errors: list[dict[str, str]],
    ) -> bool:
        status = (
            "success"
            if not errors
            else "partial"
            if stats.get("sources_succeeded", 0)
            else "failed"
        )
        with self.engine.begin() as conn:
            finished = conn.execute(
                update(source_runs)
                .where(source_runs.c.id == run_id, source_runs.c.status == "running")
                .values(finished_at=datetime.now(UTC), status=status, stats=stats, errors=errors)
            )
            return finished.rowcount == 1

    def has_jobs(self, company_id: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    select(func.count()).select_from(jobs).where(jobs.c.company_id == company_id)
                ).scalar_one()
            )

    def is_baseline_completed(self, company_id: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    select(companies.c.baseline_completed).where(companies.c.id == company_id)
                ).scalar_one()
            )

    @staticmethod
    def _plan_from_row(
        raw: RawJob,
        row: JobIndexRow | Mapping[str, Any] | None,
        now: datetime,
    ) -> JobPlan:
        if row is None:
            return JobPlan(
                job_id=str(uuid.uuid4()),
                is_new=True,
                changed=True,
                first_seen_at=now,
                previous_content_hash=None,
            )
        if isinstance(row, JobIndexRow):
            job_id = row.job_id
            content_hash = row.content_hash
            first_seen_at = row.first_seen_at
        else:
            job_id = row["job_id"]
            content_hash = row["content_hash"]
            first_seen_at = row["first_seen_at"]
        return JobPlan(
            job_id=job_id,
            is_new=False,
            changed=content_hash != raw.content_hash,
            first_seen_at=first_seen_at,
            previous_content_hash=content_hash,
        )

    def prefetch_job_index(self, company_id: str) -> dict[str, JobIndexRow]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    jobs.c.external_job_id,
                    jobs.c.id,
                    jobs.c.content_hash,
                    jobs.c.first_seen_at,
                ).where(jobs.c.company_id == company_id)
            ).mappings()
            return {
                row["external_job_id"]: JobIndexRow(
                    job_id=row["id"],
                    content_hash=row["content_hash"],
                    first_seen_at=row["first_seen_at"],
                )
                for row in rows
            }

    def plan_job_from_index(self, raw: RawJob, row: JobIndexRow | None) -> JobPlan:
        return self._plan_from_row(raw, row, datetime.now(UTC))

    def plan_job(self, company_id: str, raw: RawJob) -> JobPlan:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(
                        jobs.c.id.label("job_id"),
                        jobs.c.content_hash,
                        jobs.c.first_seen_at,
                    ).where(
                        jobs.c.company_id == company_id,
                        jobs.c.external_job_id == raw.stable_external_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._plan_from_row(raw, row, datetime.now(UTC))

    @staticmethod
    def _validate_job_plan(
        plan: JobPlan,
        raw: RawJob,
        row: Mapping[str, Any] | None,
    ) -> None:
        if plan.is_new:
            if row is not None or plan.previous_content_hash is not None:
                raise RuntimeError("job changed while its match decision was prepared")
            return
        if (
            row is None
            or row["id"] != plan.job_id
            or row["content_hash"] != plan.previous_content_hash
        ):
            raise RuntimeError("job changed while its match decision was prepared")
        if plan.changed != (row["content_hash"] != raw.content_hash):
            raise RuntimeError("job change state no longer matches its prepared decision")

    def persist_job_decisions(
        self,
        company_id: str,
        run_id: str,
        raw: RawJob,
        plan: JobPlan,
        decisions: list[MatchDecision],
    ) -> JobPersistResult:
        now = datetime.now(UTC)
        external_id = raw.stable_external_id
        with self.engine.begin() as conn:
            self._assert_active_run(conn, run_id)
            row = (
                conn.execute(
                    select(jobs)
                    .where(
                        jobs.c.company_id == company_id,
                        jobs.c.external_job_id == external_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            self._validate_job_plan(plan, raw, row)
            if plan.is_new:
                conn.execute(
                    insert(jobs).values(
                        id=plan.job_id,
                        company_id=company_id,
                        external_job_id=external_id,
                        title=raw.title,
                        location_raw=raw.location_raw,
                        description_raw=raw.description_raw,
                        canonical_url=raw.canonical_url,
                        source_posted_at=raw.posted_at,
                        first_seen_at=plan.first_seen_at,
                        last_seen_at=now,
                        last_seen_run_id=run_id,
                        content_hash=raw.content_hash,
                        status="active",
                        missing_count=0,
                    )
                )
            else:
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == plan.job_id)
                    .values(
                        title=raw.title,
                        location_raw=raw.location_raw,
                        description_raw=raw.description_raw,
                        canonical_url=raw.canonical_url,
                        source_posted_at=raw.posted_at,
                        last_seen_at=now,
                        last_seen_run_id=run_id,
                        content_hash=raw.content_hash,
                        status="active",
                        missing_count=0,
                    )
                )

            if plan.changed:
                existing_version = conn.execute(
                    select(job_versions.c.id).where(
                        job_versions.c.job_id == plan.job_id,
                        job_versions.c.content_hash == raw.content_hash,
                    )
                ).scalar_one_or_none()
                if existing_version is None:
                    conn.execute(
                        insert(job_versions).values(
                            id=str(uuid.uuid4()),
                            job_id=plan.job_id,
                            content_hash=raw.content_hash,
                            payload=raw.model_dump(mode="json"),
                            created_at=now,
                        )
                    )
                conn.execute(
                    delete(notification_outbox).where(
                        notification_outbox.c.job_id == plan.job_id,
                        notification_outbox.c.version_hash != raw.content_hash,
                    )
                )

            notifications_enqueued = 0
            for decision in decisions:
                self._record_match(
                    conn,
                    plan.job_id,
                    decision.profile_version,
                    raw.content_hash,
                    decision.result,
                    now,
                )
                if (
                    decision.notification_message is not None
                    and decision.result.eligible
                    and self._queue_notification(
                        conn,
                        plan.job_id,
                        str(decision.result.profile),
                        raw.content_hash,
                        decision.result.score,
                        decision.notification_message,
                        now,
                    )
                ):
                    notifications_enqueued += 1

            return JobPersistResult(
                job_id=plan.job_id,
                is_new=plan.is_new,
                changed=plan.changed,
                first_seen_at=plan.first_seen_at,
                notifications_enqueued=notifications_enqueued,
            )

    def persist_job_decisions_batch(
        self,
        company_id: str,
        run_id: str,
        items: list[tuple[RawJob, JobPlan, list[MatchDecision]]],
    ) -> list[JobPersistResult]:
        if not items:
            return []
        results: list[JobPersistResult | None] = [None] * len(items)
        fallback: list[int] = []
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            self._assert_active_run(conn, run_id)
            locked_items = [
                (index, raw, plan, decisions)
                for index, (raw, plan, decisions) in enumerate(items)
                if plan.is_new or plan.changed or decisions
            ]
            rows_by_external_id: dict[str, dict[str, Any]] = {}
            if locked_items:
                external_ids = [raw.stable_external_id for _, raw, _, _ in locked_items]
                rows = conn.execute(
                    select(jobs)
                    .where(
                        jobs.c.company_id == company_id,
                        jobs.c.external_job_id.in_(external_ids),
                    )
                    .with_for_update()
                ).mappings()
                rows_by_external_id = {row["external_job_id"]: row for row in rows}

            valid_locked: list[tuple[int, RawJob, JobPlan, list[MatchDecision]]] = []
            for index, raw, plan, decisions in locked_items:
                try:
                    self._validate_job_plan(
                        plan,
                        raw,
                        rows_by_external_id.get(raw.stable_external_id),
                    )
                except RuntimeError:
                    fallback.append(index)
                    logger.warning(
                        "Falling back to single-job persistence for %s after snapshot mismatch",
                        raw.stable_external_id,
                    )
                else:
                    valid_locked.append((index, raw, plan, decisions))

            valid_indexes = {index for index, *_ in valid_locked}
            touch_items = [
                (index, raw, plan, decisions)
                for index, (raw, plan, decisions) in enumerate(items)
                if index not in fallback and index not in valid_indexes
            ]
            if touch_items:
                conn.execute(
                    update(jobs)
                    .where(
                        jobs.c.company_id == company_id,
                        jobs.c.external_job_id.in_(
                            [raw.stable_external_id for _, raw, _, _ in touch_items]
                        ),
                    )
                    .values(
                        last_seen_at=now,
                        last_seen_run_id=run_id,
                        status="active",
                        missing_count=0,
                    )
                )
                for index, _, plan, _ in touch_items:
                    results[index] = JobPersistResult(
                        job_id=plan.job_id,
                        is_new=plan.is_new,
                        changed=plan.changed,
                        first_seen_at=plan.first_seen_at,
                        notifications_enqueued=0,
                    )

            if valid_locked:
                new_items = [
                    (index, raw, plan, decisions)
                    for index, raw, plan, decisions in valid_locked
                    if plan.is_new
                ]
                existing_items = [
                    (index, raw, plan, decisions)
                    for index, raw, plan, decisions in valid_locked
                    if not plan.is_new
                ]
                if new_items:
                    conn.execute(
                        insert(jobs),
                        [
                            {
                                "id": plan.job_id,
                                "company_id": company_id,
                                "external_job_id": raw.stable_external_id,
                                "title": raw.title,
                                "location_raw": raw.location_raw,
                                "description_raw": raw.description_raw,
                                "canonical_url": raw.canonical_url,
                                "source_posted_at": raw.posted_at,
                                "first_seen_at": plan.first_seen_at,
                                "last_seen_at": now,
                                "last_seen_run_id": run_id,
                                "content_hash": raw.content_hash,
                                "status": "active",
                                "missing_count": 0,
                            }
                            for _, raw, plan, _ in new_items
                        ],
                    )
                if existing_items:
                    conn.execute(
                        update(jobs)
                        .where(jobs.c.id == bindparam("_persist_job_id"))
                        .values(
                            title=bindparam("_title"),
                            location_raw=bindparam("_location_raw"),
                            description_raw=bindparam("_description_raw"),
                            canonical_url=bindparam("_canonical_url"),
                            source_posted_at=bindparam("_source_posted_at"),
                            last_seen_at=bindparam("_last_seen_at"),
                            last_seen_run_id=bindparam("_last_seen_run_id"),
                            content_hash=bindparam("_content_hash"),
                            status=bindparam("_status"),
                            missing_count=bindparam("_missing_count"),
                        ),
                        [
                            {
                                "_persist_job_id": plan.job_id,
                                "_title": raw.title,
                                "_location_raw": raw.location_raw,
                                "_description_raw": raw.description_raw,
                                "_canonical_url": raw.canonical_url,
                                "_source_posted_at": raw.posted_at,
                                "_last_seen_at": now,
                                "_last_seen_run_id": run_id,
                                "_content_hash": raw.content_hash,
                                "_status": "active",
                                "_missing_count": 0,
                            }
                            for _, raw, plan, _ in existing_items
                        ],
                    )

                changed_items = [
                    (index, raw, plan, decisions)
                    for index, raw, plan, decisions in valid_locked
                    if plan.changed
                ]
                if changed_items:
                    job_ids = [plan.job_id for _, _, plan, _ in changed_items]
                    content_hashes = [raw.content_hash for _, raw, _, _ in changed_items]
                    existing_versions = {
                        (row["job_id"], row["content_hash"])
                        for row in conn.execute(
                            select(job_versions.c.job_id, job_versions.c.content_hash).where(
                                job_versions.c.job_id.in_(job_ids),
                                job_versions.c.content_hash.in_(content_hashes),
                            )
                        ).mappings()
                    }
                    missing_versions = [
                        {
                            "id": str(uuid.uuid4()),
                            "job_id": plan.job_id,
                            "content_hash": raw.content_hash,
                            "payload": raw.model_dump(mode="json"),
                            "created_at": now,
                        }
                        for _, raw, plan, _ in changed_items
                        if (plan.job_id, raw.content_hash) not in existing_versions
                    ]
                    if missing_versions:
                        conn.execute(insert(job_versions), missing_versions)
                    conn.execute(
                        delete(notification_outbox).where(
                            or_(
                                *(
                                    and_(
                                        notification_outbox.c.job_id == plan.job_id,
                                        notification_outbox.c.version_hash != raw.content_hash,
                                    )
                                    for _, raw, plan, _ in changed_items
                                )
                            )
                        )
                    )

                match_entries = [
                    (index, raw, plan, decision)
                    for index, raw, plan, decisions in valid_locked
                    for decision in decisions
                ]
                existing_match_keys: set[tuple[str, str, str, str]] = set()
                if match_entries:
                    match_job_ids = [plan.job_id for _, _, plan, _ in match_entries]
                    match_hashes = [raw.content_hash for _, raw, _, _ in match_entries]
                    existing_match_keys = {
                        (
                            row["job_id"],
                            row["profile"],
                            row["profile_version"],
                            row["content_hash"],
                        )
                        for row in conn.execute(
                            select(
                                match_results.c.job_id,
                                match_results.c.profile,
                                match_results.c.profile_version,
                                match_results.c.content_hash,
                            ).where(
                                match_results.c.job_id.in_(match_job_ids),
                                match_results.c.content_hash.in_(match_hashes),
                            )
                        ).mappings()
                    }
                    match_values = []
                    for _, raw, plan, decision in match_entries:
                        key = (
                            plan.job_id,
                            str(decision.result.profile),
                            decision.profile_version,
                            raw.content_hash,
                        )
                        if key in existing_match_keys:
                            continue
                        existing_match_keys.add(key)
                        match_values.append(
                            {
                                "id": str(uuid.uuid4()),
                                "job_id": plan.job_id,
                                "profile": str(decision.result.profile),
                                "profile_version": decision.profile_version,
                                "content_hash": raw.content_hash,
                                "score": decision.result.score,
                                "eligible": decision.result.eligible,
                                "tier": decision.result.tier,
                                "details": decision.result.model_dump(mode="json"),
                                "created_at": now,
                            }
                        )
                    if match_values:
                        conn.execute(insert(match_results), match_values)

                notification_entries = [
                    (index, raw, plan, decision)
                    for index, raw, plan, decisions in valid_locked
                    for decision in decisions
                    if decision.notification_message is not None and decision.result.eligible
                ]
                notification_counts: dict[int, int] = {}
                if notification_entries:
                    notification_job_ids = [plan.job_id for _, _, plan, _ in notification_entries]
                    notification_hashes = [
                        raw.content_hash for _, raw, _, _ in notification_entries
                    ]
                    notification_profiles = [
                        str(decision.result.profile) for _, _, _, decision in notification_entries
                    ]
                    sent_keys = {
                        (
                            row["job_id"],
                            row["profile"],
                            row["version_hash"],
                        )
                        for row in conn.execute(
                            select(
                                notifications.c.job_id,
                                notifications.c.profile,
                                notifications.c.version_hash,
                            ).where(
                                notifications.c.job_id.in_(notification_job_ids),
                                notifications.c.profile.in_(notification_profiles),
                                notifications.c.version_hash.in_(notification_hashes),
                                notifications.c.channel == "telegram",
                            )
                        ).mappings()
                    }
                    queued_keys = {
                        (
                            row["job_id"],
                            row["profile"],
                            row["version_hash"],
                        )
                        for row in conn.execute(
                            select(
                                notification_outbox.c.job_id,
                                notification_outbox.c.profile,
                                notification_outbox.c.version_hash,
                            ).where(
                                notification_outbox.c.job_id.in_(notification_job_ids),
                                notification_outbox.c.profile.in_(notification_profiles),
                                notification_outbox.c.version_hash.in_(notification_hashes),
                                notification_outbox.c.channel == "telegram",
                            )
                        ).mappings()
                    }
                    outbox_values = []
                    for index, raw, plan, decision in notification_entries:
                        key = (
                            plan.job_id,
                            str(decision.result.profile),
                            raw.content_hash,
                        )
                        if key in sent_keys or key in queued_keys:
                            continue
                        queued_keys.add(key)
                        notification_counts[index] = notification_counts.get(index, 0) + 1
                        outbox_values.append(
                            {
                                "id": str(uuid.uuid4()),
                                "job_id": plan.job_id,
                                "profile": str(decision.result.profile),
                                "channel": "telegram",
                                "version_hash": raw.content_hash,
                                "score": decision.result.score,
                                "message": decision.notification_message,
                                "created_at": now,
                                "attempt_count": 0,
                            }
                        )
                    if outbox_values:
                        conn.execute(insert(notification_outbox), outbox_values)

                for index, raw, plan, _ in valid_locked:
                    results[index] = JobPersistResult(
                        job_id=plan.job_id,
                        is_new=plan.is_new,
                        changed=plan.changed,
                        first_seen_at=plan.first_seen_at,
                        notifications_enqueued=notification_counts.get(index, 0),
                    )

        for index in fallback:
            raw, plan, decisions = items[index]
            results[index] = self.persist_job_decisions(
                company_id,
                run_id,
                raw,
                plan,
                decisions,
            )
        if any(result is None for result in results):
            raise RuntimeError("batch persistence did not produce a result for every job")
        return cast(list[JobPersistResult], results)

    def upsert_job(
        self, company_id: str, run_id: str, raw: RawJob
    ) -> tuple[str, bool, bool, datetime]:
        plan = self.plan_job(company_id, raw)
        persisted = self.persist_job_decisions(company_id, run_id, raw, plan, [])
        return (
            persisted.job_id,
            persisted.is_new,
            persisted.changed,
            persisted.first_seen_at,
        )

    def mark_missing(self, company_id: str, run_id: str) -> int:
        with self.engine.begin() as conn:
            self._assert_active_run(conn, run_id)
            candidates = conn.execute(
                select(jobs.c.id, jobs.c.missing_count)
                .where(
                    jobs.c.company_id == company_id,
                    jobs.c.status == "active",
                    jobs.c.last_seen_run_id != run_id,
                )
                .with_for_update()
            ).all()
            closed = 0
            closed_job_ids: list[str] = []
            for job_id, count in candidates:
                new_count = count + 1
                values: dict[str, Any] = {"missing_count": new_count}
                if new_count >= 2:
                    values["status"] = "closed"
                    closed += 1
                    closed_job_ids.append(job_id)
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))
            if closed_job_ids:
                conn.execute(
                    delete(notification_outbox).where(
                        notification_outbox.c.job_id.in_(closed_job_ids)
                    )
                )
            return closed

    def record_match(
        self, job_id: str, profile_version: str, content_hash: str, result: MatchResult
    ) -> None:
        with self.engine.begin() as conn:
            self._record_match(
                conn,
                job_id,
                profile_version,
                content_hash,
                result,
                datetime.now(UTC),
            )

    @staticmethod
    def _record_match(
        conn,
        job_id: str,
        profile_version: str,
        content_hash: str,
        result: MatchResult,
        created_at: datetime,
    ) -> None:
        match_exists = conn.execute(
            select(match_results.c.id).where(
                match_results.c.job_id == job_id,
                match_results.c.profile == str(result.profile),
                match_results.c.profile_version == profile_version,
                match_results.c.content_hash == content_hash,
            )
        ).scalar_one_or_none()
        if match_exists is None:
            conn.execute(
                insert(match_results).values(
                    id=str(uuid.uuid4()),
                    job_id=job_id,
                    profile=str(result.profile),
                    profile_version=profile_version,
                    content_hash=content_hash,
                    score=result.score,
                    eligible=result.eligible,
                    tier=result.tier,
                    details=result.model_dump(mode="json"),
                    created_at=created_at,
                )
            )

    def was_notified(self, job_id: str, profile: str, version_hash: str) -> bool:
        with self.engine.connect() as conn:
            return (
                conn.execute(
                    select(notifications.c.id).where(
                        notifications.c.job_id == job_id,
                        notifications.c.profile == profile,
                        notifications.c.channel == "telegram",
                        notifications.c.version_hash == version_hash,
                    )
                ).scalar_one_or_none()
                is not None
            )

    def queue_notification(
        self,
        job_id: str,
        profile: str,
        version_hash: str,
        score: float,
        message: str,
    ) -> bool:
        with self.engine.begin() as conn:
            return self._queue_notification(
                conn,
                job_id,
                profile,
                version_hash,
                score,
                message,
                datetime.now(UTC),
            )

    @staticmethod
    def _queue_notification(
        conn,
        job_id: str,
        profile: str,
        version_hash: str,
        score: float,
        message: str,
        created_at: datetime,
    ) -> bool:
        already_sent = conn.execute(
            select(notifications.c.id).where(
                notifications.c.job_id == job_id,
                notifications.c.profile == profile,
                notifications.c.channel == "telegram",
                notifications.c.version_hash == version_hash,
            )
        ).scalar_one_or_none()
        if already_sent:
            return False
        queued = conn.execute(
            select(notification_outbox.c.id).where(
                notification_outbox.c.job_id == job_id,
                notification_outbox.c.profile == profile,
                notification_outbox.c.channel == "telegram",
                notification_outbox.c.version_hash == version_hash,
            )
        ).scalar_one_or_none()
        if queued:
            return False
        conn.execute(
            insert(notification_outbox).values(
                id=str(uuid.uuid4()),
                job_id=job_id,
                profile=profile,
                channel="telegram",
                version_hash=version_hash,
                score=score,
                message=message,
                created_at=created_at,
                attempt_count=0,
            )
        )
        return True

    def list_pending_notifications(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(notification_outbox)
                    .join(jobs, jobs.c.id == notification_outbox.c.job_id)
                    .where(
                        notification_outbox.c.channel == "telegram",
                        notification_outbox.c.claim_token.is_(None),
                        jobs.c.status == "active",
                        jobs.c.content_hash == notification_outbox.c.version_hash,
                    )
                    .order_by(
                        notification_outbox.c.score.desc(),
                        notification_outbox.c.created_at,
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def claim_pending_notifications(
        self,
        run_id: str,
        limit: int,
        *,
        stale_after_minutes: int = 15,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        now = datetime.now(UTC)
        stale_before = now - timedelta(minutes=stale_after_minutes)
        with self.engine.begin() as conn:
            self._assert_active_run(conn, run_id)
            current_job = exists(
                select(jobs.c.id).where(
                    jobs.c.id == notification_outbox.c.job_id,
                    jobs.c.status == "active",
                    jobs.c.content_hash == notification_outbox.c.version_hash,
                )
            )
            conn.execute(delete(notification_outbox).where(~current_job))
            active_owner = exists(
                select(source_runs.c.id).where(
                    source_runs.c.id == notification_outbox.c.claimed_by_run_id,
                    source_runs.c.status == "running",
                )
            )
            conn.execute(
                update(notification_outbox)
                .where(
                    notification_outbox.c.claim_token.is_not(None),
                    or_(
                        notification_outbox.c.claimed_at.is_(None),
                        notification_outbox.c.claimed_at <= stale_before,
                        ~active_owner,
                    ),
                )
                .values(
                    claim_token=None,
                    claimed_by_run_id=None,
                    claimed_at=None,
                )
            )
            rows = (
                conn.execute(
                    select(notification_outbox)
                    .join(jobs, jobs.c.id == notification_outbox.c.job_id)
                    .where(
                        notification_outbox.c.channel == "telegram",
                        notification_outbox.c.claim_token.is_(None),
                        or_(
                            notification_outbox.c.next_attempt_at.is_(None),
                            notification_outbox.c.next_attempt_at <= now,
                        ),
                        jobs.c.status == "active",
                        jobs.c.content_hash == notification_outbox.c.version_hash,
                    )
                    .order_by(
                        notification_outbox.c.score.desc(),
                        notification_outbox.c.created_at,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=notification_outbox)
                )
                .mappings()
                .all()
            )
            claimed: list[dict[str, Any]] = []
            for row in rows:
                claim_token = str(uuid.uuid4())
                updated = conn.execute(
                    update(notification_outbox)
                    .where(
                        notification_outbox.c.id == row["id"],
                        notification_outbox.c.claim_token.is_(None),
                    )
                    .values(
                        claim_token=claim_token,
                        claimed_by_run_id=run_id,
                        claimed_at=now,
                        attempt_count=notification_outbox.c.attempt_count + 1,
                    )
                )
                if updated.rowcount == 1:
                    item = dict(row)
                    item.update(
                        claim_token=claim_token,
                        claimed_by_run_id=run_id,
                        claimed_at=now,
                        attempt_count=int(row["attempt_count"]) + 1,
                    )
                    claimed.append(item)
            return claimed

    def pending_notification_count(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count())
                    .select_from(
                        notification_outbox.join(
                            jobs,
                            jobs.c.id == notification_outbox.c.job_id,
                        )
                    )
                    .where(
                        jobs.c.status == "active",
                        jobs.c.content_hash == notification_outbox.c.version_hash,
                    )
                ).scalar_one()
            )

    def release_notification_claim(
        self,
        run_id: str,
        outbox_id: str,
        claim_token: str,
        error: str,
    ) -> bool:
        with self.engine.begin() as conn:
            self._assert_active_run(conn, run_id)
            released = conn.execute(
                update(notification_outbox)
                .where(
                    notification_outbox.c.id == outbox_id,
                    notification_outbox.c.claimed_by_run_id == run_id,
                    notification_outbox.c.claim_token == claim_token,
                )
                .values(
                    claim_token=None,
                    claimed_by_run_id=None,
                    claimed_at=None,
                    last_error=error[:1000],
                    next_attempt_at=None,
                )
            )
            return released.rowcount == 1

    def mark_notification_sent(
        self,
        run_id: str,
        outbox_id: str,
        claim_token: str,
    ) -> bool:
        with self.engine.begin() as conn:
            self._assert_active_run(conn, run_id)
            item = (
                conn.execute(
                    select(notification_outbox)
                    .where(
                        notification_outbox.c.id == outbox_id,
                        notification_outbox.c.claimed_by_run_id == run_id,
                        notification_outbox.c.claim_token == claim_token,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if item is None:
                return False
            existing = conn.execute(
                select(notifications.c.id).where(
                    notifications.c.job_id == item["job_id"],
                    notifications.c.profile == item["profile"],
                    notifications.c.channel == item["channel"],
                    notifications.c.version_hash == item["version_hash"],
                )
            ).scalar_one_or_none()
            if existing is None:
                conn.execute(
                    insert(notifications).values(
                        id=str(uuid.uuid4()),
                        job_id=item["job_id"],
                        profile=item["profile"],
                        channel=item["channel"],
                        version_hash=item["version_hash"],
                        sent_at=datetime.now(UTC),
                    )
                )
            deleted = conn.execute(
                delete(notification_outbox).where(
                    notification_outbox.c.id == outbox_id,
                    notification_outbox.c.claim_token == claim_token,
                )
            )
            return deleted.rowcount == 1

    def record_notification(self, job_id: str, profile: str, version_hash: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(notifications).values(
                    id=str(uuid.uuid4()),
                    job_id=job_id,
                    profile=profile,
                    channel="telegram",
                    version_hash=version_hash,
                    sent_at=datetime.now(UTC),
                )
            )

    def source_succeeded(self, company_id: str, run_id: str | None = None) -> None:
        with self.engine.begin() as conn:
            if run_id:
                self._assert_active_run(conn, run_id)
            conn.execute(
                update(companies)
                .where(companies.c.id == company_id)
                .values(
                    consecutive_failures=0,
                    last_success_at=datetime.now(UTC),
                    baseline_completed=True,
                )
            )

    def source_failed(self, company_id: str, run_id: str | None = None) -> int:
        with self.engine.begin() as conn:
            if run_id:
                self._assert_active_run(conn, run_id)
            conn.execute(
                update(companies)
                .where(companies.c.id == company_id)
                .values(consecutive_failures=companies.c.consecutive_failures + 1)
            )
            return int(
                conn.execute(
                    select(companies.c.consecutive_failures).where(companies.c.id == company_id)
                ).scalar_one()
            )

    def replace_ndx_snapshot(self, rows: list[dict[str, Any]], as_of_date: str) -> None:
        """Atomically activate the latest snapshot while retaining prior constituents."""
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(update(ndx_constituents).values(active=False, updated_at=now))
            for row in rows:
                symbol = str(row.get("symbol") or row.get("Symbol") or "").strip().upper()
                name = str(
                    row.get("companyName") or row.get("Company Name") or row.get("name") or symbol
                )
                if not symbol:
                    continue
                existing = conn.execute(
                    select(ndx_constituents.c.symbol).where(ndx_constituents.c.symbol == symbol)
                ).scalar_one_or_none()
                values = {
                    "company_name": name,
                    "as_of_date": as_of_date,
                    "active": True,
                    "payload": row,
                    "updated_at": now,
                }
                if existing:
                    conn.execute(
                        update(ndx_constituents)
                        .where(ndx_constituents.c.symbol == symbol)
                        .values(**values)
                    )
                else:
                    conn.execute(insert(ndx_constituents).values(symbol=symbol, **values))

    def set_application_stage(
        self, job_id: str, stage: str, notes: str | None = None
    ) -> dict[str, Any]:
        if stage not in APPLICATION_STAGES:
            raise ValueError(f"stage must be one of: {', '.join(sorted(APPLICATION_STAGES))}")

        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            job_exists = conn.execute(
                select(jobs.c.id).where(jobs.c.id == job_id)
            ).scalar_one_or_none()
            if job_exists is None:
                raise KeyError(job_id)

            existing = (
                conn.execute(select(applications).where(applications.c.job_id == job_id))
                .mappings()
                .one_or_none()
            )
            values: dict[str, Any] = {"stage": stage, "updated_at": now}
            if notes is not None:
                values["notes"] = notes
            if stage in {"saved", "applied", "interview", "offer"} and (
                existing is None or existing["first_saved_at"] is None
            ):
                values["first_saved_at"] = now
            if stage in {"applied", "interview", "offer"} and (
                existing is None or existing["first_applied_at"] is None
            ):
                values["first_applied_at"] = now
            if stage in {"interview", "offer"} and (
                existing is None or existing["first_interview_at"] is None
            ):
                values["first_interview_at"] = now

            if existing:
                conn.execute(
                    update(applications).where(applications.c.job_id == job_id).values(**values)
                )
            else:
                conn.execute(insert(applications).values(job_id=job_id, **values))

            return dict(
                conn.execute(select(applications).where(applications.c.job_id == job_id))
                .mappings()
                .one()
            )

    def latest_run(
        self, statuses: tuple[str, ...] = ("success", "partial")
    ) -> dict[str, Any] | None:
        """Return the most recently finished run whose status is in `statuses`."""
        stmt = (
            select(
                source_runs.c.run_key,
                source_runs.c.status,
                source_runs.c.finished_at,
                source_runs.c.stats,
            )
            .where(source_runs.c.status.in_(statuses), source_runs.c.finished_at.is_not(None))
            .order_by(source_runs.c.finished_at.desc(), source_runs.c.run_key.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().one_or_none()
        return dict(row) if row else None

    def list_handoff_jobs(
        self,
        days: int = 7,
        limit: int = 40,
        buckets: tuple[str, ...] = ("target", "stretch"),
        min_score: float = 0.0,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return active, not-yet-actioned jobs with a match for their current content.

        Only match rows whose `content_hash` equals the job's current `content_hash` are
        considered, so a stale score from an earlier version of a posting is never exported.
        The winning match per job is the highest-scoring one, tie-broken by profile name.
        Pass `now` to make the window boundary independent of wall-clock time.
        """
        days = max(1, min(days, 365))
        limit = max(1, min(limit, 500))
        since = (now or datetime.now(UTC)) - timedelta(days=days)
        stage_expr = func.coalesce(applications.c.stage, "recommended")

        stmt = (
            select(
                jobs.c.id.label("job_id"),
                jobs.c.title,
                jobs.c.location_raw.label("location"),
                jobs.c.canonical_url.label("url"),
                jobs.c.first_seen_at,
                jobs.c.source_posted_at,
                jobs.c.content_hash,
                companies.c.slug.label("company_slug"),
                companies.c.name.label("company"),
                companies.c.industry,
                match_results.c.profile,
                match_results.c.score,
                match_results.c.tier,
                match_results.c.details,
            )
            .select_from(
                jobs.join(companies, companies.c.id == jobs.c.company_id)
                .join(
                    match_results,
                    and_(
                        match_results.c.job_id == jobs.c.id,
                        match_results.c.content_hash == jobs.c.content_hash,
                        match_results.c.eligible.is_(True),
                    ),
                )
                .outerjoin(applications, applications.c.job_id == jobs.c.id)
            )
            .where(
                jobs.c.status == "active",
                jobs.c.first_seen_at >= since,
                stage_expr == "recommended",
                match_results.c.score >= min_score,
            )
        )

        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        winners: dict[str, dict[str, Any]] = {}
        for row in rows:
            details = row["details"] if isinstance(row["details"], Mapping) else {}
            bucket = str(details.get("bucket", "target"))
            if bucket not in buckets:
                continue
            candidate = {
                "job_id": row["job_id"],
                "title": row["title"],
                "company": row["company"],
                "company_slug": row["company_slug"],
                "industry": row["industry"],
                "location": row["location"],
                "url": row["url"],
                "first_seen_at": row["first_seen_at"],
                "source_posted_at": row["source_posted_at"],
                "content_hash": row["content_hash"],
                "profile": row["profile"],
                "score": float(row["score"]),
                "tier": row["tier"],
                "bucket": bucket,
                "fit": details.get("fit"),
                "reach": details.get("reach"),
                "level": details.get("level"),
                "required_years_min": details.get("required_years_min"),
                "reasons": list(details.get("reasons") or []),
                "gaps": list(details.get("gaps") or []),
            }
            current = winners.get(candidate["job_id"])
            if current is None or _handoff_rank(candidate) < _handoff_rank(current):
                winners[candidate["job_id"]] = candidate

        ordered = sorted(winners.values(), key=_handoff_order)
        return ordered[:limit]

    def dashboard_snapshot(self, days: int = 30) -> dict[str, Any]:
        days = max(1, min(days, 365))
        now = datetime.now(UTC)
        since = now - timedelta(days=days)
        active_match = match_results.c.eligible.is_(True)

        with self.engine.connect() as conn:
            recommended = conn.execute(
                select(func.count(func.distinct(jobs.c.id)))
                .select_from(jobs.join(match_results, match_results.c.job_id == jobs.c.id))
                .where(active_match, jobs.c.status == "active", jobs.c.first_seen_at >= since)
            ).scalar_one()
            applied = conn.execute(
                select(func.count())
                .select_from(applications)
                .where(applications.c.first_applied_at >= since)
            ).scalar_one()
            interviews = conn.execute(
                select(func.count())
                .select_from(applications)
                .where(applications.c.first_interview_at >= since)
            ).scalar_one()

            industries = (
                conn.execute(
                    select(
                        companies.c.industry,
                        func.count(func.distinct(jobs.c.id)).label("recommended"),
                        func.count(
                            func.distinct(
                                case((applications.c.first_applied_at >= since, jobs.c.id))
                            )
                        ).label("applied"),
                        func.count(
                            func.distinct(
                                case((applications.c.first_interview_at >= since, jobs.c.id))
                            )
                        ).label("interviews"),
                    )
                    .select_from(
                        jobs.join(companies, companies.c.id == jobs.c.company_id)
                        .join(match_results, match_results.c.job_id == jobs.c.id)
                        .outerjoin(applications, applications.c.job_id == jobs.c.id)
                    )
                    .where(active_match, jobs.c.status == "active", jobs.c.first_seen_at >= since)
                    .group_by(companies.c.industry)
                    .order_by(companies.c.industry)
                )
                .mappings()
                .all()
            )

            stages = (
                conn.execute(
                    select(
                        func.coalesce(applications.c.stage, "recommended").label("stage"),
                        func.count(func.distinct(jobs.c.id)).label("count"),
                    )
                    .select_from(
                        jobs.join(match_results, match_results.c.job_id == jobs.c.id).outerjoin(
                            applications, applications.c.job_id == jobs.c.id
                        )
                    )
                    .where(active_match, jobs.c.status == "active", jobs.c.first_seen_at >= since)
                    .group_by(func.coalesce(applications.c.stage, "recommended"))
                )
                .mappings()
                .all()
            )

            sources = (
                conn.execute(
                    select(
                        companies.c.ats_type.label("source"),
                        func.count(func.distinct(jobs.c.id)).label("recommended"),
                        func.count(
                            func.distinct(
                                case((applications.c.first_applied_at >= since, jobs.c.id))
                            )
                        ).label("applied"),
                        func.count(
                            func.distinct(
                                case((applications.c.first_interview_at >= since, jobs.c.id))
                            )
                        ).label("interviews"),
                    )
                    .select_from(
                        jobs.join(companies, companies.c.id == jobs.c.company_id)
                        .join(match_results, match_results.c.job_id == jobs.c.id)
                        .outerjoin(applications, applications.c.job_id == jobs.c.id)
                    )
                    .where(active_match, jobs.c.status == "active", jobs.c.first_seen_at >= since)
                    .group_by(companies.c.ats_type)
                    .order_by(companies.c.ats_type)
                )
                .mappings()
                .all()
            )

            queue = self.list_dashboard_jobs(days=days, limit=8, include_archived=False)

        apply_rate = float(applied / recommended) if recommended else 0.0
        interview_rate = float(interviews / applied) if applied else 0.0
        total_rate = float(interviews / recommended) if recommended else 0.0
        return {
            "window_days": days,
            "generated_at": now.isoformat(),
            "kpis": {
                "recommended": int(recommended),
                "applied": int(applied),
                "interviews": int(interviews),
                "apply_rate": apply_rate,
                "interview_rate": interview_rate,
                "total_rate": total_rate,
            },
            "industries": [dict(row) for row in industries],
            "stages": [dict(row) for row in stages],
            "sources": [dict(row) for row in sources],
            "queue": queue,
        }

    def list_dashboard_jobs(
        self,
        days: int = 30,
        limit: int = 50,
        stage: str | None = None,
        industry: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        days = max(1, min(days, 365))
        limit = max(1, min(limit, 200))
        since = datetime.now(UTC) - timedelta(days=days)
        score_by_job = (
            select(
                match_results.c.job_id.label("job_id"),
                func.max(match_results.c.score).label("score"),
            )
            .where(match_results.c.eligible.is_(True))
            .group_by(match_results.c.job_id)
            .subquery()
        )
        stage_expr = func.coalesce(applications.c.stage, "recommended")

        stmt = (
            select(
                jobs.c.id,
                jobs.c.title,
                jobs.c.location_raw,
                jobs.c.canonical_url,
                jobs.c.first_seen_at,
                jobs.c.source_posted_at,
                jobs.c.status,
                companies.c.name.label("company"),
                companies.c.industry,
                score_by_job.c.score,
                stage_expr.label("stage"),
                applications.c.notes,
                applications.c.updated_at.label("application_updated_at"),
            )
            .select_from(
                jobs.join(companies, companies.c.id == jobs.c.company_id)
                .join(score_by_job, score_by_job.c.job_id == jobs.c.id)
                .outerjoin(applications, applications.c.job_id == jobs.c.id)
            )
            .where(jobs.c.status == "active", jobs.c.first_seen_at >= since)
            .order_by(score_by_job.c.score.desc(), jobs.c.first_seen_at.desc())
            .limit(limit)
        )
        if stage:
            stmt = stmt.where(stage_expr == stage)
        if industry:
            stmt = stmt.where(companies.c.industry == industry)
        if not include_archived:
            stmt = stmt.where(stage_expr != "archived")

        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(row) for row in rows]
