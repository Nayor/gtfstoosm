"""The table of lines: scores, shared new stops, JOSM searches and the CSV."""

import csv
import zipfile

import pytest

from gtfstoosm.compare import RouteMatch
from gtfstoosm.convert import OSMRelationBuilder, convert_gtfs_to_osm
from gtfstoosm.osm import OSMRelation
from gtfstoosm.osm_data import ExistingRelation
from gtfstoosm.report import (
    RouteFacts,
    level_of,
    line_table,
    score_route,
    write_csv,
)
from tests.test_offline import LAT, build_osm, lon


class TestScore:
    def test_a_clean_route(self):
        facts = RouteFacts(stops=5, coverage=0.99)
        assert score_route("new", facts) == (100, [])

    @pytest.mark.parametrize(
        ("facts", "status", "expected"),
        [
            (RouteFacts(coverage=0.9), "new", 85),
            (RouteFacts(coverage=0.7), "new", 70),
            (RouteFacts(coverage=None), "new", 95),
            (RouteFacts(has_shape=False), "new", 90),
            (RouteFacts(coverage=1.0, no_stop_times=True), "new", 70),
            (RouteFacts(coverage=1.0, max_distance=15), "new", 90),
            (RouteFacts(coverage=1.0, max_distance=25), "new", 80),
            (RouteFacts(coverage=1.0, name_mismatches=2), "new", 90),
            (RouteFacts(coverage=1.0, missing_stops=2), "new", 80),
            (RouteFacts(coverage=1.0), "different", 60),
            (RouteFacts(coverage=1.0), "ambiguous", 50),
        ],
    )
    def test_penalties(self, facts, status, expected):
        score, reasons = score_route(status, facts)
        assert score == expected
        assert reasons

    def test_found_without_the_link_tags(self):
        assert score_route("link", RouteFacts(coverage=1.0), linked=False)[0] == 90
        assert score_route("link", RouteFacts(coverage=1.0), linked=True)[0] == 100

    def test_the_score_does_not_go_below_zero(self):
        facts = RouteFacts(
            coverage=0.1, no_stop_times=True, missing_stops=9, max_distance=50
        )
        assert score_route("ambiguous", facts)[0] == 0

    def test_levels(self):
        assert level_of(100) == level_of(85) == "sure"
        assert level_of(84) == level_of(60) == "check"
        assert level_of(59) == "difficult"


def route(relation_id, ref, stops, route_id=None, name=None):
    relation = OSMRelation(
        id=relation_id,
        tags={
            "type": "route",
            "route": "bus",
            "ref": ref,
            "name": name or f"bus {ref}: A => B",
            "gtfs:feed": "town",
            "gtfs:route_id": route_id or f"R{ref}",
        },
    )
    for stop in stops:
        relation.add_member("node", stop, "platform")
    return relation


def match(relation, status="new", existing=None):
    return RouteMatch(relation=relation, status=status, existing=existing)


class TestLineTable:
    def test_variants_of_a_line_are_one_row(self):
        a, b = route(-1, "1", [10, 11]), route(-2, "1", [11, 10])
        rows = line_table([match(a), match(b)], {}, "town")
        assert len(rows) == 1
        assert rows[0].variants == 2
        assert rows[0].statuses == {"new": 2}
        assert rows[0].stops == 2

    def test_the_score_of_a_line_is_that_of_its_worst_variant(self):
        a, b = route(-1, "1", [10, 11]), route(-2, "1", [11, 10])
        facts = {-1: RouteFacts(coverage=1.0), -2: RouteFacts(coverage=0.5)}
        [row] = line_table([match(a), match(b)], facts, "town")
        assert row.score == 70
        assert row.level == "check"
        assert any("50%" in reason for reason in row.reasons)

    def test_best_lines_first(self):
        good, bad = route(-1, "9", [10]), route(-2, "1", [11])
        facts = {-1: RouteFacts(coverage=1.0), -2: RouteFacts(coverage=0.5)}
        rows = line_table([match(bad), match(good)], facts, "town")
        assert [r.ref for r in rows] == ["9", "1"]

    def test_new_stops_shared_by_lines(self):
        # stop -5 is new and on lines 1 and 2; stop -6 only on line 3
        one, two = route(-1, "1", [10, -5]), route(-2, "2", [-5, 12])
        three = route(-3, "3", [-6, 13])
        rows = {
            r.ref: r
            for r in line_table([match(one), match(two), match(three)], {}, "town")
        }
        assert rows["1"].new_stops == 1 and rows["1"].shared_with == ["2"]
        assert rows["2"].shared_with == ["1"]
        assert rows["3"].new_stops == 1 and rows["3"].shared_with == []

    def test_stops_of_a_route_that_is_not_created_do_not_count(self):
        one = route(-1, "1", [10, -5])
        two = route(-2, "2", [-5, 12])
        existing = ExistingRelation(900, 2, {"type": "route"}, [])
        rows = {
            r.ref: r
            for r in line_table(
                [match(one), match(two, "different", existing)], {}, "town"
            )
        }
        assert rows["1"].shared_with == []
        assert rows["2"].new_stops == 0

    def test_josm_search_uses_the_link_with_the_feed(self):
        [row] = line_table([match(route(-1, "1", [10]))], {}, "town")
        assert row.search == 'type:relation "gtfs:feed"="town" "gtfs:route_id"="R1"'

    def test_josm_search_without_a_feed_id(self):
        relation = route(-1, "1", [10])
        del relation.tags["gtfs:feed"], relation.tags["gtfs:route_id"]
        [row] = line_table([match(relation)], {}, None)
        assert row.search == 'type:relation new ref="1"'

    def test_quotes_are_escaped_in_the_search(self):
        relation = route(-1, "1", [10], route_id='A"B')
        [row] = line_table([match(relation)], {}, "town")
        assert '"gtfs:route_id"="A\\"B"' in row.search

    def test_found_through_the_link_or_not(self):
        linked = ExistingRelation(900, 2, {"gtfs:feed": "town"}, [])
        loose = ExistingRelation(901, 2, {}, [])
        facts = {-1: RouteFacts(coverage=1.0), -2: RouteFacts(coverage=1.0)}
        rows = {
            r.ref: r
            for r in line_table(
                [
                    match(route(-1, "1", [10]), "link", linked),
                    match(route(-2, "2", [11]), "link", loose),
                ],
                facts,
                "town",
            )
        }
        assert rows["1"].score == 100
        assert rows["2"].score == 90


def test_csv(tmp_path):
    one = route(-1, "1", [10, -5], name="bus 1: Café => Gare")
    two = route(-2, "2", [-5, 12])
    rows = line_table([match(one), match(two)], {}, "town")
    out = tmp_path / "lines.csv"
    write_csv(rows, str(out))

    with open(out, encoding="utf-8-sig", newline="") as f:
        table = list(csv.DictReader(f, delimiter=";"))
    assert [r["ref"] for r in table] == ["1", "2"]
    assert table[0]["name"] == "bus 1: Café => Gare"
    assert table[0]["shared_with"] == "2"
    assert table[0]["new_stops"] == "1"
    assert table[0]["josm_search"].startswith("type:relation")


FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
    "A,Agence,https://example.org,Europe/Zurich\n",
    # S1 has a stop in OSM (501); S2 and S3 have none
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
    f"S1,Gare,{LAT},{lon(1)}\nS2,Centre,{LAT},{lon(9)}\nS3,Ecole,{LAT},{lon(5)}\n",
    "routes.txt": "route_id,route_short_name,route_type\nB1,1,3\nB2,2,3\n",
    "trips.txt": "route_id,service_id,trip_id,shape_id\nB1,S,T1,SH1\nB2,S,T2,SH2\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\n"
    "T2,09:00:00,09:00:00,S3,1\nT2,09:10:00,09:10:00,S2,2\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
    f"SH1,{LAT},{lon(1)},1\nSH1,{LAT},{lon(9)},2\n"
    f"SH2,{LAT},{lon(5)},1\nSH2,{LAT},{lon(9)},2\n",
}


@pytest.fixture
def feed(tmp_path):
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in FEED.items():
            zf.writestr(name, content)
    return path


@pytest.fixture
def osm(tmp_path):
    return build_osm(tmp_path / "town.osm")


def read_table(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["ref"]: r for r in csv.DictReader(f, delimiter=";")}


class TestWholeRun:
    def test_the_table_is_written_next_to_the_result(self, feed, osm, tmp_path):
        out = tmp_path / "out.osm"
        convert_gtfs_to_osm(
            str(feed),
            str(out),
            osm_file=str(osm),
            output_format="osm",
            add_missing_stops=True,
        )
        rows = read_table(tmp_path / "out.lines.csv")
        assert set(rows) == {"1", "2"}
        # the two lines share the new stop at the Centre (S2)
        assert rows["1"]["shared_with"] == "2"
        assert rows["2"]["shared_with"] == "1"
        assert rows["1"]["new_stops"] == "1"
        assert rows["2"]["new_stops"] == "2"  # S3 and S2

    def test_ways_found_for_the_whole_shape_do_not_cost_points(
        self, feed, osm, tmp_path
    ):
        convert_gtfs_to_osm(
            str(feed),
            str(tmp_path / "o.osm"),
            osm_file=str(osm),
            output_format="osm",
            add_missing_stops=True,
        )
        assert read_table(tmp_path / "o.lines.csv")["1"]["reasons"] == ""

    def test_only_routes_uses_the_stops_osm_has(self, feed, osm, tmp_path):
        out = tmp_path / "routes.osm"
        convert_gtfs_to_osm(
            str(feed), str(out), osm_file=str(osm), output_format="osm",
            add_missing_stops=False, link_stops=False, feed_id="town",
        )  # fmt: skip
        text = out.read_text(encoding="utf-8")
        assert 'ref="501"' in text  # the stop that OSM has is a member
        assert "<node " not in text  # nothing created or modified among the stops
        row = read_table(tmp_path / "routes.lines.csv")["1"]
        assert "not in OSM left out" in row["reasons"]
        assert int(row["score"]) < 100


def test_the_builder_keeps_facts_for_each_route(feed, osm):
    from gtfstoosm.gtfs import GTFSFeed
    from gtfstoosm.osm_data import OsmExtract

    loaded = GTFSFeed(feed_dir=str(feed))
    loaded.load()
    builder = OSMRelationBuilder(
        osm_extract=OsmExtract(str(osm)), add_missing_stops=True
    )
    builder.build_relations(loaded.tables)
    facts = {r.tags["ref"]: builder.facts[r.id] for r in builder.relations}
    assert facts["1"].coverage == pytest.approx(1.0, abs=0.05)
    assert facts["1"].max_distance < 10
    assert facts["1"].name_mismatches == 0
    assert facts["1"].has_shape
