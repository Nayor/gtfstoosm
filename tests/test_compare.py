"""Finding the relation of OSM that a route of the feed is, and how they differ."""

from gtfstoosm.compare import compare_route, normalize, stop_nodes
from gtfstoosm.osm import OSMRelation
from gtfstoosm.osm_data import ExistingRelation

FEED = "town-bus"


def built(stops, **tags):
    """A route relation built from the feed, with its stops as platform members."""
    base = {
        "type": "route",
        "route": "bus",
        "ref": "1",
        "gtfs:feed": FEED,
        "gtfs:route_id": "R1",
        "from": "Gare",
        "to": "Plage",
    }
    relation = OSMRelation(id=-1, tags={**base, **tags})
    for stop in stops:
        relation.add_member("node", stop, "platform")
    relation.add_member("way", 900)
    return relation


def existing(stops, relation_id=100, **tags):
    base = {"type": "route", "route": "bus", "ref": "1"}
    members = [("node", stop, "platform") for stop in stops] + [("way", 900, "")]
    return ExistingRelation(relation_id, 4, {**base, **tags}, members)


class TestHelpers:
    def test_normalize(self):
        assert normalize("  Chorges - Gare  SNCF ") == "chorges - gare sncf"
        assert normalize("Trémouilles") == normalize("tremouilles")
        assert normalize("bus 1: A => B") == normalize("bus 1: A B")
        assert normalize("A &amp; B") == normalize("a & b")

    def test_stop_nodes_keep_the_order_and_skip_new_and_other_members(self):
        members = [
            ("node", 5, "stop"),
            ("node", -2, "platform"),
            ("way", 7, ""),
            ("node", 3, "platform"),
            ("node", 9, "platform_exit_only"),
        ]
        assert stop_nodes(members) == [5, 3]


class TestStatuses:
    def test_nothing_in_osm(self):
        assert compare_route(built([1, 2, 3]), [], FEED).status == "new"

    def test_another_line_is_not_a_match(self):
        other = existing([10, 11, 12], ref="9")
        assert compare_route(built([1, 2, 3]), [other], FEED).status == "new"

    def test_the_same_relation(self):
        relation = built([1, 2, 3], network="Bus")
        osm = existing(
            [1, 2, 3],
            **{k: v for k, v in relation.tags.items() if k != "type"},
        )
        result = compare_route(relation, [osm], FEED)
        assert result.status == "same"
        assert result.existing is osm

    def test_only_tags_are_missing(self):
        osm = existing([1, 2, 3], from_="x")
        osm.tags.pop("from_")
        result = compare_route(built([1, 2, 3], duration="00:20"), [osm], FEED)
        assert result.status == "link"
        assert result.missing_tags["gtfs:feed"] == FEED
        assert result.missing_tags["duration"] == "00:20"
        assert not result.changed_tags

    def test_a_tag_with_another_value_is_a_difference_not_overwritten(self):
        osm = existing([1, 2, 3], network="Old Bus")
        result = compare_route(built([1, 2, 3], network="Bus"), [osm], FEED)
        assert result.status == "different"
        assert result.changed_tags["network"] == ("Old Bus", "Bus")
        assert "network" not in result.missing_tags

    def test_case_and_accents_are_not_differences(self):
        osm = existing([1, 2, 3], to="PLAGE", **{"from": "gare"})
        osm.tags.update({"gtfs:feed": FEED, "gtfs:route_id": "R1"})
        result = compare_route(built([1, 2, 3], to="Plâge"), [osm], FEED)
        assert not result.changed_tags

    def test_other_stops_are_a_difference(self):
        osm = existing([1, 2, 4])
        result = compare_route(built([1, 2, 3]), [osm], FEED)
        assert result.status == "different"
        assert result.stops_only_in_feed == [3]
        assert result.stops_only_in_osm == [4]

    def test_another_order_is_a_difference(self):
        osm = existing([1, 3, 2])
        result = compare_route(built([1, 2, 3]), [osm], FEED)
        assert result.status == "different"
        assert result.stops_reordered

    def test_a_stop_of_the_feed_that_osm_lacks(self):
        relation = built([1, 2])
        relation.add_member("node", -7, "platform")
        osm = existing([1, 2])
        result = compare_route(relation, [osm], FEED)
        assert result.status == "different"
        assert result.stops_missing_in_osm == 1


class TestFinding:
    def test_the_link_with_the_feed_is_enough_even_when_stops_changed(self):
        osm = existing([1, 2, 3], **{"gtfs:feed": FEED, "gtfs:route_id": "R1"})
        result = compare_route(built([7, 8, 9]), [osm], FEED)
        assert result.existing is osm
        assert result.status == "different"

    def test_another_feed_is_not_a_match_by_link(self):
        osm = existing(
            [1, 2, 3], ref="7", **{"gtfs:feed": "other", "gtfs:route_id": "R1"}
        )
        assert compare_route(built([7, 8, 9]), [osm], FEED).status == "new"

    def test_the_two_directions_of_a_route_are_told_apart_by_their_ends(self):
        link = {"gtfs:feed": FEED, "gtfs:route_id": "R1"}
        out = existing([1, 2, 3], 100, **link, **{"from": "Gare", "to": "Plage"})
        back = existing([3, 2, 1], 101, **link, **{"from": "Plage", "to": "Gare"})
        assert compare_route(built([1, 2, 3]), [out, back], FEED).existing is out
        reverse = built([3, 2, 1], **{"from": "Plage", "to": "Gare"})
        assert compare_route(reverse, [out, back], FEED).existing is back

    def test_the_directions_are_told_apart_by_the_order_when_ends_are_unknown(self):
        link = {"gtfs:feed": FEED, "gtfs:route_id": "R1"}
        out = existing([1, 2, 3], 100, **link)
        back = existing([3, 2, 1], 101, **link)
        relation = built([3, 2, 1])
        relation.tags.pop("from")
        relation.tags.pop("to")
        assert compare_route(relation, [out, back], FEED).existing is back

    def test_without_the_link_the_ref_and_most_stops_make_a_match(self):
        osm = existing([1, 2, 3, 4], 100)
        assert compare_route(built([1, 2, 3]), [osm], FEED).existing is osm

    def test_stops_in_common_but_another_ref_below_the_sure_threshold(self):
        osm = existing([1, 2, 10, 11], ref="9")  # overlap 2/6
        assert compare_route(built([1, 2, 3, 4]), [osm], FEED).status == "new"

    def test_nearly_the_same_stops_match_whatever_the_ref(self):
        osm = existing([1, 2, 3, 4, 5], ref="Z")  # overlap 1.0
        assert compare_route(built([1, 2, 3, 4, 5]), [osm], FEED).existing is osm

    def test_two_equal_candidates_are_ambiguous(self):
        a = existing([1, 2, 3], 100)
        b = existing([1, 2, 3], 101)
        result = compare_route(built([1, 2, 3]), [a, b], FEED)
        assert result.status == "ambiguous"
        assert {c.id for c in result.candidates} == {100, 101}
        assert result.existing is None

    def test_the_mode_must_agree(self):
        tram = existing([1, 2, 3], route="tram")
        assert compare_route(built([1, 2, 3]), [tram], FEED).status == "new"

    def test_a_route_master_is_not_a_route(self):
        master = existing([1, 2, 3], type="route_master")
        assert compare_route(built([1, 2, 3]), [master], FEED).status == "new"
