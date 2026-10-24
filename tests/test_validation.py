"""Reading the report of the GTFS validator (offline, network stubbed)."""

import json
import zipfile

import pytest

from gtfstoosm.cli import main
from gtfstoosm.convert import OSMRelationBuilder
from gtfstoosm.validation import Notice, blocking_errors, read_report


def notice(code, severity, total=1):
    return {
        "code": code,
        "severity": severity,
        "totalNotices": total,
        "sampleNotices": [],
    }


def write_report(path, notices):
    path.write_text(json.dumps({"summary": {}, "notices": notices}), encoding="utf8")
    return path


class TestReadReport:
    def test_reads_notices(self, tmp_path):
        report = write_report(
            tmp_path / "report.json",
            [notice("foreign_key_violation", "ERROR", 3), notice("x", "INFO")],
        )
        assert read_report(str(report)) == [
            Notice("foreign_key_violation", "ERROR", 3),
            Notice("x", "INFO", 1),
        ]

    def test_not_a_report(self, tmp_path):
        path = tmp_path / "other.json"
        path.write_text('{"hello": 1}', encoding="utf8")
        with pytest.raises(ValueError, match="not a GTFS validator report"):
            read_report(str(path))

    def test_blocking_errors(self):
        notices = [
            Notice("a", "ERROR", 1),
            Notice("b", "ERROR", 2),
            Notice("c", "WARNING", 9),
        ]
        assert blocking_errors(notices) == notices[:2]
        assert blocking_errors(notices, {"a"}) == [notices[1]]
        assert blocking_errors(notices, {"a", "b"}) == []


@pytest.fixture
def feed_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
    )
    path = tmp_path / "feed.zip"
    files = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
        "A,Agence,https://example.org,Europe/Paris\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,44.5,6.2\nS2,Ecoles,44.6,6.3\n",
        "routes.txt": "route_id,route_short_name,route_type\nR,1,3\n",
        "trips.txt": "route_id,service_id,trip_id\nR,S,T\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T,08:00:00,08:00:00,S1,1\nT,08:10:00,08:10:00,S2,2\n",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def convert(feed_zip, tmp_path, report, *extra):
    out = tmp_path / "out.osc"
    code = main(
        ["-i", str(feed_zip), "-o", str(out), "--include-inactive", "--online"]
        + (["--validation-report", str(report)] if report else [])
        + list(extra)
    )
    return code, out


class TestCommandLine:
    def test_warnings_do_not_stop_the_conversion(self, feed_zip, tmp_path):
        report = write_report(
            tmp_path / "r.json", [notice("expired_calendar", "WARNING")]
        )
        code, out = convert(feed_zip, tmp_path, report)
        assert code == 0
        assert out.exists()

    def test_errors_stop_the_conversion(self, feed_zip, tmp_path):
        report = write_report(
            tmp_path / "r.json", [notice("foreign_key_violation", "ERROR", 2)]
        )
        code, out = convert(feed_zip, tmp_path, report)
        assert code == 1
        assert not out.exists()

    def test_ignored_codes_do_not_stop_it(self, feed_zip, tmp_path):
        report = write_report(tmp_path / "r.json", [notice("invalid_url", "ERROR")])
        code, out = convert(
            feed_zip, tmp_path, report, "--ignore-validation", "invalid_url"
        )
        assert code == 0
        assert out.exists()

    def test_unreadable_report(self, feed_zip, tmp_path):
        code, out = convert(feed_zip, tmp_path, tmp_path / "missing.json")
        assert code == 1
        assert not out.exists()

    def test_no_report_means_no_check(self, feed_zip, tmp_path):
        code, _ = convert(feed_zip, tmp_path, None)
        assert code == 0
