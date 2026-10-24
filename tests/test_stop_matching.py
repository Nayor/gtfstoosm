"""Matching GTFS stops to existing OSM stops, and the stop search radius."""

import polars as pl
import pytest

from gtfstoosm.cli import main
from gtfstoosm.convert import OSMRelationBuilder
from gtfstoosm.osm import OSMNode

LAT = 47.0
DEGREE = 111194.9  # metres per degree of latitude


def north(metres: float) -> float:
    return LAT + metres / DEGREE


def stops(*rows):
    return pl.DataFrame(
        {
            "stop_id": [r[0] for r in rows],
            "lat": [north(r[1]) for r in rows],
            "lon": [8.0] * len(rows),
            "name": [r[0] for r in rows],
        }
    )


def node(node_id: int, metres: float) -> OSMNode:
    return OSMNode(id=node_id, lat=north(metres), lon=8.0, tags={})


def match(rows, nodes, radius=15.0, add_missing=False):
    builder = OSMRelationBuilder()
    return builder, builder._match_stops(rows, nodes, add_missing, radius, "bus")


def test_closest_pairs_win_over_pattern_order():
    # B comes first and is closer to X than to Y, but X is A's own stop
    rows = stops(("B", 5), ("A", 0))
    _, result = match(rows, [node(1, 0), node(2, 17)])
    assert [e.id for e in result] == [2, 1]


def test_a_stop_visited_twice_keeps_its_node():
    rows = stops(("LOOP", 0), ("OTHER", 200), ("LOOP", 0))
    _, result = match(rows, [node(1, 0), node(2, 200)])
    assert [e.id for e in result] == [1, 2, 1]


def test_nothing_within_the_radius():
    rows = stops(("A", 0))
    builder, result = match(rows, [node(1, 20)], radius=10.0)
    assert result == []
    assert builder.new_stops == []


def test_larger_radius_matches_farther_stops():
    rows = stops(("A", 0))
    _, result = match(rows, [node(1, 20)], radius=25.0)
    assert [e.id for e in result] == [1]


def test_unmatched_stops_become_new_nodes_when_asked():
    rows = stops(("A", 0), ("B", 300))
    builder, result = match(rows, [node(1, 0)], add_missing=True)
    assert [e.id for e in result] == [1, -1]
    assert [n.tags["name"] for n in builder.new_stops] == ["B"]


class TestRadiusOption:
    @pytest.fixture
    def seen(self, monkeypatch, tmp_path):
        """Run the CLI with the conversion replaced by a recorder of its options."""
        feed = tmp_path / "feed.zip"
        feed.write_bytes(b"")
        osm = tmp_path / "area.osm"
        osm.write_text("<osm/>", encoding="utf8")
        calls = []
        monkeypatch.setattr(
            "gtfstoosm.cli.convert_gtfs_to_osm",
            lambda feed_path, out_path, **options: calls.append(options) or True,
        )

        def run(*args):
            calls.clear()
            code = main(["-i", str(feed), "-o", str(tmp_path / "o.osc"), *args])
            assert code == 0
            return calls[0]["stop_search_radius"]

        run.osm = str(osm)
        return run

    def test_online_the_radius_is_capped_at_10_metres(self, seen):
        assert seen("--stop-search-radius", "50") == 10
        assert seen("--stop-search-radius", "7") == 7

    def test_with_an_extract_it_can_be_larger(self, seen):
        assert seen("--osm-file", seen.osm, "--stop-search-radius", "50") == 50
        assert seen("--osm-file", seen.osm, "--stop-search-radius", "500") == 100
