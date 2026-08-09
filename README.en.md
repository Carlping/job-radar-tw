# Job Radar TW｜職缺雷達

[繁體中文](README.md) · [English](README.en.md)

[![Tests](https://github.com/Carlping/job-radar-tw/actions/workflows/test.yml/badge.svg)](https://github.com/Carlping/job-radar-tw/actions/workflows/test.yml)

Job Radar TW checks official company career sites on a schedule, stores the results in Supabase, and sends matched jobs to Telegram. The monitor runs in GitHub Actions, so you do not need to keep a server online.

It currently supports Greenhouse, Lever, Ashby, SmartRecruiters, Workday, and public job pages with JSON-LD. It only reads public sources: it does not sign in, bypass access controls, or apply for jobs. It is a daily monitor, not a real-time feed.

The search rules are fully configurable for jobs in Taiwan, overseas roles, remote work, or a mix of these. Cloudflare is not required for the monitor. See the [optional Cloudflare guide](docs/cloudflare-deploy.md) only if you want to publish the dashboard.

## What you need

- A GitHub account.
- A [Supabase](https://supabase.com/) project.
- A Telegram bot and the chat ID that should receive alerts.

If you plan to use GitHub Actions only, you can finish the setup in a browser. Python is needed only for local use.

## Deploy your own copy

### 1. Create the repository

On GitHub, choose **Use this template → Create a new repository**. If the template button is unavailable, use **Fork** instead.

Use a private repository if you plan to include resume data. A fork of a public repository stays public; if you need a private copy, use the template or clone the project and push it to a new private repository. Never put tokens, database credentials, or personal resume details in committed files.

After forking, open the **Actions** tab and enable workflows. [GitHub does not run workflows in a public fork by default](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflows-in-forked-repositories).

### 2. Set your search rules

Start with these files:

- `config/preferences.yml` controls locations, remote work, citizenship or clearance exclusions, and excluded seniority levels.
- `config/companies.yml` lists the companies and official ATS endpoints to monitor. Keep at least one company set to `enabled: true`.
- `config/profiles.yml` defines target titles, domains, skills, scoring weights, and notification thresholds. Each profile name must match the names referenced by `companies.yml`.

You can edit and commit these files in GitHub. The `weights` in each profile must add up to `1.0`. Set `source_verified: true` only after the endpoint has completed a real fetch successfully.

Resume matching is optional. Use `config/resume.example.md` as a format reference, then save your own plain-text or Markdown resume as the repository secret `RESUME_TEXT`. Do not commit a resume containing your name, phone number, address, or other personal data. For local use, you may instead set `RESUME_PATH` in `.env`.

### 3. Create the Supabase database

1. Create a Supabase project and save its database password.
2. In the project, choose **Connect → Session pooler** and copy the connection string for port `5432`.
3. Replace the password placeholder with the real database password. Percent-encode URI characters such as `@`, `:`, or `/` if they appear in the password.

GitHub-hosted runners are commonly on IPv4. Use the [Supabase Session pooler](https://supabase.com/docs/guides/database/connecting-to-postgres) instead of an IPv6-only direct connection. The URL should look like this:

```text
postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
```

The Setup workflow will create the tables later. If you prefer to initialize them manually, paste `migrations/001_initial.sql` into the Supabase **SQL Editor** and run it.

### 4. Create the Telegram bot

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the prompts to get a bot token.
2. Open the new bot and choose **Start**, or send `/start`. A bot cannot message you until you have started the conversation.
3. Open the following URL in a browser, replacing `{TOKEN}` with the bot token:

   ```text
   https://api.telegram.org/bot{TOKEN}/getUpdates
   ```

4. Find `message.chat.id` in the latest response. If `result` is empty, send the bot another message and refresh the page. Group chat IDs are usually negative numbers.

Treat the token like a password. If it appears on a public page, revoke and replace it through BotFather immediately.

### 5. Add GitHub Actions secrets

In your repository, open **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
| --- | --- |
| `DATABASE_URL` | Supabase Session pooler connection string |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID from the previous step |

The core monitor does not need `SUPABASE_SERVICE_ROLE_KEY`. That key is used only by the optional Cloudflare dashboard and should be stored in Cloudflare, never committed to GitHub.

`RESUME_TEXT` and `OPENAI_API_KEY` are optional secrets. Rule-based scoring works without either one.

The same page has a **Variables** section for optional overrides:

- Schedule: `MONITOR_TIMEZONE`, `MONITOR_HOUR`.
- Visa requirement: `VISA_SPONSORSHIP_REQUIRED`.
- Notifications: `IMMEDIATE_NOTIFICATION_MIN_SCORE`, `IMMEDIATE_NOTIFICATION_MAX_SOURCE_AGE_DAYS`, `IMMEDIATE_NOTIFICATION_MAX_PER_RUN`, `DAILY_SUMMARY_MAX_MATCHES`.
- LLM enrichment: `LLM_ENABLED`, `OPENAI_MODEL`; keep the key in the `OPENAI_API_KEY` secret.

If you change the time or time zone, also update the `schedule` block in `.github/workflows/monitor.yml`. Otherwise, a scheduled trigger may fall outside the configured monitoring window and exit without running.

### 6. Run Setup

Open **Actions → Setup Job Radar TW → Run workflow** and leave `send_test_message` checked. This workflow:

- validates the configuration files;
- creates the Supabase tables and enables row-level security (RLS);
- checks the database schema;
- sends a Telegram test message.

Continue only after all steps pass. If **Run workflow** is missing, make sure the workflow file is on the default branch and GitHub Actions is enabled.

### 7. Start the monitor

Open **Actions → Job Radar TW → Run workflow**. Leave `backfill` off for the first run.

The first complete scan for each company establishes its baseline. Jobs that already exist are stored and included in the Daily Summary, but they do not generate individual alerts. The baseline state is stored in Supabase and is marked complete only after a full successful scan. If that first scan is interrupted, its retry remains a baseline run instead of treating the same jobs as newly found.

Later runs send individual alerts for newly found jobs that pass the strong-match and freshness rules. They also send one Daily Summary for every completed run.

To notify yourself about existing baseline jobs, start a manual run with `backfill` checked. Backfill queues every job that currently passes profile eligibility and its score threshold and has not already been notified. It does not require the job to be new, a strong match, or within the normal freshness window. Notification deduplication and `IMMEDIATE_NOTIFICATION_MAX_PER_RUN` still apply.

Anything above the per-run limit stays in `notification_outbox` and is picked up automatically by later runs. You can start another manual run to drain the queue sooner; leave `run_key` blank to generate a new one. Backfill only uses jobs the monitor can currently fetch—it cannot recover old listings that have already been removed from the source site.

The default schedule begins around 20:00 America/New_York and includes several backup triggers. A daily `run_key` skips a run that already succeeded or is still active, while a later trigger can retry a failed or stale run. GitHub schedules may be delayed, so this project is not suitable for time-critical alerts.

## How delivery works

A completed run sends a Daily Summary with source health, fetched jobs, new or changed jobs, closed jobs, and matches above your profile thresholds. Strong new matches also receive individual messages.

Job versions, match results, and pending individual alerts are committed to Supabase in one transaction before Telegram is called. Failed sends and alerts above the per-run limit remain in the durable `notification_outbox` for a later run. `notifications` records completed deliveries so normal retries do not resend them.

Delivery is **at least once**. Telegram's Bot API has no idempotency key, so one rare duplicate is possible if Telegram accepts a message but the GitHub runner stops before the successful delivery can be written back to Supabase.

The main tables are `jobs`, `match_results`, `source_runs`, `notifications`, and `notification_outbox`. In messages, `first seen` is the date this monitor first observed the job; `source N d old` is based on the publication date reported by the source.

## Test locally (optional)

Install Python 3.12 and [`uv`](https://docs.astral.sh/uv/), then clone your repository and install the dependencies:

```text
uv sync --extra dev
```

For optional LLM enrichment, use `uv sync --extra dev --extra llm` instead.

Create a local environment file.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS or Linux:

```bash
cp .env.example .env
```

`.env` is excluded by `.gitignore`. After filling in the values you need, validate the configuration and test one source. A dry run does not write to Supabase or send Telegram messages.

```text
uv run monitor validate-config
uv run monitor sources list --status enabled
uv run monitor dry-run --company COMPANY_SLUG
```

Other useful commands:

```text
uv run monitor dry-run [--company COMPANY_SLUG]
uv run monitor run [--company COMPANY_SLUG] [--backfill] [--run-key KEY]
uv run monitor sources list --status all
uv run monitor sources verify --company COMPANY_SLUG --status all
uv run monitor sources candidates
uv run monitor init-db
uv run monitor doctor --send-telegram
uv run monitor web
```

Keep a newly added source disabled at first. After `uv run monitor sources verify --company COMPANY_SLUG --status all` fetches valid data, repeat it with `--promote`; only a verified source is written back as `enabled: true` and `source_verified: true`.

A real local run needs the database and Telegram settings. Use a new `--run-key` for each manual test, or the monitor will treat it as the same run.

## Troubleshooting

**`Network is unreachable` or the runner cannot reach `db.PROJECT_REF.supabase.co`**

You are probably using the Supabase direct connection. Switch to the **Session pooler on port 5432**.

**`password authentication failed`**

Use the database password, not your Supabase account password. Also check that special characters in the connection string are percent-encoded.

**`Telegram ... required` or no Setup test message arrives**

Check the secret names exactly, make sure you sent `/start` to the bot, and confirm that `TELEGRAM_CHAT_ID` belongs to the intended conversation.

**`matches > 0` but `notifications = 0`**

This is expected during baseline, when all jobs are old, or when new jobs do not pass the strong-match or freshness rules for immediate alerts. Matching jobs still appear in the Daily Summary. Use a manual backfill if you want individual alerts for baseline jobs.

**`skipped_reason: duplicate_run_key`**

The same key already completed successfully, or another run with that key is still active. This is normal for the backup schedule. Use a different key for a manual run.

**A fork never runs on schedule**

Enable workflows in the **Actions** tab. GitHub may also disable schedules in a public repository after a long period of inactivity; open **Actions → Job Radar TW → Enable workflow** to turn it back on.

**A company suddenly returns zero jobs or keeps failing**

Its ATS endpoint may have changed. Disable the source, then check it with `uv run monitor sources verify --company COMPANY_SLUG --status all`. Do not work around logins, CAPTCHA, or site access restrictions.

## Dashboard (optional)

You do not need the dashboard to receive Telegram messages. To view it locally, run:

```text
uv run monitor web
```

Then open `http://127.0.0.1:8080`. To publish a private dashboard, follow the [Cloudflare Pages deployment guide](docs/cloudflare-deploy.md). It uses the Supabase service-role key, so Cloudflare Access must be configured before the key is added. Do not put a production service-role key in Preview deployments.

## Project documentation

- [Functional specification](docs/spec.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [MIT License](LICENSE)
