"""Modes, closed lines and feeds without shapes (offline, network stubbed)."""

import zipfile
from datetime import date

import polars as pl
import pytest

from gtfstoosm.cli import main, parse_args
from gtfstoosm.convert import OSMRelationBuilder
from gtfstoosm.gtfs import GTFSFeed
from gtfstoosm.modes import new_stop_tags, osm_mode, stop_selectors
from gtfstoosm.services import active_service_ids


@pytest.mark.parametrize(
    ("route_type", "mode"),
    [
        (0, "tram"),
        (1, "subway"),
        (2, "train"),
        (3, "bus"),
        (4, "ferry"),
        (5, "tram"),
        (6, "aerialway"),
        (7, "funicular"),
        (11, "trolleybus"),
        (12, "monorail"),
        ("3", "bus"),
        (100, "train"),
        (106, "train"),
        (202, "bus"),
        (401, "subway"),
        (403, "light_rail"),
        (405, "monorail"),
        (700, "bus"),
        (800, "trolleybus"),
        (900, "tram"),
        (1000, "ferry"),
        (1200, "ferry"),
        (1301, "aerialway"),
        (1400, "funicular"),
        (1100, None),
        (1500, None),
        (1700, None),
        (99, None),
        ("", None),
        (None, None),
        ("x", None),
    ],
)
def test_osm_mode(route_type, mode):
    assert osm_mode(route_type) == mode


def test_new_stop_tags_follow_the_mode():
    assert new_stop_tags("bus") == {
        "public_transport": "platform",
        "highway": "bus_stop",
    }
    assert new_stop_tags("tram") == {
        "public_transport": "platform",
        "railway": "platform",
    }
    assert new_stop_tags("ferry") == {"public_transport": "platform"}


def test_stop_selectors_follow_the_mode():
    assert '["highway"="bus_stop"]' in stop_selectors("bus")
    assert any("tram_stop" in s for s in stop_selectors("tram"))
    assert any("ferry_terminal" in s for s in stop_selectors("ferry"))
    assert not any("bus_stop" in s for s in stop_selectors("train"))


CALENDAR_COLUMNS = [
    "service_id",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "start_date",
    "end_date",
]


def calendar(rows):
    return pl.DataFrame(rows, schema=CALENDAR_COLUMNS, orient="row")


EVERY_DAY = ("1", "1", "1", "1", "1", "1", "1")


class TestActiveServices:
    def test_no_calendar_means_no_information(self):
        assert active_service_ids({}, date(2026, 10, 24)) is None

    def test_ended_service_is_left_out(self):
        tables = {
            "calendar": calendar(
                [
                    ("old", *EVERY_DAY, "20240101", "20251231"),
                    ("now", *EVERY_DAY, "20260101", "20261231"),
                    ("next", *EVERY_DAY, "20270101", "20271231"),
                ]
            )
        }
        assert active_service_ids(tables, date(2026, 10, 24)) == {"now", "next"}

    def test_only_weekdays_matter(self):
        # 2026-10-24 is a Saturday; the service runs on Mondays and ends on Sunday
        tables = {
            "calendar": calendar(
                [("mon", "1", "0", "0", "0", "0", "0", "0", "20260101", "20261025")]
            )
        }
        assert active_service_ids(tables, date(2026, 10, 24)) == set()
        assert active_service_ids(tables, date(2026, 10, 19)) == {"mon"}

    def test_removed_days_cancel_the_service(self):
        tables = {
            "calendar": calendar(
                [("s", "0", "0", "0", "0", "0", "1", "0", "20260101", "20261024")]
            ),
            "calendar_dates": pl.DataFrame(
                {"service_id": ["s"], "date": ["20261024"], "exception_type": ["2"]}
            ),
        }
        assert active_service_ids(tables, date(2026, 10, 20)) == set()

    def test_added_days_count_without_calendar(self):
        tables = {
            "calendar_dates": pl.DataFrame(
                {
                    "service_id": ["past", "future"],
                    "date": ["20250101", "20261225"],
                    "exception_type": ["1", "1"],
                }
            )
        }
        assert active_service_ids(tables, date(2026, 10, 24)) == {"future"}


STOP_TIMES = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "".join(
    f"{trip},08:00:00,08:00:00,S1,1\n{trip},08:10:00,08:10:00,S2,2\n"
    for trip in ("T1", "T2", "T3", "T4")
)

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
    "A,Agence,https://example.org,Europe/Zurich\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
    "S1,Bahnhof,47.0,8.0\nS2,Dorf,47.1,8.1\n",
    "routes.txt": "route_id,route_short_name,route_type\n"
    "BUS1,1,700\nTRAM2,2,900\nOLD3,3,3\nPLANE,4,1100\n",
    "trips.txt": "route_id,service_id,trip_id\n"
    "BUS1,NOW,T1\nTRAM2,NOW,T2\nOLD3,OLD,T3\nPLANE,NOW,T4\n",
    "stop_times.txt": STOP_TIMES,
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
    "sunday,start_date,end_date\n"
    "NOW,1,1,1,1,1,1,1,20260101,20261231\nOLD,1,1,1,1,1,1,1,20240101,20241231\n",
}


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


@pytest.fixture
def feed_zip(tmp_path):
    return write_zip(tmp_path / "ch.zip", FEED)


@pytest.fixture
def calls(monkeypatch):
    """Stub the network and record how it is used."""
    record = {"stops": [], "ways": 0}

    def get_stop_objects(self, stops, add_missing_stops, max_distance=5, mode="bus"):
        record["stops"].append(mode)
        return []

    def get_route_ways(self, *args, **kwargs):
        record["ways"] += 1
        return [1, 2]

    monkeypatch.setattr(OSMRelationBuilder, "_get_stop_objects", get_stop_objects)
    monkeypatch.setattr(OSMRelationBuilder, "_get_route_ways", get_route_ways)
    return record


def build(feed_zip, **kwargs):
    feed = GTFSFeed(feed_dir=str(feed_zip))
    feed.load()
    builder = OSMRelationBuilder(**kwargs)
    builder.build_relations(feed.tables)
    builder.build_route_masters(feed.tables)
    return builder


def routes(builder):
    return {r.tags["ref"]: r for r in builder.relations if r.tags["type"] == "route"}


class TestFeedWithoutShapes:
    def test_converts_with_stops_only(self, feed_zip, calls):
        # the fixture feed has neither shapes.txt nor a shape_id column in trips.txt
        builder = build(feed_zip)
        assert set(routes(builder)) == {"1", "2", "3"}
        assert calls["ways"] == 0
        assert all(m.type != "way" for r in builder.relations for m in r.members)


class TestModes:
    def test_all_supported_modes_by_default(self, feed_zip, calls):
        builder = build(feed_zip)
        modes = {r.tags["ref"]: r.tags["route"] for r in routes(builder).values()}
        assert modes == {"1": "bus", "2": "tram", "3": "bus"}
        assert "4" not in modes  # air service is not mapped

    def test_modes_filter(self, feed_zip, calls):
        builder = build(feed_zip, modes=["tram"])
        assert set(routes(builder)) == {"2"}

    def test_route_master_has_the_mode_of_its_routes(self, feed_zip, calls):
        builder = build(feed_zip)
        masters = {
            r.tags["ref"]: r.tags["route_master"]
            for r in builder.relations
            if r.tags["type"] == "route_master"
        }
        assert masters["2"] == "tram"
        assert masters["1"] == "bus"

    def test_stops_are_looked_up_with_the_route_mode(self, feed_zip, calls):
        build(feed_zip, modes=["tram"])
        assert calls["stops"] == ["tram"]

    def test_ways_only_for_road_modes(self, tmp_path, calls):
        files = dict(FEED)
        files["trips.txt"] = (
            "route_id,service_id,trip_id,shape_id\n"
            "BUS1,NOW,T1,SH\nTRAM2,NOW,T2,SH\nOLD3,NOW,T3,SH\nPLANE,NOW,T4,SH\n"
        )
        files["shapes.txt"] = (
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
            "SH,47.0,8.0,1\nSH,47.1,8.1,2\n"
        )
        builder = build(write_zip(tmp_path / "with_shapes.zip", files))

        # routes 1 and 3 are buses and follow roads, route 2 is a tram
        assert calls["ways"] == 2
        assert any(m.type == "way" for m in routes(builder)["1"].members)
        assert not any(m.type == "way" for m in routes(builder)["2"].members)


class TestClosedLines:
    def test_inactive_routes_are_left_out(self, feed_zip, calls):
        builder = build(feed_zip, active_on=date(2026, 10, 24))
        assert set(routes(builder)) == {"1", "2"}

    def test_no_filter_by_default_in_the_library(self, feed_zip, calls):
        assert "3" in routes(build(feed_zip))


class TestCommandLine:
    def test_defaults(self):
        args = parse_args(["-i", "a.zip", "-o", "b.osc"])
        assert args.modes is None
        assert args.active_on is None
        assert args.include_inactive is False

    def test_options(self):
        args = parse_args(
            [
                "-i",
                "a.zip",
                "-o",
                "b.osc",
                "--modes",
                "tram",
                "train",
                "--active-on",
                "2026-10-24",
                "--include-inactive",
            ]
        )
        assert args.modes == ["tram", "train"]
        assert args.active_on == date(2026, 10, 24)
        assert args.include_inactive is True

    def test_unknown_mode_is_refused(self):
        with pytest.raises(SystemExit):
            parse_args(["-i", "a.zip", "-o", "b.osc", "--modes", "plane"])

    def test_bad_date_is_refused(self):
        with pytest.raises(SystemExit):
            parse_args(["-i", "a.zip", "-o", "b.osc", "--active-on", "24/10/2026"])

    def test_closed_lines_are_left_out_unless_asked(self, feed_zip, tmp_path, calls):
        out = tmp_path / "out.osc"
        args = ["-i", str(feed_zip), "-o", str(out)]

        assert main([*args, "--active-on", "2026-10-24"]) == 0
        assert 'k="ref" v="1"' in out.read_text(encoding="utf8")
        assert 'k="ref" v="3"' not in out.read_text(encoding="utf8")

        assert main([*args, "--include-inactive"]) == 0
        assert 'k="ref" v="3"' in out.read_text(encoding="utf8")
