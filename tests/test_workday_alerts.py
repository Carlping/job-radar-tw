import pytest

from job_monitor import pipeline
from job_monitor.config import SearchPreferences, Settings
from job_monitor.models import CompanyConfig
from job_monitor.sources import WorkdayBlocked
from job_monitor.storage import Storage


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeWorkdayRunner:
    blocked = True

    def __init__(self, client, max_concurrency):
        pass

    async def fetch(self, company):
        if self.blocked:
            raise WorkdayBlocked(429, "POST", "cloudflare_challenge")
        return []


def company(slug):
    return CompanyConfig(
        slug=slug,
        name=slug.upper(),
        careers_url=f"https://{slug}.wd1.myworkdayjobs.com/External",
        ats_type="workday",
        ats_config={
            "endpoint": f"https://{slug}.wd1.myworkdayjobs.com/wday/cxs/{slug}/External/jobs",
            "site": f"{slug}.wd1.myworkdayjobs.com",
        },
        industry="healthcare",
        profiles=["healthcare"],
        source_verified=True,
    )


@pytest.mark.asyncio
async def test_failure_alerts_aggregate_and_deduplicate_then_recover(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'source-alerts.db'}"
    Storage(database_url, create_schema=True)
    notifier = FakeNotifier()
    monkeypatch.setattr(pipeline, "SourceRunner", FakeWorkdayRunner)
    monkeypatch.setattr(pipeline, "TelegramNotifier", lambda *args, **kwargs: notifier)
    settings = Settings(
        database_url=database_url,
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )
    companies = [company("acme"), company("other")]
    prefs = SearchPreferences()

    FakeWorkdayRunner.blocked = True
    for run_number in range(1, 5):
        report = await pipeline.run_pipeline(
            settings, companies, {}, prefs, run_key=f"failure-{run_number}"
        )
        assert report.sources_succeeded == 0
        assert len(report.errors) == 2

    incident_alerts = [msg for msg in notifier.messages if "⚠️ Workday 來源連續失敗" in msg]
    assert len(incident_alerts) == 1
    assert "影響 2 家公司" in incident_alerts[0]
    assert "ACME" in incident_alerts[0]
    assert "OTHER" in incident_alerts[0]

    with Storage(database_url).engine.connect() as conn:
        # The new alert flag is stored in existing run history, without migrations.
        from job_monitor.storage import source_runs

        rows = list(conn.execute(source_runs.select()).mappings())
        assert len(rows) == 4
        flagged = [item for row in rows for item in row["errors"] if item.get("alert_sent")]
        assert len(flagged) == 2

    FakeWorkdayRunner.blocked = False
    recovered = await pipeline.run_pipeline(settings, companies, {}, prefs, run_key="recovery-1")
    assert recovered.sources_succeeded == 2
    recovery_alerts = [msg for msg in notifier.messages if "職缺來源已恢復" in msg]
    assert len(recovery_alerts) == 1
    assert "2 家" in recovery_alerts[0]

    await pipeline.run_pipeline(settings, companies, {}, prefs, run_key="recovery-2")
    assert len([msg for msg in notifier.messages if "職缺來源已恢復" in msg]) == 1
