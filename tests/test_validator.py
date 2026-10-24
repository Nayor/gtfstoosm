"""Running the GTFS validator from the command line (the Java program is stubbed)."""

import json
import logging
import subprocess
import zipfile
from pathlib import Path

import pytest

from gtfstoosm import cli, validator
from gtfstoosm.cli import main
from gtfstoosm.convert import OSMRelationBuilder


def write_report(out_dir: Path, notices: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps({"summary": {}, "notices": notices}), encoding="utf8"
    )
    (out_dir / "report.html").write_text("<html>report</html>", encoding="utf8")


def notice(code: str, severity: str) -> dict:
    return {"code": code, "severity": severity, "totalNotices": 1}


class TestFindValidator:
    def test_needs_java_and_the_jar(self, tmp_path, monkeypatch):
        jar = tmp_path / "validator.jar"
        jar.write_bytes(b"x")
        monkeypatch.setenv("GTFS_VALIDATOR_JAR", str(jar))

        monkeypatch.setattr(validator.shutil, "which", lambda name: "/usr/bin/java")
        assert validator.find_validator() == ("/usr/bin/java", str(jar))

        monkeypatch.setattr(validator.shutil, "which", lambda name: None)
        assert validator.find_validator() is None

    def test_no_jar(self, monkeypatch):
        monkeypatch.setattr(validator.shutil, "which", lambda name: "/usr/bin/java")
        monkeypatch.setenv("GTFS_VALIDATOR_JAR", "/nowhere/validator.jar")
        assert validator.find_validator() is None


class TestRunValidator:
    @pytest.fixture
    def installed(self, monkeypatch):
        monkeypatch.setattr(validator, "find_validator", lambda: ("java", "v.jar"))

    def test_runs_java_on_the_feed(self, tmp_path, monkeypatch, installed):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            write_report(Path(command[command.index("-o") + 1]), [])
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(validator.subprocess, "run", fake_run)
        report = validator.run_validator("feed.zip", tmp_path / "out")

        assert report == tmp_path / "out" / "report.json"
        assert calls[0][:4] == ["java", "-jar", "v.jar", "-i"]
        assert "feed.zip" in calls[0]

    def test_a_failure_says_why(self, tmp_path, monkeypatch, installed):
        monkeypatch.setattr(
            validator.subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(
                command, 1, "", "OutOfMemoryError: Java heap space"
            ),
        )
        with pytest.raises(RuntimeError, match="OutOfMemoryError"):
            validator.run_validator("feed.zip", tmp_path / "out")

    def test_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(validator, "find_validator", lambda: None)
        with pytest.raises(RuntimeError, match="not installed"):
            validator.run_validator("feed.zip", tmp_path / "out")


@pytest.fixture
def feed_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OSMRelationBuilder, "_get_stop_objects", lambda self, *a, **k: []
    )
    files = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
        "A,Agence,https://example.org,Europe/Paris\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,44.5,6.2\nS2,Ecoles,44.6,6.3\n",
        "routes.txt": "route_id,route_short_name,route_type\nR,1,3\n",
        "trips.txt": "route_id,service_id,trip_id\nR,S,T\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T,08:00:00,08:00:00,S1,1\nT,08:10:00,08:10:00,S2,2\n",
    }
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


@pytest.fixture
def validate(monkeypatch):
    """The validator is installed and finds the given notices; `ran` counts its runs."""

    class Setup:
        notices: list[dict] = []
        ran = 0

    def fake_run(feed, out_dir):
        Setup.ran += 1
        write_report(out_dir, Setup.notices)
        return out_dir / "report.json"

    Setup.notices = []
    Setup.ran = 0
    monkeypatch.setattr(cli, "find_validator", lambda: ("java", "v.jar"))
    monkeypatch.setattr(cli, "run_validator", fake_run)
    return Setup


def convert(feed_zip, tmp_path, *extra):
    out = tmp_path / "out.osc"
    code = main([str(feed_zip), "-o", str(out), "--online", *extra])
    return code, out


class TestCommandLine:
    def test_a_valid_feed_is_converted_and_the_report_kept(
        self, feed_zip, tmp_path, validate
    ):
        validate.notices = [notice("expired_calendar", "WARNING")]
        code, out = convert(feed_zip, tmp_path)
        assert code == 0
        assert out.exists()
        assert (
            (tmp_path / "out.validation.html")
            .read_text(encoding="utf8")
            .startswith("<html>")
        )

    def test_errors_stop_the_conversion(self, feed_zip, tmp_path, validate, caplog):
        validate.notices = [notice("foreign_key_violation", "ERROR")]
        with caplog.at_level(logging.ERROR):
            code, out = convert(feed_zip, tmp_path)
        assert code == 1
        assert not out.exists()
        assert "found errors" in caplog.text

    def test_ignored_codes_let_it_through(self, feed_zip, tmp_path, validate):
        validate.notices = [notice("invalid_url", "ERROR")]
        code, out = convert(feed_zip, tmp_path, "--ignore-validation", "invalid_url")
        assert code == 0
        assert out.exists()

    def test_no_validate(self, feed_zip, tmp_path, validate):
        validate.notices = [notice("foreign_key_violation", "ERROR")]
        code, out = convert(feed_zip, tmp_path, "--no-validate")
        assert code == 0
        assert validate.ran == 0

    def test_a_given_report_replaces_the_run(self, feed_zip, tmp_path, validate):
        report_dir = tmp_path / "given"
        write_report(report_dir, [])
        code, _ = convert(
            feed_zip, tmp_path, "--validation-report", str(report_dir / "report.json")
        )
        assert code == 0
        assert validate.ran == 0

    def test_without_the_validator_the_feed_is_not_checked(
        self, feed_zip, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(cli, "find_validator", lambda: None)
        with caplog.at_level(logging.INFO):
            code, out = convert(feed_zip, tmp_path)
        assert code == 0
        assert "validator is not installed" in caplog.text

    def test_a_validator_that_fails_stops_the_conversion(
        self, feed_zip, tmp_path, monkeypatch, validate
    ):
        def broken(feed, out_dir):
            raise RuntimeError("The GTFS validator failed (exit 1): boom")

        monkeypatch.setattr(cli, "run_validator", broken)
        code, out = convert(feed_zip, tmp_path)
        assert code == 1
        assert not out.exists()

    def test_the_temporary_report_is_removed(
        self, feed_zip, tmp_path, validate, monkeypatch
    ):
        folders = []
        real = cli.tempfile.mkdtemp

        def spy(*args, **kwargs):
            folders.append(real(*args, **kwargs))
            return folders[-1]

        monkeypatch.setattr(cli.tempfile, "mkdtemp", spy)
        convert(feed_zip, tmp_path)
        assert folders
        assert not Path(folders[0]).exists()
