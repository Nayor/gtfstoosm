"""Which GTFS services (service_id) still run: used to leave out closed lines."""

import logging
from datetime import date, datetime, timedelta

import polars as pl

logger = logging.getLogger(__name__)

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def parse_gtfs_date(value: str) -> date:
    """Parse a GTFS date (YYYYMMDD)."""
    return datetime.strptime(str(value).strip(), "%Y%m%d").date()


def active_service_ids(tables: dict[str, pl.DataFrame], on: date) -> set[str] | None:
    """Return the service_ids that run on `on` or on a later day.

    calendar.txt gives the regular days of a service and calendar_dates.txt adds
    or removes single days. Returns None when the feed has neither file: nothing
    can be said about its services, so they should not be filtered.
    """
    calendar = tables.get("calendar")
    calendar_dates = tables.get("calendar_dates")
    if calendar is None and calendar_dates is None:
        return None

    added: dict[str, set[date]] = {}
    removed: dict[str, set[date]] = {}
    if calendar_dates is not None:
        for row in calendar_dates.iter_rows(named=True):
            day = parse_gtfs_date(row["date"])
            kind = str(row["exception_type"]).strip()
            target = added if kind == "1" else removed if kind == "2" else None
            if target is not None:
                target.setdefault(row["service_id"], set()).add(day)

    active: set[str] = set()

    if calendar is not None:
        for row in calendar.iter_rows(named=True):
            service_id = row["service_id"]
            start = max(parse_gtfs_date(row["start_date"]), on)
            end = parse_gtfs_date(row["end_date"])
            running_days = {
                index
                for index, name in enumerate(WEEKDAYS)
                if str(row.get(name, "0")).strip() == "1"
            }
            if not running_days:
                continue
            day = start
            while day <= end:
                if day.weekday() in running_days and day not in removed.get(
                    service_id, ()
                ):
                    active.add(service_id)
                    break
                day += timedelta(days=1)

    for service_id, days in added.items():
        if any(day >= on for day in days):
            active.add(service_id)

    return active
