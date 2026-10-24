"""Stops and ways read from a local OSM extract, with no network access."""

import zipfile

import polars as pl
import pytest
import requests

from gtfstoosm.cli import main
from gtfstoosm.convert import OSMRelationBuilder, feed_bbox
from gtfstoosm.gtfs import GTFSFeed
from gtfstoosm.osm_data import OsmExtract

LAT = 47.0


def lon(step: int) -> float:
    """Longitude of the nth node of the east-west roads (about 76 m apart)."""
    return 8.0 + step * 0.001


def build_osm(path):
    """A small town.

    - way 100: residential road along latitude 47.0, nodes 1..11 (lon 8.000-8.010)
    - way 102: its continuation, nodes 11..21 (lon 8.010-8.020)
    - way 101: a street crossing way 100 at node 6 (lon 8.005)
    - way 200: a tram line 220 m north, nodes 31..41
    - bus stop 501 near node 2, tram stop 502 on the tram line, a stop 503 far away
    """
    nodes = []
    for step in range(0, 21):
        nodes.append((1 + step, LAT, lon(step), ""))
    nodes.append((60, LAT + 0.005, lon(5), ""))
    nodes.append((61, LAT - 0.005, lon(5), ""))
    for step in range(0, 11):
        nodes.append((31 + step, LAT + 0.002, lon(step), ""))
    nodes.append(
        (501, LAT + 0.00005, lon(1), '<tag k="highway" v="bus_stop"/><tag k="name" v="Gare"/>')
    )  # fmt: skip
    nodes.append((502, LAT + 0.002, lon(2), '<tag k="railway" v="tram_stop"/>'))
    nodes.append((503, LAT + 0.5, lon(1), '<tag k="highway" v="bus_stop"/>'))

    def way(way_id, refs, tags):
        nds = "".join(f'<nd ref="{r}"/>' for r in refs)
        kv = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        return f'<way id="{way_id}" version="1">{nds}{kv}</way>'

    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
    # osmium-tool wants the objects ordered by type then id
    for node_id, lat, lng, tags in sorted(nodes):
        xml.append(
            f'<node id="{node_id}" version="1" lat="{lat}" lon="{lng}">{tags}</node>'
        )
    xml.append(way(100, range(1, 12), {"highway": "residential"}))
    xml.append(way(101, [60, 6, 61], {"highway": "residential"}))
    xml.append(way(102, range(11, 22), {"highway": "secondary"}))
    xml.append(way(200, range(31, 42), {"railway": "tram"}))
    xml.append("</osm>")
    path.write_text("\n".join(xml), encoding="utf8")
    return path


@pytest.fixture
def extract(tmp_path):
    return OsmExtract(str(build_osm(tmp_path / "town.osm")))


class TestStops:
    def test_bus_stop_is_found_for_buses_only(self, extract):
        near = [(LAT, lon(1))]
        assert [n.id for n in extract.stops_near(near, 10, "bus")] == [501]
        assert extract.stops_near(near, 10, "tram") == []

    def test_tram_stop_is_found_for_trams(self, extract):
        near = [(LAT + 0.002, lon(2))]
        assert [n.id for n in extract.stops_near(near, 10, "tram")] == [502]
        assert extract.stops_near(near, 10, "bus") == []

    def test_nothing_far_away(self, extract):
        assert extract.stops_near([(LAT + 0.2, lon(1))], 10, "bus") == []

    def test_bbox_drops_what_is_outside(self, tmp_path):
        path = build_osm(tmp_path / "town.osm")
        small = OsmExtract(str(path), bbox=(46.99, 7.99, 47.01, 8.03))
        assert small.stops_near([(LAT + 0.5, lon(1))], 10, "bus") == []
        assert [n.id for n in small.stops_near([(LAT, lon(1))], 10, "bus")] == [501]


class TestWays:
    def test_straight_shape(self, extract):
        shape = [(LAT, lon(0) + 0.0005), (LAT, lon(10) - 0.0005)]
        assert extract.match_ways(shape, "bus") == [100]

    def test_crossing_street_is_not_part_of_the_route(self, extract):
        shape = [(LAT, lon(0) + 0.0005), (LAT, lon(10) - 0.0005)]
        assert 101 not in extract.match_ways(shape, "bus")

    def test_following_several_ways(self, extract):
        shape = [(LAT, lon(1)), (LAT, lon(19))]
        assert extract.match_ways(shape, "bus") == [100, 102]

    def test_turning_into_the_crossing_street(self, extract):
        shape = [(LAT, lon(1)), (LAT, lon(5)), (LAT + 0.004, lon(5))]
        assert extract.match_ways(shape, "bus") == [100, 101]

    def test_tram_shape_follows_the_tram_line(self, extract):
        shape = [(LAT + 0.002, lon(1)), (LAT + 0.002, lon(9))]
        assert extract.match_ways(shape, "tram") == [200]

    def test_a_bus_does_not_follow_tram_tracks(self, extract):
        # away from the street that crosses the tram line at lon(5)
        shape = [(LAT + 0.002, lon(7)), (LAT + 0.002, lon(9))]
        assert extract.match_ways(shape, "bus") == []

    def test_a_street_crossed_by_the_shape_is_picked_up(self, extract):
        # known limit: a street the shape only crosses is matched for a few points
        shape = [(LAT + 0.002, lon(1)), (LAT + 0.002, lon(9))]
        assert extract.match_ways(shape, "bus") == [101]

    def test_shape_away_from_every_way(self, extract):
        assert extract.match_ways([(LAT + 0.3, 8.0), (LAT + 0.3, 8.01)], "bus") == []


def feed_files(shapes: bool = True):
    files = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
        "A,Agence,https://example.org,Europe/Zurich\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        f"S1,Gare,{LAT},{lon(1)}\nS2,Centre,{LAT},{lon(9)}\n"
        f"T1,Tram 1,{LAT + 0.002},{lon(2)}\nT2,Tram 2,{LAT + 0.002},{lon(8)}\n",
        "routes.txt": "route_id,route_short_name,route_type\nB,1,3\nT,2,0\n",
        "trips.txt": "route_id,service_id,trip_id,shape_id\nB,S,TB,SB\nT,S,TT,ST\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "TB,08:00:00,08:00:00,S1,1\nTB,08:10:00,08:10:00,S2,2\n"
        "TT,08:00:00,08:00:00,T1,1\nTT,08:10:00,08:10:00,T2,2\n",
        "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        f"SB,{LAT},{lon(1)},1\nSB,{LAT},{lon(9)},2\n"
        f"ST,{LAT + 0.002},{lon(2)},1\nST,{LAT + 0.002},{lon(8)},2\n",
    }
    if not shapes:
        files.pop("shapes.txt")
        files["trips.txt"] = "route_id,service_id,trip_id\nB,S,TB\nT,S,TT\n"
    return files


def write_feed(path, shapes: bool = True):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in feed_files(shapes).items():
            zf.writestr(name, content)
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the network must not be used with an OSM extract")

    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(requests, "get", fail)


def routes(builder):
    return {r.tags["ref"]: r for r in builder.relations if r.tags["type"] == "route"}


def convert(feed_zip, extract, **kwargs):
    feed = GTFSFeed(feed_dir=str(feed_zip))
    feed.load()
    builder = OSMRelationBuilder(osm_extract=extract, **kwargs)
    builder.build_relations(feed.tables)
    return builder


class TestBuilder:
    def test_stops_and_ways_come_from_the_extract(self, tmp_path, extract):
        builder = convert(write_feed(tmp_path / "feed.zip"), extract)

        bus = routes(builder)["1"]
        assert [m.ref for m in bus.members if m.role == "platform"][0] == 501
        assert [m.ref for m in bus.members if m.type == "way"] == [100]

    def test_rail_modes_get_their_ways_too(self, tmp_path, extract):
        builder = convert(write_feed(tmp_path / "feed.zip"), extract)

        tram = routes(builder)["2"]
        assert [m.ref for m in tram.members if m.role == "platform"][0] == 502
        assert [m.ref for m in tram.members if m.type == "way"] == [200]

    def test_missing_stops_are_created_with_the_mode_tags(self, tmp_path, extract):
        builder = convert(
            write_feed(tmp_path / "feed.zip"), extract, add_missing_stops=True
        )
        created = {s.tags["name"]: s.tags for s in builder.new_stops}
        assert created["Centre"]["highway"] == "bus_stop"
        assert created["Tram 2"]["railway"] == "platform"
        assert "Gare" not in created  # already in OSM

    def test_feed_without_shapes_still_gets_its_stops(self, tmp_path, extract):
        builder = convert(write_feed(tmp_path / "feed.zip", shapes=False), extract)
        bus = routes(builder)["1"]
        assert [m.ref for m in bus.members if m.role == "platform"][0] == 501
        assert not [m for m in bus.members if m.type == "way"]


def test_feed_bbox():
    stops = pl.DataFrame({"stop_lat": [47.0, 47.1], "stop_lon": [8.0, 8.2]})
    assert feed_bbox(stops, margin=0.01) == pytest.approx((46.99, 7.99, 47.11, 8.21))
    assert feed_bbox(pl.DataFrame({"stop_lat": [], "stop_lon": []})) is None


class TestCommandLine:
    def test_osm_file_option_end_to_end(self, tmp_path):
        osm = build_osm(tmp_path / "town.osm")
        feed = write_feed(tmp_path / "feed.zip")
        out = tmp_path / "out.osc"

        code = main(
            ["-i", str(feed), "-o", str(out), "--osm-file", str(osm), "--include-inactive"]
        )  # fmt: skip

        assert code == 0
        content = out.read_text(encoding="utf8")
        assert '<member type="way" ref="100"' in content
        assert '<member type="way" ref="200"' in content

    def test_missing_osm_file_is_refused(self, tmp_path):
        feed = write_feed(tmp_path / "feed.zip")
        assert main(["-i", str(feed), "-o", str(tmp_path / "o.osc"), "--osm-file", "nope.osm"]) == 1  # fmt: skip
