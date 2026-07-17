from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

RIYADH = ZoneInfo("Asia/Riyadh")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def riyadh_now() -> datetime:
    return datetime.now(RIYADH)


def riyadh_today() -> date:
    return riyadh_now().date()


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat(timespec="milliseconds").replace("+00:00", "Z")
