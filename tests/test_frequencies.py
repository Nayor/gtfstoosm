"""interval/opening_hours tags estimated from the timetable (offline, network stubbed)."""

import zipfile

import polars as pl
import pytest

from gtfstoosm.cli import main
from gtfstoosm.convert import OSMRelationBuilder
from gtfstoosm.frequencies import (
    Band,
    format_clock,
    format_days,
    format_interval,
    frequency_bands,
    parse_time,
    service_days,
    service_tags,
    timetable_bands,
)
from gtfstoosm.gtfs import GTFSFeed

WEEKDAYS = frozenset(range(5))
WEEKEND = frozenset({5, 6})
ALL = frozenset(range(7))


class TestFormats:
    @pytest.mark.parametrize(
        ("value", "seconds"),
        [("08:30:00", 30600), ("25:10:00", 90600), ("00:00:00", 0), ("", None), (None, None), ("8h", None)],
    )  # fmt: skip
    def test_parse_time(self, value, seconds):
        assert parse_time(value) == seconds

    @pytest.mark.parametrize(
        ("days", "text"),
        [
            (ALL, ""),
            (WEEKDAYS, "Mo-Fr"),
            (WEEKEND, "Sa,Su"),
            (frozenset({0, 2, 4}), "Mo,We,Fr"),
            (frozenset({0, 1}), "Mo,Tu"),
            (frozenset({0, 1, 2, 5}), "Mo-We,Sa"),
        ],
    )
    def test_format_days(self, days, text):
        assert format_days(days) == text

    def test_format_clock(self):
        assert format_clock(6 * 3600 + 10 * 60) == "06:10"
        assert format_clock(25 * 3600 + 30 * 60) == "25:30"

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(600, "10"), (300, "5"), (30, "1"), (3540, "59"), (3600, "01:00"), (5400, "01:30")],
    )  # fmt: skip
    def test_format_interval(self, seconds, text):
        assert format_interval(seconds) == text


class TestServiceDays:
    def test_from_calendar_and_calendar_dates(self):
        tables = {
            "calendar": pl.DataFrame(
                {
                    "service_id": ["wk"],
                    "monday": ["1"],
                    "tuesday": ["1"],
                    "wednesday": ["1"],
                    "thursday": ["1"],
                    "friday": ["1"],
                    "saturday": ["0"],
                    "sunday": ["0"],
                }
            ),
            "calendar_dates": pl.DataFrame(
                {
                    "service_id": ["extra", "wk"],
                    # 2026-12-25 is a Friday, 2026-12-26 a Saturday
                    "date": ["20261225", "20261226"],
                    "exception_type": ["1", "1"],
                }
            ),
        }
        days = service_days(tables)
        assert days["wk"] == WEEKDAYS  # calendar wins
        assert days["extra"] == frozenset({4})


def departures(*minutes):
    return {f"T{i}": m * 60 for i, m in enumerate(minutes)}


class TestTimetableBands:
    def test_regular_service(self):
        times = departures(*range(360, 600, 20))  # every 20 minutes
        trip_days = dict.fromkeys(times, WEEKDAYS)
        [band] = timetable_bands(list(times), times, trip_days)
        assert band == Band(WEEKDAYS, 360 * 60, 580 * 60, 20 * 60)

    def test_too_few_departures(self):
        times = departures(360, 380, 400)
        assert timetable_bands(list(times), times, dict.fromkeys(times, ALL)) == []

    def test_irregular_service_gets_no_band(self):
        times = departures(360, 365, 480, 485, 700, 960)
        assert timetable_bands(list(times), times, dict.fromkeys(times, ALL)) == []

    def test_days_are_separate_bands(self):
        times = departures(*range(360, 600, 20), *range(480, 720, 60))
        trip_days = {
            trip: WEEKDAYS if index < 12 else WEEKEND
            for index, trip in enumerate(times)
        }
        bands = timetable_bands(list(times), times, trip_days)
        assert {b.days: b.headway for b in bands} == {
            WEEKDAYS: 20 * 60,
            WEEKEND: 60 * 60,
        }

    def test_trips_with_unknown_days_are_ignored(self):
        times = departures(*range(360, 600, 20))
        assert timetable_bands(list(times), times, {}) == []


class TestFrequencyBands:
    def test_rows_of_the_pattern_trips(self):
        frequencies = pl.DataFrame(
            {
                "trip_id": ["A", "A", "B"],
                "start_time": ["06:00:00", "09:00:00", "06:00:00"],
                "end_time": ["09:00:00", "18:00:00", "09:00:00"],
                "headway_secs": ["300", "600", "900"],
            }
        )
        bands = frequency_bands(["A"], frequencies, {"A": WEEKDAYS, "B": ALL})
        assert bands == [
            Band(WEEKDAYS, 6 * 3600, 9 * 3600, 300),
            Band(WEEKDAYS, 9 * 3600, 18 * 3600, 600),
        ]


class TestServiceTags:
    def test_nothing_without_bands(self):
        assert service_tags([]) == {}

    def test_single_band_every_day(self):
        tags = service_tags([Band(ALL, 6 * 3600, 22 * 3600, 600)])
        assert tags == {"interval": "10", "opening_hours": "06:00-22:00"}

    def test_weekdays_and_weekend(self):
        tags = service_tags(
            [
                Band(WEEKDAYS, 6 * 3600, 20 * 3600, 600),
                Band(WEEKEND, 8 * 3600, 18 * 3600, 1200),
            ]
        )
        assert tags["interval"] == "10"
        assert tags["opening_hours"] == "Mo-Fr 06:00-20:00; Sa,Su 08:00-18:00"
        assert tags["interval:conditional"] == "20 @ (Sa,Su 08:00-18:00)"

    def test_peak_and_off_peak(self):
        tags = service_tags(
            [
                Band(ALL, 6 * 3600, 9 * 3600, 300),
                Band(ALL, 9 * 3600, 18 * 3600, 600),
            ]
        )
        assert tags["interval"] == "10"  # the longest band
        assert tags["interval:conditional"] == "5 @ (06:00-09:00)"
        assert tags["opening_hours"] == "06:00-18:00"

    def test_same_hours_on_several_days_are_merged(self):
        tags = service_tags(
            [
                Band(frozenset({0, 1}), 6 * 3600, 20 * 3600, 600),
                Band(frozenset({2, 3}), 6 * 3600, 20 * 3600, 600),
            ]
        )
        assert tags["opening_hours"] == "Mo-Th 06:00-20:00"
        assert "interval:conditional" not in tags


FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
    "A,Agence,https://example.org,Europe/Paris\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,44.5,6.2\nS2,Ecoles,44.6,6.3\n",
    "routes.txt": "route_id,route_short_name,route_type\nR,1,3\nF,2,3\nI,3,3\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
    "sunday,start_date,end_date\nWK,1,1,1,1,1,0,0,20260101,20261231\n",
}


def timetable_feed(tmp_path, with_frequencies):
    trips = ["route_id,service_id,trip_id"]
    times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    for index in range(12):  # route R: every 20 minutes from 06:00
        start = 360 + 20 * index
        trip = f"R{index}"
        trips.append(f"R,WK,{trip}")
        for stop, delta, seq in (("S1", 0, 1), ("S2", 10, 2)):
            clock = f"{(start + delta) // 60:02d}:{(start + delta) % 60:02d}:00"
            times.append(f"{trip},{clock},{clock},{stop},{seq}")
    irregular = [360, 365, 480, 485, 700, 960]  # route I: no regular service
    for index, start in enumerate(irregular):
        trip = f"I{index}"
        trips.append(f"I,WK,{trip}")
        for stop, delta, seq in (("S1", 0, 1), ("S2", 10, 2)):
            clock = f"{(start + delta) // 60:02d}:{(start + delta) % 60:02d}:00"
            times.append(f"{trip},{clock},{clock},{stop},{seq}")
    files = dict(FEED)
    if with_frequencies:  # route F: one template trip run every 5 min then every 10
        trips.append("F,WK,FT")
        times += [
            "FT,00:00:00,00:00:00,S1,1",
            "FT,00:10:00,00:10:00,S2,2",
        ]
        files["frequencies.txt"] = (
            "trip_id,start_time,end_time,headway_secs\n"
            "FT,06:00:00,09:00:00,300\nFT,09:00:00,18:00:00,600\n"
        )
    files["trips.txt"] = "\n".join(trips) + "\n"
    files["stop_times.txt"] = "\n".join(times) + "\n"
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(
        OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
    )


def build(feed_zip, **kwargs):
    feed = GTFSFeed(feed_dir=str(feed_zip))
    feed.load()
    builder = OSMRelationBuilder(**kwargs)
    builder.build_relations(feed.tables)
    return {
        r.tags["ref"]: r.tags for r in builder.relations if r.tags["type"] == "route"
    }


class TestBuilder:
    def test_off_by_default(self, tmp_path):
        routes = build(timetable_feed(tmp_path, True))
        assert all("interval" not in tags for tags in routes.values())

    def test_interval_from_the_timetable(self, tmp_path):
        routes = build(timetable_feed(tmp_path, False), frequency_tags=True)
        assert routes["1"]["interval"] == "20"
        assert routes["1"]["opening_hours"] == "Mo-Fr 06:00-09:40"
        assert "interval:conditional" not in routes["1"]

    def test_irregular_service_gets_no_tags(self, tmp_path):
        routes = build(timetable_feed(tmp_path, False), frequency_tags=True)
        assert "interval" not in routes["3"]
        assert "opening_hours" not in routes["3"]

    def test_frequencies_txt(self, tmp_path):
        routes = build(timetable_feed(tmp_path, True), frequency_tags=True)
        assert routes["2"]["interval"] == "10"
        assert routes["2"]["interval:conditional"] == "5 @ (Mo-Fr 06:00-09:00)"
        assert routes["2"]["opening_hours"] == "Mo-Fr 06:00-18:00"

    def test_command_line_option(self, tmp_path):
        out = tmp_path / "out.osc"
        feed = timetable_feed(tmp_path, False)
        assert main(["-i", str(feed), "-o", str(out), "--include-inactive", "--online"]) == 0  # fmt: skip
        assert 'k="interval" v="20"' in out.read_text(encoding="utf8")
