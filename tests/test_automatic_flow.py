"""`gtfstoosm feed.zip`: everything is guessed from the feed."""

import zipfile

import pytest

from gtfstoosm import convert as convert_module
from gtfstoosm.cli import main
from gtfstoosm.convert import OSMRelationBuilder, convert_gtfs_to_osm
from gtfstoosm.gtfs import GTFSFeed
from gtfstoosm.utils import slugify
from tests.test_offline import LAT, build_osm, lon, write_feed


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("My Feed (2026)", "my-feed-2026"),
        ("Réseau TBM", "reseau-tbm"),
        ("chorges", "chorges"),
        ("  --  ", "feed"),
        ("", "feed"),
    ],
)
def test_slugify(text, slug):
    assert slugify(text) == slug


def agency_feed(tmp_path, agencies, routes):
    files = {
        "agency.txt": agencies,
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,A,44.5,6.2\nS2,B,44.6,6.3\n",
        "routes.txt": routes,
        "trips.txt": "route_id,service_id,trip_id\nR1,S,T1\nR2,S,T2\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\n"
        "T2,09:00:00,09:00:00,S1,1\nT2,09:10:00,09:10:00,S2,2\n",
    }
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    feed = GTFSFeed(feed_dir=str(path))
    feed.load()
    return feed.tables


@pytest.fixture
def no_osm(monkeypatch):
    monkeypatch.setattr(
        OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
    )


def tags_of(tables, **kwargs):
    builder = OSMRelationBuilder(**kwargs)
    builder.build_relations(tables)
    builder.build_route_masters(tables)
    return {(r.tags["type"], r.tags["ref"]): r.tags for r in builder.relations}


class TestAgencyTags:
    def test_single_agency(self, tmp_path, no_osm):
        tables = agency_feed(
            tmp_path,
            "agency_id,agency_name,agency_url,agency_timezone\nA,Alpes Bus,https://x.org,Europe/Paris\n",
            "route_id,route_short_name,route_type\nR1,1,3\nR2,2,3\n",
        )
        tags = tags_of(tables, agency_tags=True)
        for key in (("route", "1"), ("route_master", "1")):
            assert tags[key]["operator"] == "Alpes Bus"
            assert tags[key]["network"] == "Alpes Bus"

    def test_each_route_has_its_own_agency(self, tmp_path, no_osm):
        tables = agency_feed(
            tmp_path,
            "agency_id,agency_name,agency_url,agency_timezone\n"
            "A,Alpes Bus,https://x.org,Europe/Paris\nB,Lac Cars,https://y.org,Europe/Paris\n",
            "route_id,agency_id,route_short_name,route_type\nR1,A,1,3\nR2,B,2,3\n",
        )
        tags = tags_of(tables, agency_tags=True)
        assert tags[("route", "1")]["operator"] == "Alpes Bus"
        assert tags[("route", "2")]["operator"] == "Lac Cars"

    def test_given_tags_win(self, tmp_path, no_osm):
        tables = agency_feed(
            tmp_path,
            "agency_id,agency_name,agency_url,agency_timezone\nA,Alpes Bus,https://x.org,Europe/Paris\n",
            "route_id,route_short_name,route_type\nR1,1,3\nR2,2,3\n",
        )
        tags = tags_of(
            tables, agency_tags=True, relation_tags={"network": "Zou", "fee": "yes"}
        )
        assert tags[("route", "1")]["network"] == "Zou"
        assert tags[("route", "1")]["operator"] == "Alpes Bus"
        assert tags[("route", "1")]["fee"] == "yes"

    def test_off_by_default(self, tmp_path, no_osm):
        tables = agency_feed(
            tmp_path,
            "agency_id,agency_name,agency_url,agency_timezone\nA,Alpes Bus,https://x.org,Europe/Paris\n",
            "route_id,route_short_name,route_type\nR1,1,3\nR2,2,3\n",
        )
        assert "operator" not in tags_of(tables)[("route", "1")]


class TestAutomaticExtract:
    @pytest.fixture
    def prepared(self, tmp_path, monkeypatch):
        """prepare_extract replaced by a function that serves a local OSM file."""
        osm = build_osm(tmp_path / "town.osm")
        calls = []

        def fake(points, bbox, refresh=False, cache=None):
            calls.append({"points": points, "bbox": bbox, "refresh": refresh})
            return [str(osm)]

        monkeypatch.setattr(convert_module, "prepare_extract", fake)
        return calls

    def test_the_extract_is_prepared_from_the_served_stops(self, tmp_path, prepared):
        feed = write_feed(tmp_path / "feed.zip")
        out = tmp_path / "out.osc"
        convert_gtfs_to_osm(str(feed), str(out), auto_extract=True, refresh=True)

        [call] = prepared
        assert call["refresh"] is True
        assert sorted(call["points"]) == sorted(
            [(LAT, lon(1)), (LAT, lon(9)), (LAT + 0.002, lon(2)), (LAT + 0.002, lon(8))]
        )
        min_lat, min_lon, max_lat, max_lon = call["bbox"]
        assert min_lat < LAT < max_lat
        assert min_lon < lon(1) < lon(9) < max_lon
        assert '<member type="way" ref="100"' in out.read_text(encoding="utf8")

    def test_stops_that_no_trip_serves_do_not_widen_the_area(self, tmp_path, prepared):
        files = {
            "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,https://x.org,Europe/Paris\n",
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
            f"S1,Near,{LAT},{lon(1)}\nS2,Near2,{LAT},{lon(9)}\nFAR,Far,10.0,10.0\n",
            "routes.txt": "route_id,route_short_name,route_type\nR,1,3\n",
            "trips.txt": "route_id,service_id,trip_id\nR,S,T\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T,08:00:00,08:00:00,S1,1\nT,08:10:00,08:10:00,S2,2\n",
        }
        feed = tmp_path / "feed.zip"
        with zipfile.ZipFile(feed, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)

        convert_gtfs_to_osm(str(feed), str(tmp_path / "o.osc"), auto_extract=True)
        assert prepared[0]["bbox"][2] < 48  # max_lat is not pulled towards lat 10

    def test_without_the_option_nothing_is_prepared(
        self, tmp_path, prepared, monkeypatch
    ):
        monkeypatch.setattr(
            OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
        )
        feed = write_feed(tmp_path / "feed.zip", shapes=False)
        convert_gtfs_to_osm(str(feed), str(tmp_path / "o.osc"))
        assert prepared == []


class TestWholeFlow:
    def test_gtfstoosm_feed_zip(self, tmp_path, monkeypatch):
        osm = build_osm(tmp_path / "town.osm")
        monkeypatch.setattr(
            convert_module, "prepare_extract", lambda *args, **kwargs: [str(osm)]
        )
        feed = write_feed(tmp_path / "Town Bus.zip")

        assert main([str(feed)]) == 0

        out = tmp_path / "Town Bus.osm"
        content = out.read_text(encoding="utf8")
        assert 'k="gtfs:feed" v="town-bus"' in content
        assert 'k="operator" v="Agence"' in content
        assert "bus 1: Gare =&gt; Centre" in content
        assert '<member type="way" ref="100"' in content
        # the stops that OSM lacks are created
        assert 'k="name" v="Centre"' in content
