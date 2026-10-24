"""Estimate the service tags of a route (interval, opening_hours) from its timetable.

OSM records how often a service runs with `interval=*` on the route relation, the
hours it runs with `opening_hours=*`, and variations with `interval:conditional=*`.
These are rough estimates by design, so a route only gets them when its timetable is
regular enough (see MIN_DEPARTURES and MAX_DISPERSION).
"""

import statistics
from dataclasses import dataclass

import polars as pl

from gtfstoosm.services import WEEKDAYS, parse_gtfs_date

MIN_DEPARTURES = 4  # departures needed to speak of a regular service
MAX_DISPERSION = 0.5  # median deviation of the headways, relative to their median

DAY_NAMES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
ALL_DAYS = frozenset(range(7))


@dataclass(frozen=True)
class Band:
    """A service running on some days, between two times, with a headway."""

    days: frozenset[int]
    start: int  # seconds since midnight of the service day (may exceed 24h)
    end: int
    headway: int  # seconds


def parse_time(value: str | None) -> int | None:
    """Seconds in a GTFS time (HH:MM:SS, hours may exceed 24), None if empty."""
    if not value or not str(value).strip():
        return None
    try:
        hours, minutes, seconds = (int(part) for part in str(value).strip().split(":"))
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def service_days(tables: dict[str, pl.DataFrame]) -> dict[str, frozenset[int]]:
    """Weekdays (0=Monday) on which each service_id runs.

    From calendar.txt; services only listed in calendar_dates.txt get the weekdays of
    their added dates.
    """
    days: dict[str, frozenset[int]] = {}

    calendar = tables.get("calendar")
    if calendar is not None:
        for row in calendar.iter_rows(named=True):
            days[row["service_id"]] = frozenset(
                index
                for index, name in enumerate(WEEKDAYS)
                if str(row.get(name, "0")).strip() == "1"
            )

    calendar_dates = tables.get("calendar_dates")
    if calendar_dates is not None:
        added: dict[str, set[int]] = {}
        for row in calendar_dates.iter_rows(named=True):
            if str(row["exception_type"]).strip() == "1":
                added.setdefault(row["service_id"], set()).add(
                    parse_gtfs_date(row["date"]).weekday()
                )
        for service_id, weekdays in added.items():
            days.setdefault(service_id, frozenset(weekdays))

    return days


def format_days(days: frozenset[int]) -> str:
    """OSM opening_hours weekdays: "Mo-Fr", "Sa,Su"; empty when every day."""
    if days == ALL_DAYS:
        return ""
    parts: list[str] = []
    ordered = sorted(days)
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        if end - index >= 2:
            parts.append(f"{DAY_NAMES[ordered[index]]}-{DAY_NAMES[ordered[end]]}")
        else:
            parts.extend(DAY_NAMES[d] for d in ordered[index : end + 1])
        index = end + 1
    return ",".join(parts)


def format_clock(seconds: int) -> str:
    """HH:MM, hours may exceed 24 for services running past midnight."""
    minutes = round(seconds / 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_interval(seconds: int) -> str:
    """OSM interval: minutes under an hour, HH:MM from an hour."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return str(minutes)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def timetable_bands(
    trip_ids: list[str],
    first_departures: dict[str, int],
    trip_days: dict[str, frozenset[int]],
) -> list[Band]:
    """Bands of a pattern from the departures of its trips at the first stop.

    Trips are grouped by the days they run; a group makes a band when it has enough
    departures and their headways are regular.
    """
    by_days: dict[frozenset[int], list[int]] = {}
    for trip_id in trip_ids:
        if trip_id in first_departures and trip_id in trip_days:
            by_days.setdefault(trip_days[trip_id], []).append(first_departures[trip_id])

    bands: list[Band] = []
    for days, departures in by_days.items():
        times = sorted(set(departures))
        if len(times) < MIN_DEPARTURES or not days:
            continue
        headways = [b - a for a, b in zip(times, times[1:], strict=False)]
        median = statistics.median(headways)
        deviation = statistics.median(abs(h - median) for h in headways)
        if median <= 0 or deviation > MAX_DISPERSION * median:
            continue
        bands.append(Band(days, times[0], times[-1], round(median)))
    return bands


def frequency_bands(
    trip_ids: list[str],
    frequencies: pl.DataFrame,
    trip_days: dict[str, frozenset[int]],
) -> list[Band]:
    """Bands of a pattern from the rows of frequencies.txt of its trips."""
    wanted = set(trip_ids)
    bands: set[Band] = set()
    for row in frequencies.iter_rows(named=True):
        if row["trip_id"] not in wanted or row["trip_id"] not in trip_days:
            continue
        start = parse_time(row["start_time"])
        end = parse_time(row["end_time"])
        try:
            headway = int(row["headway_secs"])
        except (TypeError, ValueError):
            continue
        if (
            start is None
            or end is None
            or headway <= 0
            or not trip_days[row["trip_id"]]
        ):
            continue
        bands.add(Band(trip_days[row["trip_id"]], start, end, headway))
    return sorted(bands, key=lambda b: (sorted(b.days), b.start))


def service_tags(bands: list[Band]) -> dict[str, str]:
    """interval, opening_hours and interval:conditional tags for the bands."""
    if not bands:
        return {}

    main = max(bands, key=lambda b: (b.end - b.start, len(b.days)))
    main_interval = format_interval(main.headway)

    # Opening hours: first to last time per set of days, same hours merged
    hours: dict[tuple[int, int], set[int]] = {}
    spans: dict[frozenset[int], tuple[int, int]] = {}
    for band in bands:
        start, end = spans.get(band.days, (band.start, band.end))
        spans[band.days] = (min(start, band.start), max(end, band.end))
    for days, span in spans.items():
        hours.setdefault(span, set()).update(days)
    opening = []
    for (start, end), days in sorted(hours.items(), key=lambda kv: min(kv[1])):
        day_spec = format_days(frozenset(days))
        opening.append(
            " ".join(
                filter(None, [day_spec, f"{format_clock(start)}-{format_clock(end)}"])
            )
        )

    # Other headways during their own times
    conditional = []
    for band in bands:
        interval = format_interval(band.headway)
        if interval == main_interval or band is main:
            continue
        condition = " ".join(
            filter(
                None,
                [
                    format_days(band.days),
                    f"{format_clock(band.start)}-{format_clock(band.end)}",
                ],
            )
        )
        entry = f"{interval} @ ({condition})"
        if entry not in conditional:
            conditional.append(entry)

    tags = {"interval": main_interval, "opening_hours": "; ".join(opening)}
    if conditional:
        tags["interval:conditional"] = "; ".join(conditional)
    return tags
