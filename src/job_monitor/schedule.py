from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def local_run_key(
    now: datetime | None = None,
    *,
    timezone: str = "America/New_York",
) -> str:
    current = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    return f"daily-{current.date().isoformat()}"


def scheduled_run_key(
    now: datetime | None = None,
    *,
    timezone: str = "America/New_York",
    hour: int = 20,
    grace_hours: int = 16,
) -> str | None:
    scheduled_date = _scheduled_date(
        now,
        timezone=timezone,
        hour=hour,
        grace_hours=grace_hours,
    )
    if scheduled_date is None:
        return None
    return f"daily-{scheduled_date.isoformat()}"


def is_scheduled_window(
    now: datetime | None = None,
    *,
    timezone: str = "America/New_York",
    hour: int = 20,
    grace_hours: int = 16,
) -> bool:
    return (
        _scheduled_date(
            now,
            timezone=timezone,
            hour=hour,
            grace_hours=grace_hours,
        )
        is not None
    )


def _scheduled_date(
    now: datetime | None,
    *,
    timezone: str,
    hour: int,
    grace_hours: int,
):
    current = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if current < target:
        target -= timedelta(days=1)
    if current - target >= timedelta(hours=grace_hours):
        return None
    return target.date()
