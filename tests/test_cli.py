"""Tests for the CLI module."""

import logging
import sys
from datetime import date
from unittest.mock import patch

import pytest

from gtfstoosm.cli import main, parse_args, setup_logging


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_info_level(self, caplog):
        """Test that logging is set to INFO by default."""
        with caplog.at_level(logging.INFO):
            setup_logging(verbose=False)
            logger = logging.getLogger(__name__)

            # Verify INFO level messages are captured
            logger.info("Test info message")
            assert "Test info message" in caplog.text

            # Verify DEBUG messages are not captured at INFO level
            logger.debug("Test debug message")
            assert "Test debug message" not in caplog.text

    def test_setup_logging_debug_level(self, caplog):
        """Test that logging is set to DEBUG when verbose=True."""
        with caplog.at_level(logging.DEBUG):
            setup_logging(verbose=True)
            logger = logging.getLogger(__name__)

            # Verify both DEBUG and INFO messages are captured
            logger.debug("Test debug message")
            logger.info("Test info message")
            assert "Test debug message" in caplog.text
            assert "Test info message" in caplog.text

    def test_setup_logging_format(self, caplog):
        """Test that logging format includes expected components."""
        with caplog.at_level(logging.INFO):
            setup_logging(verbose=False)
            logger = logging.getLogger(__name__)
            logger.info("Test message")

            # The format should include timestamp, name, level, and message
            # We can't test exact format due to timestamp, but we can verify components exist
            assert "INFO" in caplog.text
            assert "Test message" in caplog.text


class TestParseArgs:
    """Tests for parse_args function."""

    def test_only_the_feed_is_needed(self):
        args = parse_args(["feed.zip"])
        assert args.feed == "feed.zip"
        assert args.input_feed is None
        assert args.output_file is None
        assert args.only is None
        assert args.modes is None
        assert args.osm_file is None
        assert args.online is False
        assert args.refresh is False
        assert args.no_new_stops is False
        assert args.no_service_tags is False
        assert args.stop_search_radius == 10.0
        assert args.route_ref_pattern is None
        assert args.tag == []
        assert args.verbose is False

    def test_input_and_output_options(self):
        args = parse_args(["-i", "input.zip", "-o", "output.osc"])
        assert args.input_feed == "input.zip"
        assert args.output_file == "output.osc"
        args = parse_args(["--input", "input.zip", "--output", "output.osc"])
        assert args.input_feed == "input.zip"
        assert args.output_file == "output.osc"

    def test_only(self):
        assert parse_args(["f.zip", "--only", "stops"]).only == "stops"
        assert parse_args(["f.zip", "--only", "routes"]).only == "routes"
        with pytest.raises(SystemExit):
            parse_args(["f.zip", "--only", "everything"])

    def test_removed_options_are_refused(self):
        for option in (
            "--exclude-stops",
            "--exclude-routes",
            "--add-missing-stops",
            "--add-route-direction",
            "--ptv2-names",
            "--frequency-tags",
            "--relation-tags",
        ):
            with pytest.raises(SystemExit):
                parse_args(["f.zip", option])

    def test_stop_search_radius(self):
        args = parse_args(["f.zip", "--stop-search-radius", "15.5"])
        assert args.stop_search_radius == 15.5

    def test_route_ref_pattern(self):
        args = parse_args(["f.zip", "--route-ref-pattern", "^[0-9]+$"])
        assert args.route_ref_pattern == "^[0-9]+$"

    def test_tags_can_be_repeated(self):
        args = parse_args(["f.zip", "--tag", "a=1", "--tag", "b=2", "--network", "N"])
        assert args.tag == ["a=1", "b=2"]
        assert args.network == "N"

    def test_verbose(self):
        assert parse_args(["f.zip", "--verbose"]).verbose is True
        assert parse_args(["f.zip", "-v"]).verbose is True


@pytest.fixture
def convert():
    """The conversion, replaced by a recorder."""
    with patch("gtfstoosm.cli.convert_gtfs_to_osm") as mock:
        yield mock


@pytest.fixture
def feed(tmp_path):
    path = tmp_path / "My Feed (2026).zip"
    path.touch()
    return path


class TestMain:
    """Tests for main function."""

    def test_the_feed_alone_is_enough(self, convert, feed):
        assert main([str(feed)]) == 0

        convert.assert_called_once()
        call_args = convert.call_args
        assert call_args[0][0] == str(feed)
        assert call_args[0][1] == str(feed.with_suffix(".osm"))

    def test_defaults_are_the_automatic_flow(self, convert, feed):
        assert main([str(feed)]) == 0

        options = convert.call_args[1]
        assert options["auto_extract"] is True
        assert options["osm_file"] is None
        assert options["add_missing_stops"] is True
        assert options["exclude_stops"] is False
        assert options["exclude_routes"] is False
        assert options["frequency_tags"] is True
        assert options["ptv2_names"] is True
        assert options["agency_tags"] is True
        assert options["feed_id"] == "my-feed-2026"
        assert options["relation_tags"] is None
        assert options["active_on"] == date.today()
        assert options["refresh"] is False
        assert options["output_format"] == "osm"

    def test_the_format_decides_the_default_file_name(self, convert, feed):
        main([str(feed), "--format", "osc"])
        assert convert.call_args[0][1] == str(feed.with_suffix(".osc"))
        assert convert.call_args[1]["output_format"] == "osc"

        main([str(feed), "--format", "maproulette"])
        assert convert.call_args[0][1].endswith(".maproulette.osm")

        main([str(feed), "--format", "both"])
        assert convert.call_args[0][1] == str(feed.with_suffix(".osm"))
        assert convert.call_args[1]["output_format"] == "both"

    def test_input_option_and_output_option(self, convert, feed, tmp_path):
        out = tmp_path / "result.osc"
        assert main(["--input", str(feed), "--output", str(out)]) == 0
        assert convert.call_args[0][:2] == (str(feed), str(out))

    def test_no_feed(self, convert, caplog):
        with caplog.at_level(logging.ERROR):
            assert main([]) == 1
        assert "Give the GTFS feed" in caplog.text
        convert.assert_not_called()

    def test_input_not_found(self, convert, caplog):
        with caplog.at_level(logging.ERROR):
            assert main(["nonexistent.zip"]) == 1
        assert "Input GTFS feed not found" in caplog.text
        convert.assert_not_called()

    def test_only_stops_and_only_routes(self, convert, feed):
        main([str(feed), "--only", "stops"])
        options = convert.call_args[1]
        assert options["exclude_routes"] is True
        assert options["exclude_stops"] is False
        assert options["add_missing_stops"] is True

        main([str(feed), "--only", "routes"])
        options = convert.call_args[1]
        assert options["exclude_stops"] is True
        assert options["exclude_routes"] is False
        assert options["add_missing_stops"] is False

    def test_opting_out(self, convert, feed):
        main([str(feed), "--no-new-stops", "--no-service-tags", "--include-inactive"])
        options = convert.call_args[1]
        assert options["add_missing_stops"] is False
        assert options["frequency_tags"] is False
        assert options["active_on"] is None

    def test_where_the_osm_data_comes_from(self, convert, feed, tmp_path):
        osm = tmp_path / "area.osm"
        osm.touch()

        main([str(feed), "--osm-file", str(osm)])
        options = convert.call_args[1]
        assert options["osm_file"] == str(osm)
        assert options["auto_extract"] is False

        main([str(feed), "--online"])
        options = convert.call_args[1]
        assert options["osm_file"] is None
        assert options["auto_extract"] is False

        main([str(feed), "--refresh"])
        assert convert.call_args[1]["refresh"] is True

    def test_osm_file_and_online_are_exclusive(self, convert, feed, tmp_path, caplog):
        osm = tmp_path / "area.osm"
        osm.touch()
        with caplog.at_level(logging.ERROR):
            assert main([str(feed), "--osm-file", str(osm), "--online"]) == 1
        assert "either --osm-file or --online" in caplog.text
        convert.assert_not_called()

    def test_missing_osm_file(self, convert, feed, caplog):
        with caplog.at_level(logging.ERROR):
            assert main([str(feed), "--osm-file", "nope.osm"]) == 1
        assert "OSM extract not found" in caplog.text
        convert.assert_not_called()

    def test_negative_search_radius(self, convert, feed, caplog):
        with caplog.at_level(logging.ERROR):
            assert main([str(feed), "--stop-search-radius", "-5"]) == 1
        assert "Stop search radius must be a positive number" in caplog.text
        convert.assert_not_called()

    def test_search_radius_limits(self, convert, feed, caplog):
        with caplog.at_level(logging.WARNING):
            main([str(feed), "--online", "--stop-search-radius", "50"])
        assert "reverting to 10 meters" in caplog.text
        assert convert.call_args[1]["stop_search_radius"] == 10

        main([str(feed), "--stop-search-radius", "50"])
        assert convert.call_args[1]["stop_search_radius"] == 50

        main([str(feed), "--stop-search-radius", "500"])
        assert convert.call_args[1]["stop_search_radius"] == 100

    def test_creates_output_directory(self, convert, feed, tmp_path, caplog):
        output_dir = tmp_path / "new_dir" / "subdir"
        with caplog.at_level(logging.INFO):
            assert main([str(feed), "-o", str(output_dir / "output.osc")]) == 0
        assert output_dir.exists()
        assert f"Creating output directory: {output_dir}" in caplog.text

    def test_tags(self, convert, feed):
        main(
            [
                str(feed),
                "--tag",
                "network:wikidata=Q123",
                "--tag",
                "fee=no",
                "--network",
                "City Bus",
                "--operator",
                "Transit",
                "--feed-id",
                "XX-CITY",
            ]
        )
        options = convert.call_args[1]
        assert options["relation_tags"] == {
            "network:wikidata": "Q123",
            "fee": "no",
            "network": "City Bus",
            "operator": "Transit",
        }
        assert options["feed_id"] == "XX-CITY"

    def test_filters(self, convert, feed):
        main(
            [
                str(feed),
                "--modes",
                "tram",
                "train",
                "--route-ref-pattern",
                "^[0-9]+$",
                "--active-on",
                "2026-10-24",
            ]
        )
        options = convert.call_args[1]
        assert options["modes"] == ["tram", "train"]
        assert options["route_ref_pattern"] == "^[0-9]+$"
        assert options["active_on"] == date(2026, 10, 24)

    def test_handles_conversion_exception(self, convert, feed, caplog):
        convert.side_effect = ValueError("Test error")
        with caplog.at_level(logging.ERROR):
            assert main([str(feed)]) == 1
        assert "Conversion failed: Test error" in caplog.text

    def test_verbose_logging(self, convert, feed, caplog):
        with caplog.at_level(logging.DEBUG):
            assert main([str(feed), "--verbose"]) == 0
        assert "CLI options:" in caplog.text

    def test_main_with_none_args_uses_sys_argv(self, convert, feed):
        with patch.object(sys, "argv", ["gtfstoosm", str(feed)]):
            assert main() == 0
