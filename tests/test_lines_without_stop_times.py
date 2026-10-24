"""Routes whose trips have no stop_times are imported from their shape."""

import zipfile

import pytest

from gtfstoosm.convert import OSMRelationBuilder
from gtfstoosm.gtfs import GTFSFeed
from gtfstoosm.osm_data import OsmExtract
from tests.test_offline import LAT, build_osm, lon

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
    "A,Agence,https://example.org,Europe/Zurich\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,47.0,8.001\n",
    "routes.txt": "route_id,route_short_name,route_type\n"
    "B,1,3\nT,2,0\nN,3,3\nE,4,3\nM,5,3\n",
    # B and T have a shape, N has no shape, E has no trip, M is complete
    "trips.txt": "route_id,service_id,trip_id,shape_id\n"
    "B,S,TB,SB\nT,S,TT,ST\nN,S,TN,\nM,S,TM,SB\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "TM,08:00:00,08:00:00,S1,1\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
    f"SB,{LAT},{lon(1)},1\nSB,{LAT},{lon(9)},2\n"
    f"ST,{LAT + 0.002},{lon(2)},1\nST,{LAT + 0.002},{lon(8)},2\n",
}


@pytest.fixture
def feed(tmp_path):
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in FEED.items():
            zf.writestr(name, content)
    loaded = GTFSFeed(feed_dir=str(path))
    loaded.load()
    return loaded.tables


def routes(builder):
    return {r.tags["ref"]: r for r in builder.relations if r.tags["type"] == "route"}


@pytest.fixture
def extract(tmp_path):
    return OsmExtract(str(build_osm(tmp_path / "town.osm")))


class TestWithAnExtract:
    def test_ways_only_relations(self, feed, extract):
        builder = OSMRelationBuilder(osm_extract=extract)
        builder.build_relations(feed)
        found = routes(builder)

        bus = found["1"]
        assert [m.ref for m in bus.members] == [100]
        assert "fixme" in bus.tags
        assert [m.ref for m in found["2"].members] == [200]  # a tram too

    def test_complete_routes_have_no_fixme(self, feed, extract):
        builder = OSMRelationBuilder(osm_extract=extract)
        builder.build_relations(feed)
        assert "fixme" not in routes(builder)["5"].tags

    def test_route_without_shape_or_trip_is_left_out(self, feed, extract, caplog):
        builder = OSMRelationBuilder(osm_extract=extract)
        with caplog.at_level("WARNING"):
            builder.build_relations(feed)
        found = routes(builder)
        assert "3" not in found
        assert "4" not in found
        assert "neither stop_times nor a shape" in caplog.text
        assert "no trip at all" in caplog.text


class TestOnline:
    @pytest.fixture(autouse=True)
    def stubs(self, monkeypatch):
        monkeypatch.setattr(
            OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
        )
        monkeypatch.setattr(
            OSMRelationBuilder, "_get_route_ways", lambda self, *a, **k: [7, 8]
        )

    def test_bus_gets_ways_but_a_tram_has_nothing_to_import(self, feed, caplog):
        builder = OSMRelationBuilder()
        with caplog.at_level("WARNING"):
            builder.build_relations(feed)
        found = routes(builder)
        assert [m.ref for m in found["1"].members] == [7, 8]
        assert "2" not in found  # Valhalla only follows roads
        assert "no stop_times and no way found" in caplog.text
