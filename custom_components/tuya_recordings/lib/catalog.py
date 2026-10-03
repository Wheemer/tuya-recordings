"""Bounded newest-first catalog scheduling, separate from image/video transfer."""

from datetime import date, datetime, timedelta

CATALOG_FAILURE_BACKOFFS = (
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
)


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def catalog_camera_available(camera: dict, now: datetime) -> bool:
    """Keep automatic camera work quiet after a catalog connection failure."""
    if camera.get("online") is False:
        return False
    retry_after = _timestamp(camera.get("catalogRetryAfter"))
    if retry_after is not None and retry_after > now:
        return False
    failed_at = _timestamp(camera.get("catalogErrorAt"))
    return failed_at is None or now - failed_at >= CATALOG_FAILURE_BACKOFFS[0]


def next_catalog_retry_after(camera: dict, now: datetime) -> tuple[int, datetime]:
    """Return the updated failure count and next automatic catalog retry time."""
    try:
        count = max(0, int(camera.get("catalogErrorCount") or 0)) + 1
    except (TypeError, ValueError):
        count = 1
    return count, now + CATALOG_FAILURE_BACKOFFS[min(count - 1, len(CATALOG_FAILURE_BACKOFFS) - 1)]


def catalog_queries(cameras: list[dict], *, today: date, now: datetime, days: int, limit: int = 2) -> list[tuple[str, date]]:
    """Prioritize discovery, then share time between current days and older gaps.

    Progress belongs to the caller's private catalog. No completed historical
    day is queried again here. Today's catalog is refreshed periodically, and
    a failed camera is left quiet without marking a day empty.
    """
    if days < 1 or limit < 1:
        return []
    unseen_today = []
    recent = []
    backlog = []
    for camera in cameras:
        dev_id = camera.get("devId")
        if not dev_id or not catalog_camera_available(camera, now):
            continue
        scanned = camera.get("catalogDays") or {}
        if not isinstance(scanned, dict):
            scanned = {}
        current = _timestamp(scanned.get(today.isoformat()))
        if current is None:
            unseen_today.append((dev_id, today))
        elif now - current >= timedelta(minutes=2) or current > now:
            recent.append((current, dev_id, today))
        for offset in range(1, days):
            day = today - timedelta(days=offset)
            scanned_at = _timestamp(scanned.get(day.isoformat()))
            # A scan made while this day was still current is not a final list.
            if scanned_at is None or scanned_at.date() <= day:
                backlog.append((dev_id, day))
    unseen_today.sort()
    selected = unseen_today[:limit]
    remaining = limit - len(selected)
    if remaining <= 0:
        return selected
    recent.sort()
    backlog.sort(key=lambda item: (-item[1].toordinal(), item[0]))
    # Reserve part of each maintenance pass for historical discovery.
    recent_count = min(len(recent), max(1, remaining // 2) if backlog else remaining)
    selected.extend((dev_id, day) for _, dev_id, day in recent[:recent_count])
    selected.extend(backlog[:limit - len(selected)])
    selected.extend((dev_id, day) for _, dev_id, day in recent[recent_count:recent_count + limit - len(selected)])
    return selected
