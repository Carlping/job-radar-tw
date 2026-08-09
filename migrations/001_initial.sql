-- Supabase/PostgreSQL schema. Application UUIDs are generated client-side.
create table if not exists companies (
  id varchar(36) primary key, slug varchar(120) not null, name varchar(255) not null,
  careers_url text not null, ats_type varchar(50) not null, industry varchar(80) not null,
  profiles jsonb not null, priority integer not null, enabled boolean not null,
  source_verified boolean not null default false, config jsonb not null,
  consecutive_failures integer not null default 0, last_success_at timestamptz,
  baseline_completed boolean not null default false,
  constraint uq_companies_slug unique(slug)
);
alter table companies
  add column if not exists baseline_completed boolean not null default false;
create table if not exists source_runs (
  id varchar(36) primary key, run_key varchar(100) not null, started_at timestamptz not null,
  finished_at timestamptz, status varchar(30) not null, stats jsonb not null default '{}'::jsonb,
  errors jsonb not null default '[]'::jsonb,
  constraint uq_source_runs_run_key unique(run_key)
);
create table if not exists jobs (
  id varchar(36) primary key, company_id varchar(36) not null references companies(id),
  external_job_id varchar(255) not null, title text not null, location_raw text not null,
  description_raw text not null, canonical_url text not null, source_posted_at timestamptz,
  first_seen_at timestamptz not null, last_seen_at timestamptz not null,
  last_seen_run_id varchar(36) not null, content_hash varchar(64) not null,
  status varchar(20) not null default 'active', missing_count integer not null default 0,
  constraint uq_job_company_external unique(company_id, external_job_id)
);
create table if not exists job_versions (
  id varchar(36) primary key, job_id varchar(36) not null references jobs(id),
  content_hash varchar(64) not null, payload jsonb not null, created_at timestamptz not null,
  constraint uq_job_version_hash unique(job_id, content_hash)
);
create table if not exists match_results (
  id varchar(36) primary key, job_id varchar(36) not null references jobs(id),
  profile varchar(40) not null, profile_version varchar(30) not null,
  content_hash varchar(64) not null, score double precision not null, eligible boolean not null,
  tier varchar(30) not null, details jsonb not null, created_at timestamptz not null,
  constraint uq_match_version unique(job_id, profile, profile_version, content_hash)
);
create table if not exists notifications (
  id varchar(36) primary key, job_id varchar(36) not null references jobs(id),
  profile varchar(40) not null, channel varchar(30) not null, version_hash varchar(64) not null,
  sent_at timestamptz not null,
  constraint uq_notification_dedupe unique(job_id, profile, channel, version_hash)
);
create table if not exists notification_outbox (
  id varchar(36) primary key, job_id varchar(36) not null references jobs(id),
  profile varchar(40) not null, channel varchar(30) not null default 'telegram',
  version_hash varchar(64) not null, score double precision not null,
  message text not null, created_at timestamptz not null,
  claim_token varchar(36), claimed_by_run_id varchar(36), claimed_at timestamptz,
  attempt_count integer not null default 0, last_error text, next_attempt_at timestamptz,
  constraint uq_notification_outbox_dedupe unique(job_id, profile, channel, version_hash)
);
alter table notification_outbox add column if not exists claim_token varchar(36);
alter table notification_outbox add column if not exists claimed_by_run_id varchar(36);
alter table notification_outbox add column if not exists claimed_at timestamptz;
alter table notification_outbox
  add column if not exists attempt_count integer not null default 0;
alter table notification_outbox add column if not exists last_error text;
alter table notification_outbox add column if not exists next_attempt_at timestamptz;
create table if not exists applications (
  job_id varchar(36) primary key references jobs(id),
  stage varchar(30) not null default 'recommended',
  notes text,
  first_saved_at timestamptz,
  first_applied_at timestamptz,
  first_interview_at timestamptz,
  updated_at timestamptz not null
);
create table if not exists ndx_constituents (
  symbol varchar(20) primary key, company_name varchar(255) not null, as_of_date varchar(10) not null,
  active boolean not null default true, payload jsonb not null, updated_at timestamptz not null
);
create index if not exists ix_jobs_status on jobs(status);
create index if not exists ix_jobs_first_seen on jobs(first_seen_at desc);
create index if not exists ix_notification_outbox_pending
  on notification_outbox(channel, claim_token, score desc, created_at);

-- Preserve the old monitor's baseline state when this idempotent file upgrades an existing DB.
update companies as company
set baseline_completed = true
where baseline_completed = false
  and exists (select 1 from jobs where jobs.company_id = company.id);

alter table companies enable row level security;
alter table source_runs enable row level security;
alter table jobs enable row level security;
alter table job_versions enable row level security;
alter table match_results enable row level security;
alter table notifications enable row level security;
alter table notification_outbox enable row level security;
alter table applications enable row level security;
alter table ndx_constituents enable row level security;
-- No anon/authenticated policies are created. PostgreSQL/service-role access remains available.
