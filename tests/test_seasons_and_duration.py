"""Seasonal services and travel duration (offline, network stubbed)."""

import zipfile
from datetime import date

import polars as pl
import pytest

from gtfstoosm.convert import OSMRelationBuilder
from gtfstoosm.frequencies import (
    ALL_DAYS,
    Band,
    format_duration,
    format_period,
    service_periods,
    service_tags,
)
from gtfstoosm.gtfs import GTFSFeed

SUMMER = (date(2026, 7, 4), date(2026, 8, 23))
WINTER = (date(2026, 12, 19), date(2027, 4, 5))


def calendar(rows):
    return pl.DataFrame(
        rows,
        schema=[
            "service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday", "start_date", "end_date",
        ],
        orient="row",
    )  # fmt: skip


EVERY_DAY = ("1",) * 7


class TestServicePeriods:
    def test_short_services_of_a_year_long_feed_are_seasonal(self):
        tables = {
            "calendar": calendar(
                [
                    ("year", *EVERY_DAY, "20260101", "20261231"),
                    ("summer", *EVERY_DAY, "20260704", "20260823"),
                ]
            )
        }
        assert service_periods(tables) == {"year": None, "summer": SUMMER}

    def test_a_short_feed_says_nothing_about_seasons(self):
        tables = {
            "calendar": calendar(
                [
                    ("a", *EVERY_DAY, "20260901", "20261130"),
                    ("b", *EVERY_DAY, "20261001", "20261015"),
                ]
            )
        }
        assert service_periods(tables) == {"a": None, "b": None}

    def test_no_calendar(self):
        assert service_periods({}) == {}


def test_format_period():
    assert format_period(SUMMER) == "Jul 04-Aug 23"
    assert format_period(WINTER) == "Dec 19-Apr 05"
    assert format_period(None) == ""


def test_format_duration():
    assert format_duration(31 * 60) == "00:31"
    assert format_duration(95 * 60) == "01:35"
    assert format_duration(10) == "00:01"


class TestServiceTagsWithSeasons:
    def test_seasonal_service_only(self):
        tags = service_tags([Band(ALL_DAYS, 9 * 3600, 18 * 3600, 1800, SUMMER)])
        assert tags == {
            "interval": "30",
            "opening_hours": "Jul 04-Aug 23 09:00-18:00",
        }

    def test_year_round_rule_comes_before_the_seasonal_one(self):
        tags = service_tags(
            [
                Band(ALL_DAYS, 9 * 3600, 18 * 3600, 1800, SUMMER),
                Band(frozenset(range(5)), 6 * 3600, 20 * 3600, 900),
            ]
        )
        assert tags["opening_hours"] == "Mo-Fr 06:00-20:00; Jul 04-Aug 23 09:00-18:00"
        assert tags["interval"] == "15"  # the year-round headway
        assert tags["interval:conditional"] == "30 @ (Jul 04-Aug 23 09:00-18:00)"

    def test_summer_and_winter_services(self):
        tags = service_tags(
            [
                Band(ALL_DAYS, 9 * 3600, 18 * 3600, 1800, SUMMER),
                Band(ALL_DAYS, 8 * 3600, 17 * 3600, 3600, WINTER),
            ]
        )
        assert tags["opening_hours"] == (
            "Jul 04-Aug 23 09:00-18:00; Dec 19-Apr 05 08:00-17:00"
        )


FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
    "A,Agence,https://example.org,Europe/Paris\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,44.5,6.2\nS2,Plage,44.6,6.3\n",
    "routes.txt": "route_id,route_short_name,route_type\nY,1,3\nS,2,3\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
    "sunday,start_date,end_date\n"
    "YEAR,1,1,1,1,1,0,0,20260101,20261231\n"
    "SUM,1,1,1,1,1,1,1,20260704,20260823\n",
}


@pytest.fixture
def feed_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
    )
    trips = ["route_id,service_id,trip_id"]
    times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]

    def add(route, service, trip, start, minutes):
        trips.append(f"{route},{service},{trip}")
        end = start + minutes
        for stop, clock, seq in (("S1", start, 1), ("S2", end, 2)):
            text = f"{clock // 60:02d}:{clock % 60:02d}:00"
            times.append(f"{trip},{text},{text},{stop},{seq}")

    for index in range(6):  # route Y: all year, every hour, 25 minutes on the road
        add("Y", "YEAR", f"Y{index}", 480 + 60 * index, 25)
    for index in range(6):  # route S: only in summer, every 30 minutes, 40 minutes
        add("S", "SUM", f"S{index}", 540 + 30 * index, 40)

    files = dict(FEED)
    files["trips.txt"] = "\n".join(trips) + "\n"
    files["stop_times.txt"] = "\n".join(times) + "\n"
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def build(feed_zip):
    feed = GTFSFeed(feed_dir=str(feed_zip))
    feed.load()
    builder = OSMRelationBuilder(frequency_tags=True)
    builder.build_relations(feed.tables)
    return {
        r.tags["ref"]: r.tags for r in builder.relations if r.tags["type"] == "route"
    }


class TestBuilder:
    def test_year_round_route(self, feed_zip):
        tags = build(feed_zip)["1"]
        assert tags["interval"] == "01:00"
        assert tags["opening_hours"] == "Mo-Fr 08:00-13:00"
        assert tags["duration"] == "00:25"

    def test_summer_route_gets_its_dates(self, feed_zip):
        tags = build(feed_zip)["2"]
        assert tags["interval"] == "30"
        assert tags["opening_hours"] == "Jul 04-Aug 23 09:00-11:30"
        assert tags["duration"] == "00:40"


class TestFeedInfo:
    """The validity announced in feed_info.txt tells how long the whole feed is."""

    TABLES = {
        "calendar": calendar([("summer", *EVERY_DAY, "20260704", "20260823")]),
    }

    def test_a_single_service_is_the_whole_feed_without_feed_info(self):
        assert service_periods(self.TABLES) == {"summer": None}

    def test_with_feed_info_it_is_seasonal(self):
        tables = dict(self.TABLES)
        tables["feed_info"] = pl.DataFrame(
            {"feed_start_date": ["20250901"], "feed_end_date": ["20260831"]}
        )
        assert service_periods(tables) == {"summer": SUMMER}

    def test_unreadable_feed_info_is_ignored(self):
        tables = dict(self.TABLES)
        tables["feed_info"] = pl.DataFrame(
            {"feed_start_date": [""], "feed_end_date": ["x"]}
        )
        assert service_periods(tables) == {"summer": None}
