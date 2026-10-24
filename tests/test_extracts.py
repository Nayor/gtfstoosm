"""Finding, downloading, caching and cutting the OSM data around a feed."""

import json
import os
import subprocess
import time

import pytest
import requests

from gtfstoosm import extracts
from gtfstoosm.extracts import (
    Region,
    cut,
    download,
    prepare_extract,
    read_regions,
    regions_for_points,
)
from gtfstoosm.osm_data import OsmExtract
from gtfstoosm.sources import Source
from tests.test_offline import LAT, build_osm, lon


def square(min_lon, min_lat, max_lon, max_lat):
    return [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]


def feature(region_id, url, *rings):
    return {
        "type": "Feature",
        "properties": {"id": region_id, "name": region_id, "urls": {"pbf": url}},
        "geometry": {"type": "MultiPolygon", "coordinates": [list(rings)]},
    }


@pytest.fixture
def index_file(tmp_path):
    index = {
        "type": "FeatureCollection",
        "features": [
            feature(
                "europe", "https://example.org/europe.pbf", square(-10, 35, 30, 70)
            ),
            feature("france", "https://example.org/france.pbf", square(-5, 42, 8, 51)),
            feature("paca", "https://example.org/paca.pbf", square(4, 43, 8, 45.5)),
            feature(
                "switzerland", "https://example.org/ch.pbf", square(6, 45.8, 10.5, 47.9)
            ),
            # a region with a hole: a lake in the middle of the lake region
            feature(
                "lakeland",
                "https://example.org/lake.pbf",
                square(20, 40, 25, 45),
                square(22, 42, 23, 43),
            ),
            {
                "type": "Feature",
                "properties": {"id": "nofile", "urls": {}},
                "geometry": None,
            },
        ],
    }
    path = tmp_path / "index.json"
    path.write_text(json.dumps(index), encoding="utf8")
    return path


class TestRegions:
    def test_regions_without_file_are_ignored(self, index_file):
        assert "nofile" not in {r.id for r in read_regions(index_file)}

    def test_smallest_region_wins(self, index_file):
        regions = read_regions(index_file)
        assert [r.id for r in regions_for_points(regions, [(44.5, 6.3)])] == ["paca"]
        assert [r.id for r in regions_for_points(regions, [(48.5, 2.3)])] == ["france"]
        assert [r.id for r in regions_for_points(regions, [(60.0, 15.0)])] == ["europe"]

    def test_a_feed_over_two_regions(self, index_file):
        regions = read_regions(index_file)
        found = regions_for_points(regions, [(44.5, 6.3), (46.9, 7.4), (44.6, 6.4)])
        assert sorted(r.id for r in found) == ["paca", "switzerland"]

    def test_holes_and_outside(self, index_file):
        regions = {r.id: r for r in read_regions(index_file)}
        assert regions["lakeland"].contains(41.0, 21.0)
        assert not regions["lakeland"].contains(42.5, 22.5)  # in the hole
        assert regions_for_points(list(regions.values()), [(-30.0, -30.0)]) == []

    def test_area(self):
        assert Region("a", "a", "u", [[square(0, 0, 2, 3)]]).area == pytest.approx(6)


class FakeResponse:
    def __init__(self, status=200, body=b"data", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def server(monkeypatch):
    """Replace requests.get; `server.answers` is a list of responses to give."""

    class Server:
        answers: list = []
        requests: list = []

    def get(url, headers=None, **kwargs):
        Server.requests.append({"url": url, "headers": headers or {}})
        answer = Server.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    Server.answers = []
    Server.requests = []
    monkeypatch.setattr(requests, "get", get)
    return Server


def age(path, days):
    """Pretend the download of a file happened `days` ago."""
    meta = path.with_name(path.name + ".json")
    data = json.loads(meta.read_text(encoding="utf8"))
    data["fetched_at"] = time.time() - days * 86400
    meta.write_text(json.dumps(data), encoding="utf8")


class TestDownload:
    OK = {"ETag": '"abc"', "Last-Modified": "Fri, 23 Oct 2026 23:20:52 GMT"}

    def test_first_download(self, tmp_path, server):
        server.answers = [FakeResponse(200, b"hello", self.OK)]
        dest = download("https://example.org/a.pbf", tmp_path / "a.pbf")
        assert dest.read_bytes() == b"hello"
        assert "gtfstoosm" in server.requests[0]["headers"]["User-Agent"]

    def test_recent_file_is_not_requested_again(self, tmp_path, server):
        server.answers = [FakeResponse(200, b"hello", self.OK)]
        dest = download("https://example.org/a.pbf", tmp_path / "a.pbf")
        age(dest, 2)
        download("https://example.org/a.pbf", dest)
        assert len(server.requests) == 1

    def test_old_file_is_checked_with_a_conditional_request(self, tmp_path, server):
        server.answers = [FakeResponse(200, b"hello", self.OK), FakeResponse(304)]
        dest = download("https://example.org/a.pbf", tmp_path / "a.pbf")
        age(dest, 30)
        download("https://example.org/a.pbf", dest)

        headers = server.requests[1]["headers"]
        assert headers["If-None-Match"] == '"abc"'
        assert headers["If-Modified-Since"] == self.OK["Last-Modified"]
        assert dest.read_bytes() == b"hello"
        # the check counts as fresh: no new request right after
        download("https://example.org/a.pbf", dest)
        assert len(server.requests) == 2

    def test_changed_file_is_downloaded_again(self, tmp_path, server):
        server.answers = [FakeResponse(200, b"old", self.OK), FakeResponse(200, b"new")]
        dest = download("https://example.org/a.pbf", tmp_path / "a.pbf")
        age(dest, 30)
        download("https://example.org/a.pbf", dest)
        assert dest.read_bytes() == b"new"

    def test_refresh_downloads_without_conditions(self, tmp_path, server):
        server.answers = [FakeResponse(200, b"old", self.OK), FakeResponse(200, b"new")]
        dest = download("https://example.org/a.pbf", tmp_path / "a.pbf")
        download("https://example.org/a.pbf", dest, refresh=True)
        assert "If-None-Match" not in server.requests[1]["headers"]
        assert dest.read_bytes() == b"new"

    def test_network_failure_falls_back_on_the_cache(self, tmp_path, server):
        server.answers = [
            FakeResponse(200, b"old", self.OK),
            requests.ConnectionError("down"),
        ]
        dest = download("https://example.org/a.pbf", tmp_path / "a.pbf")
        age(dest, 30)
        assert download("https://example.org/a.pbf", dest).read_bytes() == b"old"

    def test_network_failure_without_cache(self, tmp_path, server):
        server.answers = [requests.ConnectionError("down")]
        with pytest.raises(RuntimeError, match="Cannot download"):
            download("https://example.org/a.pbf", tmp_path / "a.pbf")

    def test_http_error_without_cache(self, tmp_path, server):
        server.answers = [FakeResponse(404)]
        with pytest.raises(RuntimeError, match="HTTP 404"):
            download("https://example.org/a.pbf", tmp_path / "a.pbf")
        assert not (tmp_path / "a.pbf").exists()


BBOX = (LAT - 0.02, 8.0 - 0.02, LAT + 0.02, lon(20) + 0.02)


class TestCut:
    def test_cut_keeps_the_area(self, tmp_path):
        town = build_osm(tmp_path / "town.osm")
        out = tmp_path / "area.osm.pbf"
        assert cut([town], BBOX, out)

        area = OsmExtract(str(out))
        near = [(LAT, lon(1))]
        assert [n.id for n in area.stops_near(near, 10, "bus")] == [501]
        assert area.stops_near([(LAT + 0.5, lon(1))], 10, "bus") == []  # cut away

    def test_several_sources_are_merged(self, tmp_path):
        town = build_osm(tmp_path / "town.osm")
        out = tmp_path / "area.osm.pbf"
        assert cut([town, town], BBOX, out)
        assert OsmExtract(str(out)).stops_near([(LAT, lon(1))], 10, "bus")

    def test_without_osmium(self, tmp_path, monkeypatch):
        monkeypatch.setattr(extracts.shutil, "which", lambda name: None)
        assert not cut([tmp_path / "x"], BBOX, tmp_path / "out.pbf")


class FakeProvider:
    """A provider that always finds the same source (or none)."""

    def __init__(self, source):
        self.source = source

    def find(self, lat, lon):
        return self.source


TOWN = Source("fake", "town", "town", "https://example.org/town.pbf", 1.0)


class TestPrepareExtract:
    @pytest.fixture
    def asked(self, tmp_path, monkeypatch):
        """prepare_extract with the downloads served from a local file."""
        town = build_osm(tmp_path / "town.osm")
        asked = []

        def fake_download(url, dest, max_age_days=7, refresh=False):
            asked.append((url, refresh))
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["osmium", "cat", "--overwrite", "-o", str(dest), str(town)],
                check=True,
                capture_output=True,
            )
            dest.with_name(dest.name + ".json").write_text(
                json.dumps({"last_modified": "stamp", "fetched_at": time.time()}),
                encoding="utf8",
            )
            return dest

        monkeypatch.setattr(extracts, "download", fake_download)
        return asked

    def prepare(self, tmp_path, source=TOWN, **kwargs):
        return prepare_extract(
            [(LAT, lon(1)), (LAT, lon(9))],
            BBOX,
            cache=tmp_path / "cache",
            providers=[FakeProvider(source)],
            **kwargs,
        )

    def test_downloads_cuts_and_caches(self, tmp_path, asked):
        [path] = self.prepare(tmp_path)
        assert path.startswith(str(tmp_path / "cache" / "extracts"))
        assert OsmExtract(path).stops_near([(LAT, lon(1))], 10, "bus")

        mtime = os.path.getmtime(path)
        [again] = self.prepare(tmp_path)
        assert again == path
        assert os.path.getmtime(path) == mtime  # not cut a second time

    def test_refresh_is_passed_on(self, tmp_path, asked):
        self.prepare(tmp_path, refresh=True)
        assert asked == [("https://example.org/town.pbf", True)]

    def test_the_whole_region_without_osmium(self, tmp_path, asked, monkeypatch):
        monkeypatch.setattr(extracts.shutil, "which", lambda name: None)
        [path] = self.prepare(tmp_path)
        assert "regions" in path

    def test_no_region_for_the_feed(self, tmp_path, asked):
        with pytest.raises(RuntimeError, match="--osm-file"):
            self.prepare(tmp_path, source=None)

    def test_a_file_put_in_the_cache_by_hand_is_used(self, tmp_path, asked):
        forbidden = Source(
            "osmfr", "fr_town", "fr/town", "https://x.org/t.pbf", 0.5, False
        )
        by_hand = tmp_path / "cache" / "regions" / "osmfr-fr_town.osm.pbf"
        by_hand.parent.mkdir(parents=True)
        subprocess.run(
            ["osmium", "cat", "-o", str(by_hand), str(build_osm(tmp_path / "t.osm"))],
            check=True,
            capture_output=True,
        )

        [path] = self.prepare(tmp_path, source=forbidden)

        assert asked == []  # nothing downloaded
        assert OsmExtract(path).stops_near([(LAT, lon(1))], 10, "bus")
