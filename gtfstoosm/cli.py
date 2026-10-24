"""
Command-line interface for the GTFS to OSM converter.

This module provides a CLI for converting GTFS feeds to OSM relations.
"""

import argparse
import logging
import os
import sys
from datetime import date

from gtfstoosm.convert import convert_gtfs_to_osm
from gtfstoosm.modes import SUPPORTED_MODES
from gtfstoosm.utils import parse_tag_string
from gtfstoosm.validation import blocking_errors, log_report, read_report


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
        description="Convert GTFS transit feeds to OpenStreetMap relations"
    )

    parser.add_argument(
        "--input",
        "-i",
        dest="input_feed",
        required=True,
        help="Path to the GTFS feed zip file",
    )

    parser.add_argument(
        "--output",
        "-o",
        dest="output_file",
        required=True,
        help="Path to write the OSM XML file",
    )

    parser.add_argument(
        "--exclude-stops",
        dest="exclude_stops",
        action="store_true",
        default=False,
        help="Exclude stops from the output",
    )

    parser.add_argument(
        "--exclude-routes",
        dest="exclude_routes",
        action="store_true",
        default=False,
        help="Exclude routes from the output",
    )

    parser.add_argument(
        "--add-missing-stops",
        dest="add_missing_stops",
        action="store_true",
        default=False,
        help="Add stops missing from the database to the output (default: False)",
    )

    # parser.add_argument(
    #     "--route-types",
    #     dest="route_types",
    #     type=int,
    #     nargs="+",
    #     help="Only include routes with these GTFS route_type values (space-separated). Only bus routes are currently supported.",
    # )

    # parser.add_argument(
    #     "--agency",
    #     dest="agency_id",
    #     type=str,
    #     help="Only include routes for this agency ID",
    # )

    parser.add_argument(
        "--stop-search-radius",
        dest="stop_search_radius",
        type=float,
        default=10.0,
        help="Radius to search for stops (in meters)",
    )

    parser.add_argument(
        "--add-route-direction",
        dest="add_route_direction",
        action="store_true",
        default=False,
        help="Add route direction to the output (default: False)",
    )

    parser.add_argument(
        "--route-ref-pattern",
        dest="route_ref_pattern",
        type=str,
        help="Regex pattern to filter routes by route_id (e.g., '^[0-9]+$' for numeric routes only)",
    )

    parser.add_argument(
        "--relation-tags",
        dest="relation_tags",
        type=str,
        help="Semicolon-separated list of tags to add to the route and route_master relations (e.g., 'operator=TransitCenter;network=Whoville Bus;network:wikidata=Q123')",
    )

    parser.add_argument(
        "--feed-id",
        dest="feed_id",
        type=str,
        help="Identifier of this feed (e.g. 'FR-IDF-IDFM'). Adds gtfs:feed, gtfs:route_id and gtfs:trip_id:sample tags to route relations so they can be compared with the feed later",
    )

    parser.add_argument(
        "--ptv2-names",
        dest="ptv2_names",
        action="store_true",
        default=False,
        help="Name routes like 'bus 1: Origin => Destination' (OSM public transport v2 scheme)",
    )

    parser.add_argument(
        "--frequency-tags",
        dest="frequency_tags",
        action="store_true",
        default=False,
        help="Add interval, opening_hours and interval:conditional to route relations, estimated from frequencies.txt or the timetable (only when the service is regular)",
    )

    parser.add_argument(
        "--osm-file",
        dest="osm_file",
        type=str,
        metavar="FILE",
        help="Read stops and ways from this OSM extract (.osm.pbf, .osm...) instead of querying Overpass and Valhalla. Nothing is sent over the network. Cut it to the area of the feed first",
    )

    parser.add_argument(
        "--validation-report",
        dest="validation_report",
        type=str,
        metavar="report.json",
        help="Report of the MobilityData GTFS validator for this feed. The conversion stops if it contains errors (see --ignore-validation-codes)",
    )

    parser.add_argument(
        "--ignore-validation-codes",
        dest="ignore_validation_codes",
        nargs="+",
        default=[],
        metavar="CODE",
        help="Validator error codes that do not stop the conversion",
    )

    parser.add_argument(
        "--modes",
        dest="modes",
        nargs="+",
        choices=SUPPORTED_MODES,
        metavar="MODE",
        help="Modes to convert, among: "
        + ", ".join(SUPPORTED_MODES)
        + " (default: all of them)",
    )

    parser.add_argument(
        "--active-on",
        dest="active_on",
        type=parse_cli_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Leave out routes without service from this day on, i.e. closed lines (default: today)",
    )

    parser.add_argument(
        "--include-inactive",
        dest="include_inactive",
        action="store_true",
        default=False,
        help="Convert routes even if they no longer have service",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

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

    try:
        # Validate input file
        if not os.path.exists(parsed_args.input_feed):
            logger.error(f"Input GTFS feed not found: {parsed_args.input_feed}")
            return 1

        # Validate add missing stops
        if parsed_args.add_missing_stops and parsed_args.exclude_stops:
            logger.error("Cannot add missing stops without including stops")
            return 1

        # Validate something is included
        if parsed_args.exclude_stops and parsed_args.exclude_routes:
            logger.error("Nothing to convert")
            return 1

        if parsed_args.stop_search_radius < 0:
            logger.error("Stop search radius must be a positive number")
            return 1

        if parsed_args.stop_search_radius > 10:
            logger.warning("Stop search radius is too large, reverting to 10 meters")
            parsed_args.stop_search_radius = 10

        if parsed_args.osm_file and not os.path.exists(parsed_args.osm_file):
            logger.error(f"OSM extract not found: {parsed_args.osm_file}")
            return 1

        if parsed_args.validation_report:
            try:
                notices = read_report(parsed_args.validation_report)
            except (OSError, ValueError) as err:
                logger.error(f"Cannot read the validation report: {err}")
                return 1
            ignored = set(parsed_args.ignore_validation_codes)
            log_report(notices, ignored)
            if blocking_errors(notices, ignored):
                logger.error(
                    "The GTFS validator found errors in this feed, not converting it. "
                    "Fix the feed, or ignore codes with --ignore-validation-codes"
                )
                return 1

        # Validate output directory
        output_dir = os.path.dirname(parsed_args.output_file)
        if output_dir and not os.path.exists(output_dir):
            logger.info(f"Creating output directory: {output_dir}")
            os.makedirs(output_dir, exist_ok=True)

        # Convert GTFS to OSM
        options = {
            "exclude_stops": parsed_args.exclude_stops,
            "exclude_routes": parsed_args.exclude_routes,
            "add_missing_stops": parsed_args.add_missing_stops,
            "stop_search_radius": parsed_args.stop_search_radius,
            "add_route_direction": parsed_args.add_route_direction,
            "route_ref_pattern": parsed_args.route_ref_pattern,
            "frequency_tags": parsed_args.frequency_tags,
            "osm_file": parsed_args.osm_file,
            "modes": parsed_args.modes,
            "active_on": None
            if parsed_args.include_inactive
            else (parsed_args.active_on or date.today()),
            "feed_id": parsed_args.feed_id,
            "ptv2_names": parsed_args.ptv2_names,
            "relation_tags": parse_tag_string(parsed_args.relation_tags)
            if parsed_args.relation_tags
            else None,
            # "route_types": parsed_args.route_types,
            # "agency_id": parsed_args.agency_id,
        }

        logger.debug(f"CLI options: {options}")

        convert_gtfs_to_osm(parsed_args.input_feed, parsed_args.output_file, **options)

        logger.info("Conversion completed successfully")
        logger.info(f"Output OSM file: {parsed_args.output_file}")
        return 0

    except Exception as e:
        logger.error(f"Conversion failed: {str(e)}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
