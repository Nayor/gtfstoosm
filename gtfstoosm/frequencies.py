"""Estimate the service tags of a route from its timetable.

OSM records how often a service runs with `interval=*` on the route relation, the
hours it runs with `opening_hours=*`, variations with `interval:conditional=*` and the
travel time with `duration=*`. These are rough estimates by design, so a route only
gets an interval when its timetable is regular enough (see MIN_DEPARTURES and
MAX_DISPERSION).

A service that runs during a small part of the feed's validity (a summer shuttle in
a feed valid for a year) is seasonal: its hours get a date range, such as
`Jul 04-Aug 23 Mo-Su 09:00-18:00`.
"""

import statistics
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from gtfstoosm.services import WEEKDAYS, parse_gtfs_date

MIN_FEED_DAYS = 300  # a feed shorter than this cannot tell a season from its coverage
SEASON_RATIO = (
    0.5  # a service is seasonal when it spans less than this share of the feed
)
MIN_DEPARTURES = 4  # departures needed to speak of a regular service
MAX_DISPERSION = 0.5  # median deviation of the headways, relative to their median

DAY_NAMES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # fmt: skip
ALL_DAYS = frozenset(range(7))

Period = tuple[date, date]


@dataclass(frozen=True)
class Band:
    """A service running on some days, between two times, with a headway."""

    days: frozenset[int]
    start: int  # seconds since midnight of the service day (may exceed 24h)
    end: int
    headway: int  # seconds
    period: Period | None = None  # first and last day, for seasonal services


@dataclass
class Timetable:
    """What the timetable says about each trip, looked up by trip_id."""

    trip_days: dict[str, frozenset[int]] = field(default_factory=dict)
    trip_periods: dict[str, Period | None] = field(default_factory=dict)
    first_departures: dict[str, int] = field(default_factory=dict)
    durations: dict[str, int] = field(default_factory=dict)


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


def service_periods(tables: dict[str, pl.DataFrame]) -> dict[str, Period | None]:
    """First and last day of the services that are seasonal, None for the others.

    A service is seasonal when calendar.txt gives it a period that is a small part of
    the period covered by the whole feed. When the feed itself is short, nothing can
    be told from the periods.
    """
    calendar = tables.get("calendar")
    if calendar is None:
        return {}

    periods = {
        row["service_id"]: (
            parse_gtfs_date(row["start_date"]),
            parse_gtfs_date(row["end_date"]),
        )
        for row in calendar.iter_rows(named=True)
    }
    if not periods:
        return {}

    feed_days = (
        max(end for _, end in periods.values())
        - min(start for start, _ in periods.values())
    ).days + 1
    if feed_days < MIN_FEED_DAYS:
        return dict.fromkeys(periods)

    return {
        service_id: period
        if (period[1] - period[0]).days + 1 <= SEASON_RATIO * feed_days
        else None
        for service_id, period in periods.items()
    }


def build_timetable(tables: dict[str, pl.DataFrame], trips: pl.DataFrame) -> Timetable:
    """Days, season, first departure and duration of each trip."""
    days_of_service = service_days(tables)
    periods_of_service = service_periods(tables)
    # Without any calendar nothing is known about the days: assume every day
    no_calendar = "calendar" not in tables and "calendar_dates" not in tables

    timetable = Timetable()
    for row in trips.select(["trip_id", "service_id"]).iter_rows(named=True):
        timetable.trip_days[row["trip_id"]] = (
            ALL_DAYS
            if no_calendar
            else days_of_service.get(row["service_id"], frozenset())
        )
        timetable.trip_periods[row["trip_id"]] = periods_of_service.get(
            row["service_id"]
        )

    ordered = tables["stop_times"].sort(["trip_id", "stop_sequence"])
    first = ordered.group_by("trip_id", maintain_order=True).first()
    last = ordered.group_by("trip_id", maintain_order=True).last()

    departures = {}
    for row in first.iter_rows(named=True):
        seconds = parse_time(row.get("departure_time"))
        if seconds is None:
            seconds = parse_time(row.get("arrival_time"))
        if seconds is not None:
            departures[row["trip_id"]] = seconds
    timetable.first_departures = departures

    for row in last.iter_rows(named=True):
        arrival = parse_time(row.get("arrival_time"))
        if arrival is None:
            arrival = parse_time(row.get("departure_time"))
        start = departures.get(row["trip_id"])
        if arrival is not None and start is not None and arrival > start:
            timetable.durations[row["trip_id"]] = arrival - start

    return timetable


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


def format_period(period: Period | None) -> str:
    """OSM opening_hours date range: "Jul 04-Aug 23"; empty without a period."""
    if period is None:
        return ""
    start, end = period
    return (
        f"{MONTH_NAMES[start.month - 1]} {start.day:02d}-"
        f"{MONTH_NAMES[end.month - 1]} {end.day:02d}"
    )


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


def format_duration(seconds: int) -> str:
    """OSM duration: HH:MM."""
    minutes = max(1, round(seconds / 60))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def timetable_bands(
    trip_ids: list[str],
    first_departures: dict[str, int],
    trip_days: dict[str, frozenset[int]],
    trip_periods: dict[str, Period | None] | None = None,
) -> list[Band]:
    """Bands of a pattern from the departures of its trips at the first stop.

    Trips are grouped by the days and the season they run; a group makes a band when
    it has enough departures and their headways are regular.
    """
    trip_periods = trip_periods or {}
    groups: dict[tuple[frozenset[int], Period | None], list[int]] = {}
    for trip_id in trip_ids:
        if trip_id in first_departures and trip_id in trip_days:
            key = (trip_days[trip_id], trip_periods.get(trip_id))
            groups.setdefault(key, []).append(first_departures[trip_id])

    bands: list[Band] = []
    for (days, period), departures in groups.items():
        times = sorted(set(departures))
        if len(times) < MIN_DEPARTURES or not days:
            continue
        headways = [b - a for a, b in zip(times, times[1:], strict=False)]
        median = statistics.median(headways)
        deviation = statistics.median(abs(h - median) for h in headways)
        if median <= 0 or deviation > MAX_DISPERSION * median:
            continue
        bands.append(Band(days, times[0], times[-1], round(median), period))
    return bands


def frequency_bands(
    trip_ids: list[str],
    frequencies: pl.DataFrame,
    trip_days: dict[str, frozenset[int]],
    trip_periods: dict[str, Period | None] | None = None,
) -> list[Band]:
    """Bands of a pattern from the rows of frequencies.txt of its trips."""
    trip_periods = trip_periods or {}
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
        bands.add(
            Band(
                trip_days[row["trip_id"]],
                start,
                end,
                headway,
                trip_periods.get(row["trip_id"]),
            )
        )
    return sorted(
        bands,
        key=lambda b: (b.period or (date.min, date.min), sorted(b.days), b.start),
    )


def _rule(
    band_period: Period | None, days: frozenset[int], start: int, end: int
) -> str:
    return " ".join(
        filter(
            None,
            [
                format_period(band_period),
                format_days(days),
                f"{format_clock(start)}-{format_clock(end)}",
            ],
        )
    )


def service_tags(bands: list[Band]) -> dict[str, str]:
    """interval, opening_hours and interval:conditional tags for the bands.

    Services that run all year are listed first, seasonal ones after them, as later
    rules of an opening_hours value take precedence.
    """
    if not bands:
        return {}

    # The headway of the year when there is one, else of the season
    year_round = [b for b in bands if b.period is None] or bands
    main = max(year_round, key=lambda b: (b.end - b.start, len(b.days)))
    main_interval = format_interval(main.headway)

    # Opening hours: first to last time per (season, days), same hours merged
    spans: dict[tuple[Period | None, frozenset[int]], tuple[int, int]] = {}
    for band in bands:
        key = (band.period, band.days)
        start, end = spans.get(key, (band.start, band.end))
        spans[key] = (min(start, band.start), max(end, band.end))

    merged: dict[tuple[Period | None, int, int], set[int]] = {}
    for (period, days), (start, end) in spans.items():
        merged.setdefault((period, start, end), set()).update(days)

    def order(item: tuple[tuple[Period | None, int, int], set[int]]):
        (period, start, _), days = item
        return (period is not None, period or (date.min, date.min), min(days), start)

    opening = [
        _rule(period, frozenset(days), start, end)
        for (period, start, end), days in sorted(merged.items(), key=order)
    ]

    # Other headways during their own times
    conditional: list[str] = []
    for band in bands:
        interval = format_interval(band.headway)
        if band is main or (interval == main_interval and band.period is None):
            continue
        entry = f"{interval} @ ({_rule(band.period, band.days, band.start, band.end)})"
        if entry not in conditional:
            conditional.append(entry)

    tags = {"interval": main_interval, "opening_hours": "; ".join(opening)}
    if conditional:
        tags["interval:conditional"] = "; ".join(conditional)
    return tags


def pattern_service_tags(
    trip_ids: list[str],
    tables: dict[str, pl.DataFrame],
    timetable: Timetable,
) -> dict[str, str]:
    """duration, interval, opening_hours and interval:conditional of a stop pattern.

    The pattern is the set of trips (trip_ids) that share the same stop sequence.
    """
    tags: dict[str, str] = {}

    durations = [timetable.durations[t] for t in trip_ids if t in timetable.durations]
    if durations:
        tags["duration"] = format_duration(round(statistics.median(durations)))

    frequencies = tables.get("frequencies")
    bands: list[Band] = []
    if frequencies is not None:
        bands = frequency_bands(
            trip_ids, frequencies, timetable.trip_days, timetable.trip_periods
        )
    if not bands:
        bands = timetable_bands(
            trip_ids,
            timetable.first_departures,
            timetable.trip_days,
            timetable.trip_periods,
        )
    tags.update(service_tags(bands))
    return tags
