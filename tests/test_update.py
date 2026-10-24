"""What is created and what is changed when OSM has some of the routes already."""

import logging
from xml.etree import ElementTree

import pytest

from gtfstoosm.convert import OSMRelationBuilder, convert_gtfs_to_osm
from gtfstoosm.gtfs import GTFSFeed
from gtfstoosm.osm_data import OsmExtract
from gtfstoosm.update import compare_with_osm
from tests.test_offline import LAT, build_osm, lon, write_feed

FEED = "town"


def town_with_routes(path, routes):
    """The town of test_offline, plus a stop at lon(9) and the given relations."""
    build_osm(path)
    xml = path.read_text(encoding="utf8")
    # nodes must come before ways: put the extra stop among the nodes
    stop = (
        f'<node id="510" version="2" lat="{LAT + 0.00005}" lon="{lon(9)}">'
        '<tag k="highway" v="bus_stop" /><tag k="name" v="Centre" />'
        "</node>"
    )
    xml = xml.replace('<node id="501"', stop + '<node id="501"', 1) if False else xml
    xml = xml.replace("<way ", stop + "<way ", 1)
    xml = xml.replace("</osm>", routes + "</osm>")
    path.write_text(xml, encoding="utf8")
    return path


def relation_xml(relation_id, version, tags, members):
    kv = "".join(f'<tag k="{k}" v="{v}" />' for k, v in tags.items())
    m = "".join(
        f'<member type="{t}" ref="{r}" role="{role}" />' for t, r, role in members
    )
    return f'<relation id="{relation_id}" version="{version}">{m}{kv}</relation>'


BUS = {"type": "route", "route": "bus", "ref": "1"}


def compare(tmp_path, routes, **kwargs):
    osm = town_with_routes(tmp_path / "town.osm", routes)
    feed = GTFSFeed(feed_dir=str(write_feed(tmp_path / "feed.zip")))
    feed.load()
    builder = OSMRelationBuilder(
        osm_extract=OsmExtract(str(osm)),
        add_missing_stops=True,
        feed_id=FEED,
        agency_tags=True,
        **kwargs,
    )
    builder.build_relations(feed.tables)
    builder.build_route_masters(feed.tables)
    compare_with_osm(builder)
    return builder


def statuses(builder):
    return {m.relation.tags["ref"]: m.status for m in builder.matches}


def created_refs(builder):
    return {r.tags["ref"] for r in builder.relations if r.tags["type"] == "route"}


def modified(builder, element_id):
    return next(m for m in builder.modifications if m.id == element_id)


class TestRoutesThatOsmHas:
    def test_nothing_in_osm_everything_is_created(self, tmp_path):
        builder = compare(tmp_path, "")
        assert statuses(builder) == {"1": "new", "2": "new"}
        assert created_refs(builder) == {"1", "2"}
        # the stops that OSM has still get the link with the feed
        assert {m.id for m in builder.modifications} == {501, 502, 510}

    def test_a_route_with_the_same_stops_only_lacks_the_link(self, tmp_path):
        osm = relation_xml(
            701,
            5,
            BUS,
            [("node", 501, "platform"), ("node", 510, "platform"), ("way", 100, "")],
        )
        builder = compare(tmp_path, osm)
        assert statuses(builder)["1"] == "link"
        # it is not created again, the tram that OSM lacks is
        assert created_refs(builder) == {"2"}

        change = modified(builder, 701)
        assert change.version == 5
        assert change.tags["gtfs:feed"] == FEED
        assert change.tags["gtfs:route_id"] == "B"
        assert change.tags["operator"] == "Agence"
        assert change.tags["ref"] == "1"  # what OSM had is kept
        assert [(m.type, m.ref) for m in change.members] == [
            ("node", 501),
            ("node", 510),
            ("way", 100),
        ]

    def test_a_route_that_differs_is_reported_and_left_alone(self, tmp_path, caplog):
        osm = relation_xml(
            701,
            5,
            {**BUS, "network": "Old Bus"},
            [("node", 501, "platform"), ("way", 100, "")],
        )
        with caplog.at_level(logging.WARNING):
            builder = compare(tmp_path, osm, relation_tags={"network": "Bus"})
        [match] = [m for m in builder.matches if m.relation.tags["ref"] == "1"]
        assert match.status == "different"
        assert match.changed_tags["network"] == ("Old Bus", "Bus")
        assert match.stops_only_in_feed == [510]  # the relation of OSM lacks that stop
        assert "r701" in caplog.text
        assert 701 not in {m.id for m in builder.modifications}
        assert "1" not in created_refs(builder)  # not created a second time

    def test_two_equal_candidates_are_not_touched(self, tmp_path):
        members = [("node", 501, "platform"), ("node", 510, "platform")]
        osm = relation_xml(701, 1, BUS, members) + relation_xml(702, 1, BUS, members)
        builder = compare(tmp_path, osm)
        assert statuses(builder)["1"] == "ambiguous"
        assert {701, 702}.isdisjoint({m.id for m in builder.modifications})
        assert "1" not in created_refs(builder)

    def test_the_stop_link_is_not_added_twice(self, tmp_path):
        link = f"gtfs:stop_id:{FEED}"
        osm = town_with_routes(tmp_path / "town.osm", "")
        text = osm.read_text(encoding="utf8").replace(
            '<tag k="name" v="Gare"/>',
            f'<tag k="name" v="Gare"/><tag k="{link}" v="S1" />',
        )
        osm.write_text(text, encoding="utf8")
        feed = GTFSFeed(feed_dir=str(write_feed(tmp_path / "feed.zip")))
        feed.load()
        builder = OSMRelationBuilder(osm_extract=OsmExtract(str(osm)), feed_id=FEED)
        builder.build_relations(feed.tables)
        compare_with_osm(builder)
        assert 501 not in {m.id for m in builder.modifications}


class TestMasters:
    def test_a_new_route_is_added_to_the_master_of_osm(self, tmp_path):
        route = relation_xml(
            701, 5, BUS, [("node", 501, "platform"), ("node", 510, "platform")]
        )
        master = relation_xml(
            800,
            3,
            {"type": "route_master", "route_master": "bus", "ref": "1"},
            [("relation", 701, "")],
        )
        builder = compare(tmp_path, route + master)

        # the route is known and in the master already: only the tags are lacking
        change = modified(builder, 800)
        assert change.tags["gtfs:feed"] == FEED
        assert [(m.type, m.ref) for m in change.members] == [("relation", 701)]
        assert not [r for r in builder.relations if r.tags["ref"] == "1"]

    def test_a_master_that_osm_lacks_is_created_with_the_routes_of_osm(self, tmp_path):
        route = relation_xml(
            701, 5, BUS, [("node", 501, "platform"), ("node", 510, "platform")]
        )
        builder = compare(tmp_path, route)
        [master] = [
            r
            for r in builder.relations
            if r.tags["type"] == "route_master" and r.tags["ref"] == "1"
        ]
        assert [(m.type, m.ref) for m in master.members] == [("relation", 701)]


class TestWriting:
    def test_josm_file_has_the_changes_as_modifications(self, tmp_path):
        osm = town_with_routes(
            tmp_path / "town.osm",
            relation_xml(
                701, 5, BUS, [("node", 501, "platform"), ("node", 510, "platform")]
            ),
        )
        feed = write_feed(tmp_path / "feed.zip")
        out = tmp_path / "out.osm"

        convert_gtfs_to_osm(
            str(feed), str(out), osm_file=str(osm), output_format="both", feed_id=FEED
        )

        root = ElementTree.parse(out).getroot()
        changed = {(e.tag, e.get("id")): e for e in root if e.get("action") == "modify"}
        assert ("relation", "701") in changed
        assert ("node", "501") in changed
        assert changed[("relation", "701")].get("version") == "5"
        # the created ones have negative ids and no action
        assert all(int(e.get("id")) < 0 for e in root if e.get("action") is None)

        change = ElementTree.parse(tmp_path / "out.osc").getroot()
        modify = change.find("modify")
        assert modify is not None
        assert {e.get("id") for e in modify} >= {"701", "501"}


@pytest.mark.parametrize("flag", [False])
def test_comparison_can_be_switched_off(tmp_path, flag):
    osm = town_with_routes(
        tmp_path / "town.osm",
        relation_xml(
            701, 5, BUS, [("node", 501, "platform"), ("node", 510, "platform")]
        ),
    )
    feed = GTFSFeed(feed_dir=str(write_feed(tmp_path / "feed.zip")))
    feed.load()
    builder = OSMRelationBuilder(
        osm_extract=OsmExtract(str(osm)), compare_existing=flag, feed_id=FEED
    )
    builder.build_relations(feed.tables)
    compare_with_osm(builder)
    assert builder.matches == []
    assert builder.modifications == []
    assert "1" in created_refs(builder)
