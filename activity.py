"""Calendar statistics and current attendance from one read-only snapshot.

Periods begin at Moscow midnight, Monday midnight or the first day of the
month, and end at ``snapshot.generated_at``. Visits and unique guests count
arrivals in that period. Time includes the clipped overlap of *all* visits,
including visits that began earlier, and active visits stop at the snapshot
instant. Different database sources keep distinct guest identities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from reporting import ReportGuest, ReportSnapshot
from storage import normalize_name


# Match the bot's existing Moscow display zone; modern Moscow has no DST.
MOSCOW = timezone(timedelta(hours=3))
_LABELS = {"day": "За день", "week": "За неделю", "month": "За месяц"}


@dataclass(frozen=True, slots=True)
class RankedGuest:
    row: ReportGuest
    visit_count: int
    total_seconds: int


@dataclass(frozen=True, slots=True)
class PeriodStats:
    period: str
    label: str
    start: datetime
    end: datetime
    visit_count: int
    unique_guests: int
    total_seconds: int
    overlap_guests: int
    top: tuple[RankedGuest, ...]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Время должно содержать часовой пояс.")
    return value.astimezone(timezone.utc)


def _timestamp(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _period_start(now: datetime, period: str) -> datetime:
    if period not in _LABELS:
        raise ValueError("Неизвестный период статистики.")
    local = now.astimezone(MOSCOW)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
    elif period == "month":
        start = start.replace(day=1)
    return start.astimezone(timezone.utc)


def calculate_stats(snapshot: ReportSnapshot, period: str = "day") -> PeriodStats:
    """Return start-based counts, elapsed overlap, and the five most frequent guests.

    ``start`` and ``end`` are aware UTC timestamps. Both arrival boundaries are
    inclusive, so a visit started exactly at the snapshot instant counts with
    zero elapsed time. Future arrivals contribute nothing. Each visit's
    elapsed time is rounded down to whole seconds, matching stored visit
    summaries. Ranking uses arrival count, then total time in this period,
    then normalized name, source number and guest ID for stable ties.

    ``overlap_guests`` also includes guests whose earlier visit extends into
    the period; it can therefore exceed the arrival-based ``unique_guests``.
    The input snapshot and its guest/visit objects are never changed.
    """
    end = _utc(snapshot.generated_at)
    start = _period_start(end, period)
    ranked: list[RankedGuest] = []
    total_seconds = 0
    visit_count = 0
    overlap_guests = 0
    for row in snapshot.rows:
        arrivals = 0
        elapsed = 0
        overlaps = False
        for visit in row.visits:
            began = _timestamp(visit.started_at)
            if began > end:
                continue
            if began >= start:
                arrivals += 1
            ended = _timestamp(visit.stopped_at) if visit.stopped_at else end
            clipped_start = max(start, began)
            clipped_end = min(end, ended)
            if clipped_end > clipped_start:
                overlaps = True
                elapsed += int((clipped_end - clipped_start).total_seconds())
        visit_count += arrivals
        total_seconds += elapsed
        overlap_guests += int(overlaps)
        if arrivals:
            ranked.append(RankedGuest(row, arrivals, elapsed))
    ranked.sort(key=lambda item: (-item.visit_count, -item.total_seconds,
                                 normalize_name(item.row.guest.name),
                                 item.row.source_index, item.row.guest.id))
    return PeriodStats(period, _LABELS[period], start, end, visit_count,
                       len(ranked), total_seconds, overlap_guests, tuple(ranked[:5]))


def active_guests(snapshot: ReportSnapshot) -> tuple[ReportGuest, ...]:
    """Return all guests with an active, already started visit, oldest first.

    Source number and guest ID provide stable ties. Visits dated after the
    snapshot instant are excluded until their start time is reached.
    """
    now = _utc(snapshot.generated_at)
    result = [row for row in snapshot.rows
              if row.summary.active is not None
              and _timestamp(row.summary.active.started_at) <= now]
    result.sort(key=lambda row: (_timestamp(row.summary.active.started_at),
                                row.source_index, row.guest.id))
    return tuple(result)
