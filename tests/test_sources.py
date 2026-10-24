"""Which region file covers a feed: OSM France, Geofabrik, and robots.txt."""

import json
import logging

import pytest
import requests

from gtfstoosm import sources
from gtfstoosm.sources import (
    Geofabrik,
    OsmFrance,
    Source,
    choose_sources,
    manual_path,
    parse_poly,
    robots_allows,
)

OSMFR_ROBOTS = (
    "User-agent: *\nDisallow: /replication\n\nUser-agent: *\nDisallow: /*.pbf$\n"
)


class TestRobots:
    def test_osm_france_forbids_pbf_files_only(self):
        assert not robots_allows(OSMFR_ROBOTS, "/extracts/europe/france.osm.pbf")
        assert not robots_allows(OSMFR_ROBOTS, "/extracts/europe/france-latest.osm.pbf")
        assert robots_allows(OSMFR_ROBOTS, "/extracts/europe/france/")
        assert robots_allows(OSMFR_ROBOTS, "/polygons/europe/france.poly")
        assert robots_allows(OSMFR_ROBOTS, "/extracts/europe/france.osm.pbf.md5")
        assert not robots_allows(OSMFR_ROBOTS, "/replication/x")

    def test_no_rules_means_allowed(self):
        assert robots_allows("", "/anything.pbf")
        assert robots_allows("User-agent: *\nDisallow:\n", "/anything.pbf")

    def test_the_longest_rule_wins(self):
        robots = "User-agent: *\nDisallow: /data/\nAllow: /data/public/\n"
        assert not robots_allows(robots, "/data/secret.pbf")
        assert robots_allows(robots, "/data/public/a.pbf")

    def test_a_group_for_this_robot_wins_over_the_wildcard(self):
        robots = "User-agent: *\nDisallow: /\n\nUser-agent: gtfstoosm\nAllow: /\n"
        assert robots_allows(robots, "/a.pbf")
        assert not robots_allows(robots, "/a.pbf", robot="other")

    def test_comments_and_case(self):
        robots = "# hi\nUSER-AGENT: *   # everyone\nDISALLOW: /private # no\n"
        assert not robots_allows(robots, "/private/x")
        assert robots_allows(robots, "/public/x")


def square(min_lon, min_lat, max_lon, max_lat):
    return [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]


def poly_text(*rings):
    lines = ["region"]
    for number, ring in enumerate(rings, start=1):
        lines.append(f"{'!' if number > 1 else ''}{number}")
        lines += [f"   {x}   {y}" for x, y in ring]
        lines.append("END")
    lines.append("END")
    return "\n".join(lines) + "\n"


class TestParsePoly:
    def test_a_polygon(self):
        [rings] = parse_poly(poly_text(square(0, 0, 2, 1)))
        assert len(rings) == 1
        assert rings[0][0] == [0.0, 0.0]
        assert rings[0][0] == rings[0][-1]

    def test_holes_belong_to_the_polygon_before(self):
        [rings] = parse_poly(poly_text(square(0, 0, 10, 10), square(4, 4, 5, 5)))
        assert len(rings) == 2

    def test_several_polygons_and_exponents(self):
        text = (
            "islands\n1\n 1.0E1 1.0E1\n 2.0E1 1.0E1\n 2.0E1 2.0E1\nEND\n"
            "2\n 5 5\n 6 5\n 6 6\nEND\nEND\n"
        )
        polygons = parse_poly(text)
        assert len(polygons) == 2
        assert polygons[0][0][0] == [10.0, 10.0]


def listing(*hrefs):
    links = "".join(f'<tr><td><a href="{h}">{h}</a></td></tr>' for h in hrefs)
    return f'<html><body><a href="/extracts/">Parent</a><table>{links}</table></body></html>'


ROOT = "https://download.openstreetmap.fr"

#   europe
#   ├── france
#   │   └── paca (region)
#   │       └── hautes_alpes (department)
#   ├── switzerland (no sub-region in this fake)
#   └── monaco
SITE = {
    "/robots.txt": OSMFR_ROBOTS,
    "/extracts/": listing(
        "africa-latest.osm.pbf", "africa.osm.pbf", "europe.osm.pbf", "europe/", "merge/"
    ),
    "/extracts/europe/": listing(
        "france.osm.pbf", "france/", "monaco.osm.pbf", "switzerland.osm.pbf"
    ),
    "/extracts/europe/france/": listing("paca.osm.pbf", "paca/", "bretagne.osm.pbf"),
    "/extracts/europe/france/paca/": listing("hautes_alpes.osm.pbf", "var.osm.pbf"),
    "/polygons/africa.poly": poly_text(square(-20, -35, 50, 35)),
    "/polygons/europe.poly": poly_text(square(-10, 35, 30, 70)),
    "/polygons/europe/france.poly": poly_text(square(-5, 42, 8, 51)),
    "/polygons/europe/monaco.poly": poly_text(square(7.4, 43.7, 7.5, 43.8)),
    "/polygons/europe/switzerland.poly": poly_text(square(6, 45.8, 10.5, 47.9)),
    "/polygons/europe/france/paca.poly": poly_text(square(4, 43, 8, 45.5)),
    "/polygons/europe/france/bretagne.poly": poly_text(square(-5, 47, -1, 49)),
    "/polygons/europe/france/paca/hautes_alpes.poly": poly_text(
        square(5.4, 44.4, 7.2, 45.1)
    ),
    "/polygons/europe/france/paca/var.poly": poly_text(square(5.5, 43.0, 7.0, 43.8)),
}


class FakeResponse:
    def __init__(self, status=200, body=""):
        self.status_code = status
        self._body = body.encode() if isinstance(body, str) else body
        self.headers = {}

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def site(monkeypatch):
    """OSM France replaced by SITE; `site.hits` lists the URLs asked."""
    hits = []

    def get(url, headers=None, **kwargs):
        hits.append(url)
        path = url.removeprefix(ROOT)
        if url.startswith(ROOT) and path in SITE:
            return FakeResponse(200, SITE[path])
        return FakeResponse(404)

    monkeypatch.setattr(requests, "get", get)
    return hits


@pytest.fixture
def osmfr(tmp_path, site):
    return OsmFrance(tmp_path / "cache")


class TestOsmFrance:
    def test_the_deepest_region_wins(self, osmfr):
        source = osmfr.find(44.5, 6.3)
        assert source.name == "europe/france/paca/hautes_alpes"
        assert source.url == f"{ROOT}/extracts/europe/france/paca/hautes_alpes.osm.pbf"

    def test_a_region_without_sub_regions(self, osmfr):
        assert osmfr.find(46.9, 7.4).name == "europe/switzerland"
        assert osmfr.find(43.74, 7.45).name == "europe/monaco"

    def test_inside_a_region_but_between_departments(self, osmfr):
        # in paca (lat 43-45.5) but in no department of the fake
        assert osmfr.find(45.3, 4.5).name == "europe/france/paca"

    def test_nowhere(self, osmfr):
        assert osmfr.find(-60.0, -120.0) is None

    def test_robots_txt_forbids_the_download(self, osmfr):
        assert osmfr.find(44.5, 6.3).allowed is False

    def test_robots_txt_allowing_it(self, osmfr, monkeypatch):
        monkeypatch.setitem(
            SITE, "/robots.txt", "User-agent: *\nDisallow: /replication\n"
        )
        assert osmfr.find(44.5, 6.3).allowed is True

    def test_listings_and_outlines_are_cached(self, osmfr, site):
        osmfr.find(44.5, 6.3)
        first = len(site)
        osmfr.find(44.6, 6.4)
        assert len(site) == first

    def test_unreachable_site(self, tmp_path, monkeypatch):
        def down(*args, **kwargs):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(requests, "get", down)
        assert OsmFrance(tmp_path / "c").find(44.5, 6.3) is None


class FakeProvider:
    def __init__(self, *sources):
        self.sources = list(sources)

    def find(self, lat, lon):
        return self.sources.pop(0) if self.sources else None


def source(provider, name, area, allowed=True):
    return Source(
        provider, name, name, f"https://{provider}.org/{name}.pbf", area, allowed
    )


class TestChoose:
    def test_the_smallest_allowed_region(self, tmp_path):
        small = source("osmfr", "dept", 1.0)
        big = source("geofabrik", "region", 20.0)
        chosen = choose_sources(
            [(44.5, 6.3)], tmp_path, providers=[FakeProvider(small), FakeProvider(big)]
        )
        assert chosen == [small]

    def test_a_large_region_is_compared_with_the_next_provider(self, tmp_path):
        country = source("osmfr", "country", 50.0)
        region = source("geofabrik", "region", 8.0)
        chosen = choose_sources(
            [(44.5, 6.3)],
            tmp_path,
            providers=[FakeProvider(country), FakeProvider(region)],
        )
        assert chosen == [region]

    def test_a_small_region_spares_the_next_provider(self, tmp_path):
        class Fails:
            def find(self, lat, lon):
                raise AssertionError("not asked")

        small = source("osmfr", "dept", 0.8)
        assert choose_sources(
            [(44.5, 6.3)], tmp_path, providers=[FakeProvider(small), Fails()]
        ) == [small]

    def test_a_forbidden_file_is_replaced_and_reported(self, tmp_path, caplog):
        small = source("osmfr", "dept", 1.0, allowed=False)
        big = source("geofabrik", "region", 20.0)
        with caplog.at_level(logging.WARNING):
            chosen = choose_sources(
                [(44.5, 6.3)],
                tmp_path,
                providers=[FakeProvider(small), FakeProvider(big)],
            )
        assert chosen == [big]
        assert "robots.txt" in caplog.text
        assert "keeping its name" in caplog.text
        assert "--osm-file" in caplog.text

    def test_a_forbidden_file_put_in_the_cache_is_used(self, tmp_path):
        small = source("osmfr", "dept", 1.0, allowed=False)
        big = source("geofabrik", "region", 20.0)
        (tmp_path / "regions").mkdir()
        (tmp_path / "regions" / "osmfr-dept.osm.pbf").write_bytes(b"x")
        chosen = choose_sources(
            [(44.5, 6.3)], tmp_path, providers=[FakeProvider(small), FakeProvider(big)]
        )
        assert chosen == [small]

    def test_only_a_forbidden_file(self, tmp_path):
        small = source("osmfr", "dept", 1.0, allowed=False)
        assert (
            choose_sources([(44.5, 6.3)], tmp_path, providers=[FakeProvider(small)])
            == []
        )

    def test_two_areas_two_files_and_no_repeat(self, tmp_path):
        a = source("osmfr", "a", 1.0)
        b = source("osmfr", "b", 2.0)

        class ByLongitude:
            def find(self, lat, lon):
                return a if lon < 7 else b

        points = [(44.5, 6.3), (44.5, 6.31), (46.9, 7.4), (46.9, 7.41)]
        chosen = choose_sources(points, tmp_path, providers=[ByLongitude()])
        assert sorted(s.id for s in chosen) == ["a", "b"]

    def test_nothing_found(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            assert (
                choose_sources([(0.0, 0.0)], tmp_path, providers=[FakeProvider()]) == []
            )
        assert "No OSM region found" in caplog.text


class TestGeofabrik:
    def test_smallest_region_of_the_index(self, tmp_path, monkeypatch):
        index = {
            "features": [
                {
                    "properties": {
                        "id": "france",
                        "name": "France",
                        "urls": {"pbf": "https://g.org/france.pbf"},
                    },
                    "geometry": {
                        "type": "MultiPolygon",
                        "coordinates": [[square(-5, 42, 8, 51)]],
                    },
                },
                {
                    "properties": {
                        "id": "paca",
                        "name": "PACA",
                        "urls": {"pbf": "https://g.org/paca.pbf"},
                    },
                    "geometry": {
                        "type": "MultiPolygon",
                        "coordinates": [[square(4, 43, 8, 45.5)]],
                    },
                },
            ]
        }
        path = tmp_path / "index.json"
        path.write_text(json.dumps(index), encoding="utf8")
        monkeypatch.setattr(sources, "download", lambda *args, **kwargs: path)

        provider = Geofabrik(tmp_path)
        found = provider.find(44.5, 6.3)
        assert (found.provider, found.id, found.allowed) == ("geofabrik", "paca", True)
        assert provider.find(48.8, 2.3).id == "france"
        assert provider.find(-60.0, -120.0) is None


class TestManualFiles:
    def test_the_name_given_by_the_site_is_recognised(self, tmp_path):
        small = Source(
            "osmfr", "europe_france_x_hautes_alpes", "europe/france/x/hautes_alpes",
            "https://x.org/hautes_alpes.osm.pbf", 0.5, False,
        )  # fmt: skip
        big = source("geofabrik", "region", 20.0)
        (tmp_path / "regions").mkdir()
        (tmp_path / "regions" / "hautes_alpes.osm.pbf").write_bytes(
            b"x"
        )  # as a browser saves it

        chosen = choose_sources(
            [(44.5, 6.3)], tmp_path, providers=[FakeProvider(small), FakeProvider(big)]
        )
        assert chosen == [small]
        assert manual_path(tmp_path, small).name == "hautes_alpes.osm.pbf"

    def test_the_folder_to_use_is_created_and_named(self, tmp_path, caplog):
        small = source("osmfr", "dept", 1.0, allowed=False)
        big = source("geofabrik", "region", 20.0)
        with caplog.at_level(logging.WARNING):
            choose_sources(
                [(44.5, 6.3)],
                tmp_path,
                providers=[FakeProvider(small), FakeProvider(big)],
            )
        assert (tmp_path / "regions").is_dir()
        assert str(tmp_path / "regions") in caplog.text
