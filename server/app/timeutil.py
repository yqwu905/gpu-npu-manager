from datetime import datetime, timezone


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite 读回的时间不带时区，统一视为 UTC。"""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)
