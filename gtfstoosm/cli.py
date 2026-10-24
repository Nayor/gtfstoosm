"""
Command-line interface for the GTFS to OSM converter.

Giving the feed is enough: `python -m gtfstoosm.cli feed.zip` writes feed.osc next to
it, with the OSM data around the stops downloaded (and cached) as needed.
"""

import argparse
import logging
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from gtfstoosm.convert import convert_gtfs_to_osm
from gtfstoosm.modes import SUPPORTED_MODES
from gtfstoosm.utils import parse_tag_string, slugify
from gtfstoosm.validation import blocking_errors, log_report, read_report
from gtfstoosm.validator import find_validator, run_validator

# Largest distance (in metres) between a GTFS stop and the OSM stop it is matched to
ONLINE_RADIUS_LIMIT = 10  # Overpass has to look around every stop: slow when large
OFFLINE_RADIUS_LIMIT = 100  # in a local extract this only avoids unrelated matches


def parse_cli_date(value: str) -> date:
    """Parse a YYYY-MM-DD command-line date."""
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date, expected YYYY-MM-DD"
        ) from err


def setup_logging(verbose: bool = False) -> None:
    """
    Set up logging configuration.

    Args:
        verbose: Whether to enable verbose logging
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """
    Parse command-line arguments.

    Args:
        args: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description=(
            "Convert a GTFS feed to OpenStreetMap public transport data (an .osc "
            "file to review in JOSM). Only the feed is needed: the OSM data around "
            "its stops is downloaded and cached automatically."
        )
    )

    parser.add_argument("feed", nargs="?", help="GTFS feed (.zip)")
    parser.add_argument(
        "--input",
        "-i",
        dest="input_feed",
        help="GTFS feed (.zip); same as the positional argument",
    )
    parser.add_argument(
        "--output",
        "-o",
        dest="output_file",
        help="Output file (default: the feed's name with .osm, or .osc for --format osc)",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=["osm", "osc", "both", "maproulette"],
        default="osm",
        help="osm: the file format of JOSM (default); osc: osmChange, for other tools "
        "(osmium, scripts); both: the two, side by side; maproulette: JOSM's format "
        "without the route_master relations, to make a MapRoulette challenge (see the README)",
    )

    selection = parser.add_argument_group("what to convert")
    selection.add_argument(
        "--only",
        choices=["stops", "routes"],
        help="Only the stops, or only the routes (default: both)",
    )
    selection.add_argument(
        "--modes",
        nargs="+",
        choices=SUPPORTED_MODES,
        metavar="MODE",
        help="Modes to convert, among: "
        + ", ".join(SUPPORTED_MODES)
        + " (default: all of them)",
    )
    selection.add_argument(
        "--route-ref-pattern",
        dest="route_ref_pattern",
        metavar="REGEX",
        help="Only convert routes whose route_id matches (e.g. '^[0-9]+$')",
    )
    selection.add_argument(
        "--include-inactive",
        dest="include_inactive",
        action="store_true",
        help="Also convert routes that no longer have service (closed lines)",
    )
    selection.add_argument(
        "--no-new-stops",
        dest="no_new_stops",
        action="store_true",
        help="Do not create the stops that are missing from OSM",
    )
    selection.add_argument(
        "--no-service-tags",
        dest="no_service_tags",
        action="store_true",
        help="Do not estimate duration, interval and opening_hours from the timetable",
    )

    tags = parser.add_argument_group("tags (guessed from the feed when not given)")
    tags.add_argument(
        "--feed-id",
        dest="feed_id",
        help="Identifier of the feed, written in gtfs:feed (default: the feed's file name)",
    )
    tags.add_argument(
        "--network", help="network=* of the routes (default: the agency's name)"
    )
    tags.add_argument(
        "--operator", help="operator=* of the routes (default: the agency's name)"
    )
    tags.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Another tag for the route relations; can be repeated",
    )

    data = parser.add_argument_group("OpenStreetMap data")
    data.add_argument(
        "--osm-file",
        dest="osm_file",
        metavar="FILE",
        help="Use this OSM extract (.osm.pbf, .osm...) instead of downloading one",
    )
    data.add_argument(
        "--refresh",
        action="store_true",
        help="Download the OSM data again even if the cache is recent",
    )
    data.add_argument(
        "--online",
        action="store_true",
        help="Ask Overpass and Valhalla instead of reading an extract (slow, "
        "uses public servers: only for small networks)",
    )
    data.add_argument(
        "--stop-search-radius",
        dest="stop_search_radius",
        type=float,
        default=10.0,
        metavar="METRES",
        help="Distance within which a GTFS stop is matched to an OSM stop (default: "
        f"10; at most {ONLINE_RADIUS_LIMIT} with --online, {OFFLINE_RADIUS_LIMIT} "
        "otherwise)",
    )

    checks = parser.add_argument_group("checking the feed")
    checks.add_argument(
        "--no-validate",
        dest="no_validate",
        action="store_true",
        help="Do not check the feed with the GTFS validator (the full Docker image "
        "carries it and checks every feed)",
    )
    checks.add_argument(
        "--validation-report",
        dest="validation_report",
        metavar="report.json",
        help="Use this report of the MobilityData GTFS validator instead of running it",
    )
    checks.add_argument(
        "--ignore-validation",
        dest="ignore_validation",
        nargs="+",
        default=[],
        metavar="CODE",
        help="Error codes of the validation report that do not stop the conversion",
    )

    parser.add_argument(
        "--active-on",
        dest="active_on",
        type=parse_cli_date,
        default=None,
        help=argparse.SUPPRESS,  # for tests: replaces today in the closed-line filter
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Detailed log")

    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        args: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    parsed_args = parse_args(args)
    setup_logging(parsed_args.verbose)

    logger = logging.getLogger(__name__)
    logger.info("Starting GTFS to OSM conversion")

    feed = parsed_args.input_feed or parsed_args.feed
    if not feed:
        logger.error("Give the GTFS feed to convert, e.g. feed.zip")
        return 1
    suffix = {"osc": ".osc", "maproulette": ".maproulette.osm"}.get(
        parsed_args.output_format, ".osm"
    )
    output_file = parsed_args.output_file or str(Path(feed).with_suffix(suffix))
    report_dir = None  # where the validator wrote its report, removed at the end

    try:
        # Validate input file
        if not os.path.exists(feed):
            logger.error(f"Input GTFS feed not found: {feed}")
            return 1

        if parsed_args.stop_search_radius < 0:
            logger.error("Stop search radius must be a positive number")
            return 1

        radius_limit = (
            ONLINE_RADIUS_LIMIT if parsed_args.online else OFFLINE_RADIUS_LIMIT
        )
        if parsed_args.stop_search_radius > radius_limit:
            logger.warning(
                f"Stop search radius is too large, reverting to {radius_limit} meters"
            )
            parsed_args.stop_search_radius = radius_limit

        if parsed_args.osm_file and parsed_args.online:
            logger.error("Use either --osm-file or --online, not both")
            return 1
        if parsed_args.osm_file and not os.path.exists(parsed_args.osm_file):
            logger.error(f"OSM extract not found: {parsed_args.osm_file}")
            return 1

        report_path = parsed_args.validation_report
        if not report_path and not parsed_args.no_validate:
            if find_validator() is None:
                logger.info(
                    "The GTFS validator is not installed: the feed is not checked"
                )
            else:
                report_dir = tempfile.mkdtemp(prefix="gtfstoosm-validation-")
                try:
                    report_path = str(run_validator(feed, Path(report_dir)))
                except RuntimeError as err:
                    logger.error(str(err))
                    return 1

        if report_path:
            try:
                notices = read_report(report_path)
            except (OSError, ValueError) as err:
                logger.error(f"Cannot read the validation report: {err}")
                return 1
            ignored = set(parsed_args.ignore_validation)
            if report_dir and (Path(report_dir) / "report.html").exists():
                html = Path(output_file).with_suffix(".validation.html")
                html.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(Path(report_dir) / "report.html", html)
                logger.info(f"Validation report: {html}")
            log_report(notices, ignored)
            if blocking_errors(notices, ignored):
                logger.error(
                    "The GTFS validator found errors in this feed, not converting it. "
                    "Fix the feed, or ignore codes with --ignore-validation"
                )
                return 1

        # Validate output directory
        output_dir = os.path.dirname(output_file)
        if output_dir and not os.path.exists(output_dir):
            logger.info(f"Creating output directory: {output_dir}")
            os.makedirs(output_dir, exist_ok=True)

        relation_tags: dict[str, str] = {}
        for entry in parsed_args.tag:
            relation_tags.update(parse_tag_string(entry))
        if parsed_args.network:
            relation_tags["network"] = parsed_args.network
        if parsed_args.operator:
            relation_tags["operator"] = parsed_args.operator

        # Convert GTFS to OSM
        options = {
            "exclude_stops": parsed_args.only == "routes",
            "exclude_routes": parsed_args.only == "stops",
            "add_missing_stops": not parsed_args.no_new_stops
            and parsed_args.only != "routes",
            "stop_search_radius": parsed_args.stop_search_radius,
            "route_ref_pattern": parsed_args.route_ref_pattern,
            "frequency_tags": not parsed_args.no_service_tags,
            "ptv2_names": True,
            "agency_tags": True,
            "feed_id": parsed_args.feed_id or slugify(Path(feed).stem),
            "relation_tags": relation_tags or None,
            "modes": parsed_args.modes,
            "active_on": None
            if parsed_args.include_inactive
            else (parsed_args.active_on or date.today()),
            "osm_file": parsed_args.osm_file,
            "auto_extract": not parsed_args.online and not parsed_args.osm_file,
            "refresh": parsed_args.refresh,
            "output_format": parsed_args.output_format,
        }

        logger.debug(f"CLI options: {options}")

        convert_gtfs_to_osm(feed, output_file, **options)

        logger.info("Conversion completed successfully")
        logger.info(f"Output OSM file: {output_file}")
        return 0

    except Exception as e:
        logger.error(f"Conversion failed: {str(e)}", exc_info=True)
        return 1
    finally:
        if report_dir:
            shutil.rmtree(report_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
