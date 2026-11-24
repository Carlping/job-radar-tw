# AGENTS.md

Instructions for coding agents that work in this repository.
Human contributors: read [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## What this project does

Job Radar TW reads public job postings from official company career sites
(Greenhouse, Lever, Ashby, SmartRecruiters, Workday, and pages with JSON-LD).
It scores each posting against the profiles in `config/`, stores the results in
Postgres (Supabase), and sends matches to Telegram. GitHub Actions runs it on a
schedule.

## Setup

```bash
uv sync --frozen --extra dev
```

Use Python 3.12 and `uv`. Do not install packages with `pip`.

## The one check command

```bash
uv run --frozen --extra dev python scripts/check.py
```

- Run this command before you report that a task is done. A task is done only
  when every check passes.
- It runs the same checks as CI: lockfile, lint, format, tests, and config
  validation.
- Add `--fix` to apply safe lint fixes and format the code. Then run the command
  again without `--fix`.
- Add `--with-dashboard` when you change `cloudflare/` or
  `src/job_monitor/web_static/`. This step needs Node.js.
- If a check fails, fix the cause. Do not skip, delete, or weaken a check or a
  test to make it pass. Do not edit `.github/workflows/` to hide a failure.

## Layout

| Path | Contents |
| --- | --- |
| `src/job_monitor/models.py` | Data types. It imports no other module of this package. |
| `src/job_monitor/sources.py`, `sitemap.py` | Fetch and parse each ATS source. |
| `src/job_monitor/matching.py`, `resume.py`, `llm.py` | Score postings against profiles. |
| `src/job_monitor/storage.py` | Database access. |
| `src/job_monitor/notifier.py` | Telegram messages. |
| `src/job_monitor/pipeline.py` | Connects sources, matching, storage, and notifier. |
| `src/job_monitor/cli.py` | The `monitor` command. |
| `src/job_monitor/web.py`, `web_static/` | Local dashboard. |
| `config/` | Public example configuration. |
| `migrations/` | SQL for the database schema. |
| `cloudflare/` | Optional hosted dashboard (Cloudflare Pages Functions). |
| `tests/` | Pytest tests. |

## Rules

- Do not commit secrets, `.env`, resumes, names, addresses, or job-search
  records. `config/resume.md` and `config/candidate.yml` are in `.gitignore`.
  Keep them there.
- Use only official, public job sources. Do not bypass login, CAPTCHA, rate
  limits, or other access controls.
- When you add or change a parser, add tests for a good response, an empty
  response, and an error response. Mock HTTP with `respx`, as in
  `tests/test_sources.py`.
- A new company in `config/companies.yml` keeps `enabled: false` and
  `source_verified: false` until a person verifies it with
  `uv run monitor sources verify --company SLUG --status all`.
- The weights of each profile in `config/profiles.yml` must add up to `1.0`.
  `validate-config` checks this.
- Do not add an `anon` write policy to the database. The migrations enable RLS.

## Pull requests

- Make one change per pull request.
- In the description, state the problem, the new behavior, and how you tested it.
- Say clearly if the change touches config, migrations, secrets, or the
  notification format.

## When you are blocked

If a check fails and you cannot find the cause, stop. Report the command and its
output. Do not guess, and do not change the check.
