"""The JOSM file format, and the choice between it and osmChange."""

from xml.etree import ElementTree

import pytest

from gtfstoosm.convert import OSMRelationBuilder, convert_gtfs_to_osm
from gtfstoosm.osm import OSMNode, OSMRelation
from tests.test_offline import write_feed


class TestElements:
    def test_a_new_node_has_no_version_or_action(self):
        node = OSMNode(id=-3, lat=44.5, lon=6.2, tags={"name": "Gare"})
        xml = node.to_josm_xml()
        assert xml.startswith('  <node id="-3" lat="44.5" lon="6.2">')
        assert "version" not in xml and "action" not in xml
        assert '<tag k="name" v="Gare" />' in xml

    def test_an_existing_node_that_is_modified(self):
        node = OSMNode(id=123, version=3, lat=44.5, lon=6.2, tags={"a": "b"})
        xml = node.to_josm_xml(action="modify")
        assert 'id="123" version="3" action="modify" lat="44.5"' in xml

    def test_an_existing_node_without_action(self):
        node = OSMNode(id=123, version=3, lat=44.5, lon=6.2)
        assert "action" not in node.to_josm_xml()

    def test_a_relation_with_members(self):
        relation = OSMRelation(id=-1, tags={"type": "route", "name": "A & B"})
        relation.add_member("node", 123, "platform")
        relation.add_member("way", 456)
        xml = relation.to_josm_xml()
        assert '<member type="node" ref="123" role="platform" />' in xml
        assert '<member type="way" ref="456" role="" />' in xml
        assert 'v="A &amp; B"' in xml
        ElementTree.fromstring(xml)

    def test_an_existing_relation_that_is_modified(self):
        relation = OSMRelation(id=77, version=9, tags={"type": "route"})
        assert 'id="77" version="9" action="modify"' in relation.to_josm_xml("modify")


@pytest.fixture
def feed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
    )
    return write_feed(tmp_path / "feed.zip", shapes=False)


class TestOutputFormat:
    def test_osc_is_the_library_default(self, feed, tmp_path):
        out = tmp_path / "out.osc"
        convert_gtfs_to_osm(str(feed), str(out))
        root = ElementTree.parse(out).getroot()
        assert root.tag == "osmChange"
        assert root.find("create") is not None

    def test_osm_is_the_josm_format(self, feed, tmp_path):
        out = tmp_path / "out.osm"
        convert_gtfs_to_osm(str(feed), str(out), output_format="osm")
        root = ElementTree.parse(out).getroot()
        assert root.tag == "osm"
        assert root.get("version") == "0.6"
        assert root.findall("relation")
        assert all(int(r.get("id")) < 0 for r in root.findall("relation"))
        assert all("action" not in r.attrib for r in root.findall("relation"))

    def test_both_writes_the_two_files(self, feed, tmp_path):
        out = tmp_path / "out.osm"
        convert_gtfs_to_osm(str(feed), str(out), output_format="both")
        assert ElementTree.parse(out).getroot().tag == "osm"
        assert ElementTree.parse(tmp_path / "out.osc").getroot().tag == "osmChange"

    def test_accents_are_written_in_utf8(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
        )
        feed = write_feed(tmp_path / "feed.zip", shapes=False)
        out = tmp_path / "out.osm"
        convert_gtfs_to_osm(
            str(feed),
            str(out),
            output_format="osm",
            relation_tags={"note": "Trémouilles"},
        )
        assert "Trémouilles" in out.read_text(encoding="utf-8")


class TestMapRouletteFormat:
    """mr-cli cannot read relations of relations: the route_masters are left out."""

    @pytest.fixture
    def feed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
        )
        return write_feed(tmp_path / "feed.zip", shapes=False)

    def kinds(self, path):
        root = ElementTree.parse(path).getroot()
        return {
            tag.get("v")
            for e in root.iter("relation")
            for tag in e.findall("tag")
            if tag.get("k") == "type"
        }

    def test_the_masters_are_written_in_the_normal_format(self, feed, tmp_path):
        out = tmp_path / "out.osm"
        convert_gtfs_to_osm(str(feed), str(out), output_format="osm")
        assert self.kinds(out) == {"route", "route_master"}

    def test_and_left_out_for_maproulette(self, feed, tmp_path):
        out = tmp_path / "out.maproulette.osm"
        convert_gtfs_to_osm(str(feed), str(out), output_format="maproulette")
        assert self.kinds(out) == {"route"}
        assert ElementTree.parse(out).getroot().tag == "osm"
