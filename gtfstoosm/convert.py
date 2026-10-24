"""
Core module for converting GTFS feeds to OSM relations.

This module contains the main functionality for reading GTFS data and
converting it to OSM relations that can be imported into OpenStreetMap.
"""

import itertools
import logging
import time
from datetime import date
from pathlib import Path

import polars as pl
import requests

from gtfstoosm.compare import RouteMatch
from gtfstoosm.extracts import prepare_extract
from gtfstoosm.frequencies import (
    build_timetable,
    pattern_service_tags,
    service_periods,
)
from gtfstoosm.gtfs import GTFSFeed
from gtfstoosm.modes import (
    ROAD_MODES,
    SUPPORTED_MODES,
    new_stop_tags,
    osm_mode,
    stop_selectors,
)
from gtfstoosm.osm import OSMElement, OSMNode, OSMRelation
from gtfstoosm.osm_data import OsmExtract
from gtfstoosm.services import active_service_ids
from gtfstoosm.update import compare_with_osm
from gtfstoosm.utils import (
    Trip,
    calculate_direction,
    deduplicate_trips,
    format_colour,
    format_name,
    pattern_key,
)

logger = logging.getLogger(__name__)

# A seasonal service that ended less than this many days ago is not a closed line
SEASON_RETURN_DAYS = 365


class OSMRelationBuilder:
    """Class for building OSM relations from GTFS data."""

    def __init__(
        self,
        exclude_stops: bool = False,
        exclude_routes: bool = False,
        add_missing_stops: bool = False,
        route_types: list[int] | None = None,
        agency_id: str | None = None,
        search_radius: float = 10.0,
        route_direction: bool = False,
        route_ref_pattern: str | None = None,
        relation_tags: dict[str, str] | None = None,
        feed_id: str | None = None,
        ptv2_names: bool = False,
        modes: list[str] | None = None,
        active_on: date | None = None,
        osm_extract: OsmExtract | None = None,
        frequency_tags: bool = False,
        agency_tags: bool = False,
        compare_existing: bool = True,
    ):
        """
        Initialize the OSM relation builder.
        """
        self.exclude_stops: bool = exclude_stops
        self.exclude_routes: bool = exclude_routes
        self.add_missing_stops: bool = add_missing_stops
        self.route_types: list[int] | None = route_types
        self.agency_id: str | None = agency_id
        self.relations: list[OSMRelation] = []
        self.nodes: list[OSMNode] = []
        self.new_stops: list[OSMNode] = []
        self.search_radius: float = search_radius
        self.route_direction: bool = route_direction
        self.route_ref_pattern: str | None = route_ref_pattern
        self.relation_tags: dict[str, str] | None = relation_tags
        self.feed_id: str | None = feed_id
        self.ptv2_names: bool = ptv2_names
        self.modes: list[str] = list(modes) if modes else list(SUPPORTED_MODES)
        self.active_on: date | None = active_on
        self.osm_extract: OsmExtract | None = osm_extract
        self.frequency_tags: bool = frequency_tags
        self.agency_tags: bool = agency_tags
        self._new_stop_node_ids: dict[str, int] = {}
        self._relation_ids = itertools.count(-1, -1)  # new relations: -1, -2, ...
        self.compare_existing: bool = compare_existing
        self.matches: list[RouteMatch] = []  # routes compared with the relations of OSM
        # what is changed in objects that are in OSM already (see update.py)
        self.modifications: list[OSMElement] = []
        self.stop_links: dict[int, set[str]] = {}  # OSM stop -> ids of the feed's stops
        self.matched_nodes: dict[
            int, OSMNode
        ] = {}  # the OSM stops they were matched with

    def __str__(self) -> str:
        # Exclude None values and internal collections
        exclude_attrs = {
            "relations",
            "nodes",
            "new_stops",
            "matches",
            "modifications",
            "stop_links",
            "matched_nodes",
        }
        attrs = [
            f"{k}={v!r}"
            for k, v in vars(self).items()
            if v is not None and not k.startswith("_") and k not in exclude_attrs
        ]
        return f"OSMRelationBuilder({', '.join(attrs)})"

    def __repr__(self) -> str:
        attrs = dict(vars(self).items())
        return (
            f"OSMRelationBuilder({', '.join(f'{k}={v!r}' for k, v in attrs.items())})"
        )

    def build_relations(self, gtfs_data: dict[str, pl.DataFrame]) -> None:
        """
        Build OSM relations from GTFS data.

        Args:
            gtfs_data: The GTFS data as returned by GTFSFeed.load()
        """
        logger.info("Building OSM relations from GTFS data")

        self._process_routes(gtfs_data)

        logger.info(
            f"Built {len(self.relations)} route relations and {len(self.nodes) + len(self.new_stops)} nodes"
        )

    def build_route_masters(self, gtfs_data: dict[str, pl.DataFrame]) -> None:
        """
        Create route_master relations for routes with the same ref.
        """
        logger.info("Creating route_master relations")

        # Route variants carry route_short_name as ref, which often differs from route_id
        made_routes = {variant.tags["ref"] for variant in self.relations}
        unique_routes = gtfs_data["routes"].filter(
            pl.col("route_short_name").cast(pl.Utf8).is_in(made_routes)
        )

        for unique_route in unique_routes.iter_rows(named=True):
            short_name = unique_route.get("route_short_name") or ""
            long_name = unique_route.get("route_long_name") or ""
            route_master_tags = {
                "type": "route_master",
                "route_master": osm_mode(unique_route["route_type"]) or "bus",
                "ref": short_name,
                "name": " ".join(
                    part for part in ("Route", short_name, long_name) if part
                ),
            }
            colour = format_colour(unique_route.get("route_color"))
            if colour:
                route_master_tags["colour"] = colour
            if self.agency_tags:
                route_master_tags.update(self._agency_tags(gtfs_data, unique_route))
            if self.feed_id:
                route_master_tags["gtfs:feed"] = self.feed_id
                route_master_tags["gtfs:route_id"] = str(unique_route["route_id"])

            if self.relation_tags:
                route_master_tags.update(self.relation_tags)

            master = OSMRelation(
                id=next(self._relation_ids),
                tags=route_master_tags,
            )
            for route in self.relations:
                if (
                    route.tags.get("ref") == unique_route["route_short_name"]
                    and route.tags.get("route") == route_master_tags["route_master"]
                ):
                    master.add_member(osm_type="relation", ref=route.id)
            self.relations.append(master)
        logger.info(
            f"Built {len([i for i in self.relations if i.tags.get('route_master')])} route_master relations"
        )

    def _process_routes(self, gtfs_data: dict[str, pl.DataFrame]) -> None:
        """
        Process GTFS routes and convert them to OSM relations.

        Args:
            gtfs_data: Complete GTFS data dictionary
            include_stops: Whether to include stops in the output relations
        """
        routes = gtfs_data["routes"]
        trips = gtfs_data["trips"]
        stop_times = gtfs_data["stop_times"]
        stops = gtfs_data["stops"]
        shapes = gtfs_data.get("shapes")
        if shapes is None:
            logger.warning("No shapes.txt in the feed: routes are written without ways")
        if "shape_id" not in trips.columns:
            trips = trips.with_columns(pl.lit(None, dtype=pl.Utf8).alias("shape_id"))

        logger.info(f"Processing {routes.height} routes")
        routes_with_trips = set(trips["route_id"].to_list())

        # Leave out services that no longer run (closed lines)
        if self.active_on is not None:
            active = active_service_ids(gtfs_data, self.active_on)
            if active is None:
                logger.warning(
                    "No calendar.txt or calendar_dates.txt: cannot tell closed lines apart"
                )
            else:
                # A seasonal service whose season ended less than a year ago is
                # expected to come back: it is not a closed line
                returning = {
                    service_id
                    for service_id, period in service_periods(gtfs_data).items()
                    if period
                    and (self.active_on - period[1]).days <= SEASON_RETURN_DAYS
                }
                all_routes = trips["route_id"].n_unique()
                trips = trips.filter(pl.col("service_id").is_in(active | returning))
                logger.info(
                    f"{all_routes - trips['route_id'].n_unique()} routes without "
                    f"service from {self.active_on} left out"
                    + (
                        f", {len(returning - active)} seasonal services kept"
                        if returning - active
                        else ""
                    )
                )

        # Filter routes by type if specified
        if self.route_types:
            routes = routes.filter(pl.col("route_type").is_in(self.route_types))

        # Keep the modes asked for, leave out those OSM does not map
        routes = routes.with_columns(
            pl.col("route_type")
            .map_elements(osm_mode, return_dtype=pl.Utf8)
            .alias("osm_mode")
        )
        unsupported = routes.filter(pl.col("osm_mode").is_null()).height
        if unsupported:
            logger.info(f"{unsupported} routes of an unsupported type left out")
        routes = routes.filter(pl.col("osm_mode").is_in(self.modes))

        # Filter routes by regex pattern if specified
        if self.route_ref_pattern is not None:
            routes_to_process = routes.filter(
                pl.col("route_id").cast(pl.Utf8).str.contains(self.route_ref_pattern)
            )
            logger.debug(
                f"Filtered to routes matching pattern '{self.route_ref_pattern}'"
            )
        else:
            routes_to_process = routes

        logger.info(f"Processing {routes_to_process.height} filtered routes")

        without_trips = set(routes_to_process["route_id"].to_list()) - routes_with_trips
        if without_trips:
            logger.warning(
                f"{len(without_trips)} routes have no trip at all, left out: "
                f"{', '.join(sorted(map(str, without_trips))[:10])}"
            )

        # Routes whose trips have no timetable (stop_times): no stops are known
        timetabled_trips = trips.filter(
            pl.col("trip_id").is_in(stop_times["trip_id"].unique().to_list())
        )
        no_timetable = set(
            routes_to_process.filter(
                pl.col("route_id").is_in(trips["route_id"].unique().to_list())
            )["route_id"].to_list()
        ) - set(timetabled_trips["route_id"].to_list())
        if no_timetable:
            logger.warning(
                f"{len(no_timetable)} routes have trips but no stop_times, so no stops: "
                f"{', '.join(sorted(map(str, no_timetable))[:10])}"
            )

        timetable = build_timetable(gtfs_data, trips) if self.frequency_tags else None

        # Process routes using vectorized operations
        route_trip_stops = (
            trips.join(stop_times, on="trip_id")
            .filter(pl.col("route_id").is_in(routes_to_process["route_id"].to_list()))
            .sort(["route_id", "trip_id", "stop_sequence"])
            .group_by(["route_id", "trip_id", "shape_id"], maintain_order=True)
            .agg([pl.col("stop_id").alias("stops")])
        )

        # Routes without stop_times are still imported when their trips have a shape:
        # the relation then has ways but no stops, and is marked with a fixme
        if no_timetable:
            with_shape = (
                trips.filter(
                    pl.col("route_id").is_in(list(no_timetable))
                    & pl.col("shape_id").is_not_null()
                )
                .group_by(["route_id", "shape_id"], maintain_order=True)
                .agg(pl.col("trip_id").first())
            )
            no_shape = no_timetable - set(with_shape["route_id"].to_list())
            if no_shape:
                logger.warning(
                    f"{len(no_shape)} routes have neither stop_times nor a shape, "
                    f"nothing to import: {', '.join(sorted(map(str, no_shape))[:10])}"
                )
            if with_shape.height:
                skeletons = with_shape.select(
                    ["route_id", "trip_id", "shape_id"]
                ).with_columns(
                    pl.Series(
                        "stops",
                        [[] for _ in range(with_shape.height)],
                        dtype=pl.List(pl.Utf8),
                    )
                )
                route_trip_stops = pl.concat(
                    [route_trip_stops, skeletons], how="vertical_relaxed"
                )

        # Process all routes at once instead of looping
        for route_data in route_trip_stops.group_by("route_id"):
            route_ref = str(route_data[0][0])  # route_id
            route_trips_data = route_data[1]  # DataFrame for this route

            try:
                route_info = routes_to_process.filter(
                    pl.col("route_id").cast(pl.Utf8) == route_ref
                ).row(0, named=True)
                logger.info(f"Processing route {route_ref}")
            except pl.exceptions.OutOfBoundsError:
                continue

            osm_route_type = route_info["osm_mode"]

            # Create Trip objects from the grouped data
            trip_sequences = [
                Trip(
                    trip_id=row["trip_id"],
                    route_id=route_ref,
                    shape_id=row["shape_id"],
                    stops=row["stops"],
                )
                for row in route_trips_data.iter_rows(named=True)
            ]

            # Trips that share a stop sequence are one pattern: keep them for the timetable
            pattern_trips: dict[tuple, list[str]] = {}
            for trip in trip_sequences:
                pattern_trips.setdefault(pattern_key(trip), []).append(trip.trip_id)

            # Deduplicate stop sets
            trip_sequences = deduplicate_trips(trip_sequences)

            # Process each unique stop pattern
            for trip_sequence in trip_sequences:
                # Get the stop locations (with lat and long)
                stop_locations = self._get_stop_locations(trip_sequence.stops, stops)
                stop_objects = []
                if not self.exclude_stops:
                    stop_objects = self._get_stop_objects(
                        stop_locations,
                        self.add_missing_stops,
                        self.search_radius,
                        mode=osm_route_type,
                    )

                # Get the OSM ways that make up the route
                osm_way_ids = []
                if (
                    not self.exclude_routes
                    and shapes is not None
                    and trip_sequence.shape_id is not None
                ):
                    if self.osm_extract is not None:
                        osm_way_ids = self._get_route_ways_offline(
                            trip_sequence.shape_id, shapes, osm_route_type
                        )
                    elif osm_route_type in ROAD_MODES:
                        osm_way_ids = self._get_route_ways(
                            trip_sequence.shape_id, shapes
                        )

                # Calculate direction
                direction = ""
                if self.route_direction:
                    first_stop = stop_locations.head(1).select(["lat", "lon"]).row(0)
                    last_stop = stop_locations.tail(1).select(["lat", "lon"]).row(0)
                    direction = calculate_direction(first_stop, last_stop)

                # Optional GTFS columns may be absent from routes.txt or empty
                short_name = route_info.get("route_short_name") or ""
                long_name = format_name(route_info.get("route_long_name"))
                from_name, to_name = self._get_terminus_names(stop_locations)
                if self.ptv2_names and from_name and to_name:
                    name = f"{osm_route_type} {short_name}: {from_name} => {to_name}"
                else:
                    name = " ".join(
                        part
                        for part in ("Route", short_name, long_name, direction)
                        if part
                    )
                route_tags = {
                    "type": "route",
                    "public_transport:version": "2",
                    "route": osm_route_type,
                    "ref": short_name,
                    "name": name,
                }
                if from_name:
                    route_tags["from"] = from_name
                if to_name:
                    route_tags["to"] = to_name
                if self.feed_id:
                    route_tags["gtfs:feed"] = self.feed_id
                    route_tags["gtfs:route_id"] = route_ref
                    route_tags["gtfs:trip_id:sample"] = trip_sequence.trip_id
                colour = format_colour(route_info.get("route_color"))
                if colour:
                    route_tags["colour"] = colour

                if timetable is not None:
                    route_tags.update(
                        pattern_service_tags(
                            pattern_trips[pattern_key(trip_sequence)],
                            gtfs_data,
                            timetable,
                        )
                    )

                if self.agency_tags:
                    route_tags.update(self._agency_tags(gtfs_data, route_info))

                if not trip_sequence.stops:
                    route_tags["fixme"] = (
                        "stops to add: the GTFS feed has no stop_times for this route"
                    )

                if self.relation_tags:
                    route_tags.update(self.relation_tags)

                # Create OSM relation
                relation = OSMRelation(
                    id=next(self._relation_ids),
                    tags=route_tags,
                    members=[],
                )

                # Add stop objects as members
                for stop_object in stop_objects:
                    role = (
                        "stop"
                        if stop_object.tags.get("public_transport") == "stop_position"
                        else "platform"
                    )
                    relation.add_member(osm_type="node", ref=stop_object.id, role=role)

                # Add ways as members
                for way_id in osm_way_ids:
                    relation.add_member(osm_type="way", ref=way_id)

                if not relation.members and not trip_sequence.stops:
                    logger.warning(
                        f"Route {route_ref} ({osm_route_type}) has no stop_times and no way found, left out"
                    )
                    continue

                self.relations.append(relation)

    @staticmethod
    def _agency_tags(gtfs_data: dict[str, pl.DataFrame], route: dict) -> dict[str, str]:
        """operator and network of a route: the name of its agency."""
        agency = gtfs_data.get("agency")
        if agency is None or "agency_name" not in agency.columns:
            return {}
        names = {
            str(row.get("agency_id") or ""): row["agency_name"]
            for row in agency.iter_rows(named=True)
            if row["agency_name"]
        }
        name = names.get(str(route.get("agency_id") or ""))
        if name is None and len(names) == 1:
            name = next(iter(names.values()))
        return {"operator": name, "network": name} if name else {}

    @staticmethod
    def _get_terminus_names(stop_locations: pl.DataFrame) -> tuple[str, str]:
        """Return the names of the first and last stop of a trip ("" if unknown)."""
        if stop_locations.is_empty():
            return "", ""
        names = stop_locations["name"].to_list()
        return (names[0] or "").strip(), (names[-1] or "").strip()

    def _get_stop_objects(
        self,
        stops: pl.DataFrame,
        add_missing_stops: bool,
        max_distance: float = 5,
        mode: str = "bus",
    ) -> list[OSMElement]:
        """
        Get OSM elements for stops by querying nearby bus stops from OpenStreetMap.

        Args:
            stops: DataFrame containing stop information with lat, lon, name, and stop_id columns
            add_missing_stops: Whether to add stops missing from the database to the output

        Returns:
            List of OSMElement objects representing nearby bus stops, ordered to match input stops
        """
        if stops.is_empty():
            return []

        if self.osm_extract is not None:
            candidates = self.osm_extract.stops_near(
                list(zip(stops["lat"].to_list(), stops["lon"].to_list(), strict=True)),
                max_distance,
                mode,
            )
            return self._match_stops(
                stops, candidates, add_missing_stops, max_distance, mode
            )

        osm_elements: list[OSMElement] = []
        # overpass_url = "https://overpass-api.de/api/interpreter"
        overpass_url = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"

        # Build a single query with all coordinates
        around_clauses = [
            f"(around:{max_distance},{stop_row['lat']},{stop_row['lon']})"
            for stop_row in stops.iter_rows(named=True)
        ]

        selectors = stop_selectors(mode)
        query_body = "\n".join(
            "\n".join(f"        node{selector}{around};" for selector in selectors)
            for around in around_clauses
        )

        query = f"""
        [out:json][timeout:60];
        (
        {query_body}
        );
        out skel;
        """

        max_retries = 3
        retry_count = 0

        while retry_count <= max_retries:
            try:
                logger.info(
                    f"Querying Overpass API for {stops.height} stop locations (attempt {retry_count + 1})"
                )
                response = requests.post(
                    overpass_url,
                    data=query,
                    headers={
                        "User-Agent": "gtfstoosm (https://github.com/whubsch/gtfstoosm)"
                    },
                )

                if response.status_code == 200:
                    result = response.json()

                    # Create OSMNode objects from all results
                    all_osm_nodes: list[OSMNode] = []
                    for element in result.get("elements", []):
                        if element["type"] == "node":
                            osm_node = OSMNode(
                                id=element["id"],
                                lat=element["lat"],
                                lon=element["lon"],
                                tags=element.get("tags", {}),
                            )
                            all_osm_nodes.append(osm_node)

                    osm_elements = self._match_stops(
                        stops, all_osm_nodes, add_missing_stops, max_distance, mode
                    )

                    # Success - break out of retry loop
                    break

                elif response.status_code in [429, 504]:
                    retry_count += 1
                    error_base = (
                        f"Request error by Overpass API (HTTP {response.status_code})."
                    )
                    if retry_count <= max_retries:
                        logger.warning(
                            " ".join(
                                [
                                    error_base,
                                    "Retrying in 2 seconds...",
                                    f"(attempt {retry_count + 1}/{max_retries + 1})",
                                ]
                            )
                        )
                        time.sleep(2)
                    else:
                        logger.error(
                            " ".join(
                                [error_base, f"Max retries ({max_retries}) exceeded."]
                            )
                        )
                        break
                else:
                    logger.warning(
                        f"Failed to query Overpass API: HTTP {response.status_code}"
                    )
                    break

            except Exception as e:
                logger.warning(f"Error querying nearby stops: {str(e)}")
                break

        return osm_elements

    def _match_stops(
        self,
        stops: pl.DataFrame,
        all_osm_nodes: list[OSMNode],
        add_missing_stops: bool,
        max_distance: float,
        mode: str,
    ) -> list[OSMElement]:
        """
        Match each GTFS stop to the closest OSM stop among the candidates.

        A GTFS stop without an OSM stop within max_distance becomes a new node when
        add_missing_stops is set. The result is ordered like `stops`.
        """
        rows = list(stops.iter_rows(named=True))

        # Pair every GTFS stop with the OSM stops within reach, closest pairs first,
        # so that a stop is not given the node of its neighbour just because it
        # comes first in the pattern
        pairs = sorted(
            (distance, row_index, node_index)
            for row_index, row in enumerate(rows)
            for node_index, node in enumerate(all_osm_nodes)
            if (
                distance := self._calculate_distance(
                    row["lat"], row["lon"], node.lat, node.lon
                )
            )
            <= max_distance
        )

        matched: dict[int, OSMNode] = {}  # row index -> OSM stop
        node_of_stop: dict[str, OSMNode] = {}  # a GTFS stop always gets the same node
        used_nodes: set[int] = set()
        for _, row_index, node_index in pairs:
            stop_id = rows[row_index]["stop_id"]
            if row_index in matched:
                continue
            if stop_id in node_of_stop:
                matched[row_index] = node_of_stop[stop_id]
            elif node_index not in used_nodes:
                node = all_osm_nodes[node_index]
                matched[row_index] = node_of_stop[stop_id] = node
                used_nodes.add(node_index)

        osm_elements: list[OSMElement] = []
        for row_index, stop_row in enumerate(rows):
            if row_index in matched:
                node = matched[row_index]
                osm_elements.append(node)
                self.matched_nodes[node.id] = node
                self.stop_links.setdefault(node.id, set()).add(str(stop_row["stop_id"]))
                continue

            logger.debug(
                f"No OSM stop found within {max_distance}m of GTFS stop at "
                f"{stop_row['lat']}, {stop_row['lon']}"
            )
            if add_missing_stops:
                new_stop = OSMNode(
                    id=self._new_stop_node_id(stop_row["stop_id"]),
                    lat=stop_row["lat"],
                    lon=stop_row["lon"],
                    tags={"name": stop_row["name"], **new_stop_tags(mode)},
                )
                if not self.is_stop_duplicate(new_stop):
                    logger.debug(f"Adding new stop {new_stop.id}")
                    self.new_stops.append(new_stop)
                else:
                    logger.debug(f"Skipping duplicate stop {new_stop.id}")
                osm_elements.append(new_stop)

        logger.info(
            f"Found {len([i for i in osm_elements if i.id > 0])} OSM stop elements, created {len([i for i in osm_elements if i.id < 0])} (ordered)"
        )
        return osm_elements

    def _calculate_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """
        Calculate the distance between two points using Haversine formula.

        Returns:
            Distance in meters
        """
        import math

        # Convert latitude and longitude from degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.asin(math.sqrt(a))

        # Radius of earth in meters
        r = 6371000

        return c * r

    def _get_stop_locations(
        self, stop_ids: list[str], stops: pl.DataFrame
    ) -> pl.DataFrame:
        """
        Get location information for a list of stop IDs.

        Args:
            stop_ids: List of stop IDs to get locations for
            stops: Complete list of GTFS stops data

        Returns:
            List of dictionaries containing stop information with lat and lon
        """
        if not stop_ids:
            return pl.DataFrame(
                schema={
                    "stop_id": pl.Utf8,
                    "lat": pl.Float64,
                    "lon": pl.Float64,
                    "name": pl.Utf8,
                }
            )

        # Create a DataFrame from stop_ids to preserve order
        stop_ids_df = pl.DataFrame({"stop_id": stop_ids, "order": range(len(stop_ids))})

        # Join with stops data and sort by original order
        stop_locations = (
            stop_ids_df.join(
                stops.select(["stop_id", "stop_lat", "stop_lon", "stop_name"]),
                on="stop_id",
                how="inner",
            )
            .sort("order")
            .select(
                [
                    "stop_id",
                    pl.col("stop_lat").alias("lat"),
                    pl.col("stop_lon").alias("lon"),
                    pl.col("stop_name").alias("name"),
                ]
            )
        )

        return stop_locations

    def _get_route_ways_offline(
        self, shape_id: str | int, shapes: pl.DataFrame, mode: str
    ) -> list[int]:
        """OSM way ids a GTFS shape follows, found in the local extract."""
        assert self.osm_extract is not None
        shape = shapes.filter(pl.col("shape_id") == shape_id).sort("shape_pt_sequence")
        ways = self.osm_extract.match_ways(
            list(
                zip(
                    shape["shape_pt_lat"].to_list(),
                    shape["shape_pt_lon"].to_list(),
                    strict=True,
                )
            ),
            mode,
        )
        logger.info(f"Matched {len(ways)} ways for shape {shape_id} ({mode})")
        return ways

    def _get_route_ways(
        self,
        shape_id: str | int,
        shapes: pl.DataFrame,
        costing: str = "bus",
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> list[int]:
        """
        Get OSM way IDs for a route between stops using Valhalla API.

        Args:
            shape_id: GTFS shape ID
            shapes: Complete list of GTFS shapes data
            costing: Valhalla costing model to use (bus, auto, pedestrian, etc.)
            max_retries: Maximum number of retry attempts if request fails
            retry_delay: Delay in seconds between retry attempts

        Returns:
            List of unique OSM way IDs that make up the route
        """
        logger.info(f"Getting OSM ways for route {shape_id}")

        filtered_shapes = shapes.filter(pl.col("shape_id") == shape_id).sort(
            "shape_pt_sequence"
        )

        route_ways = []

        # Get the input data for the request
        valhalla_url = "https://valhalla1.openstreetmap.de/trace_attributes"

        request_json = {
            "shape": filtered_shapes.select(
                [
                    pl.struct(
                        [
                            pl.col("shape_pt_lat").alias("lat"),
                            pl.col("shape_pt_lon").alias("lon"),
                        ]
                    )
                ]
            )
            .to_series()
            .to_list(),
            "costing": costing,
            "costing_options": {
                "maneuver_penalty": 10,
                "ignore_access": True,
                "ignore_restrictions": True,
            },
            "format": "default",
            "shape_match": "map_snap",
            "filters": {"attributes": ["edge.way_id"]},
        }

        # Try the request with retries
        retry_count = 0
        while retry_count <= max_retries:
            try:
                response = requests.post(
                    valhalla_url,
                    json=request_json,
                    headers={
                        "User-Agent": "gtfstoosm (https://github.com/whubsch/gtfstoosm)"
                    },
                ).json()

                # Check if the response contains the expected data
                if "edges" in response:
                    # Extract the way IDs
                    for edge in response.get("edges", []):
                        if "way_id" in edge:
                            way_id = edge["way_id"]
                            # Prevent repeated way IDs
                            if not route_ways or way_id != route_ways[-1]:
                                route_ways.append(way_id)

                    logger.info(
                        f"Found {len(route_ways)} total ways for route {shape_id} variant"
                    )
                    return route_ways
                else:
                    logger.warning(
                        f"Invalid response from Valhalla (attempt {retry_count + 1}/{max_retries + 1})"
                    )

            except Exception as e:
                logger.warning(
                    f"Error getting ways between stops (attempt {retry_count + 1}/{max_retries + 1}): {str(e)}"
                )

            # If we get here, the request failed or the response was invalid
            if retry_count < max_retries:
                logger.info(f"Retrying in {retry_delay} seconds...")
                import time

                time.sleep(retry_delay)
                retry_count += 1
            else:
                logger.error(f"Failed to get ways after {max_retries + 1} attempts")
                break

        logger.info(f"Found {len(route_ways)} total ways for route {shape_id} variant")

        return route_ways

    def _get_osm_route_type(self, gtfs_route_type: int | str) -> str:
        """
        Convert GTFS route_type to OSM route tag value.

        Args:
            gtfs_route_type: GTFS route_type value

        Returns:
            Corresponding OSM route tag value
        """
        # Convert to int if it's a string
        try:
            route_type = int(gtfs_route_type)
        except (ValueError, TypeError):
            return "bus"  # Default to bus if conversion fails

        return osm_mode(route_type) or "bus"

    def _new_stop_node_id(self, stop_id: str) -> int:
        """Return the negative OSM node id used for a GTFS stop that is not in OSM.

        GTFS stop IDs are text, so they cannot be negated: each distinct stop gets
        the next free negative number, and the same stop always gets the same one.
        """
        if stop_id not in self._new_stop_node_ids:
            self._new_stop_node_ids[stop_id] = -(len(self._new_stop_node_ids) + 1)
        return self._new_stop_node_ids[stop_id]

    def is_stop_duplicate(self, new_stop: OSMNode) -> bool:
        """Check if a stop is already in self.new_stops."""
        for existing_stop in self.new_stops:
            # Check if ID matches
            if existing_stop.id == new_stop.id:
                return True

        return False

    def write_to_file(self, output_path: str) -> None:
        """
        Write the OSM data to a file.

        Args:
            output_path: Path to write the OSM XML file
        """
        logger.info(f"Writing OSM data to {output_path}")

        # Writing logic
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("<?xml version='1.0' encoding='UTF-8'?>\n")
                f.write("<osmChange version='0.6' generator='gtfstoosm'>\n")
                f.write("<create>\n")

                # Write new stops
                for stop in self.new_stops:
                    f.write(stop.to_xml() + "\n")

                # Write nodes
                for node in self.nodes:
                    f.write(node.to_xml() + "\n")

                # Write relations
                for relation in self.relations:
                    f.write(relation.to_xml() + "\n")

                f.write("</create>\n")
                if self.modifications:
                    f.write("<modify>\n")
                    for element in self.modifications:
                        f.write(element.to_josm_xml())
                    f.write("</modify>\n")
                f.write("</osmChange>\n")

            logger.info(f"Successfully wrote OSM data to {output_path}")

        except Exception as e:
            logger.error(f"Error writing OSM file: {str(e)}")
            raise OSError(f"Failed to write OSM file: {str(e)}") from e

    def write_josm_file(self, output_path: str) -> None:
        """
        Write the OSM data in the JOSM file format, to open in JOSM.

        New objects have negative ids. Members that are in OSM already (stops, ways)
        are not in the file: download the area in JOSM first, then merge the layer.

        Args:
            output_path: Path to write the .osm file
        """
        logger.info(f"Writing JOSM data to {output_path}")
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("<?xml version='1.0' encoding='UTF-8'?>\n")
                f.write("<osm version='0.6' generator='gtfstoosm'>\n")
                for node in [*self.new_stops, *self.nodes]:
                    f.write(node.to_josm_xml())
                for relation in self.relations:
                    f.write(relation.to_josm_xml())
                for element in self.modifications:
                    f.write(element.to_josm_xml("modify"))
                f.write("</osm>\n")
            logger.info(f"Successfully wrote JOSM data to {output_path}")
        except Exception as e:
            logger.error(f"Error writing OSM file: {str(e)}")
            raise OSError(f"Failed to write OSM file: {str(e)}") from e


def feed_bbox(
    stops: pl.DataFrame, margin: float = 0.02
) -> tuple[float, float, float, float] | None:
    """(min_lat, min_lon, max_lat, max_lon) of the stops of a feed, with a margin in degrees."""
    lats = stops["stop_lat"].drop_nulls()
    lons = stops["stop_lon"].drop_nulls()
    if lats.is_empty() or lons.is_empty():
        return None
    return (
        float(lats.min()) - margin,  # type: ignore[arg-type]
        float(lons.min()) - margin,  # type: ignore[arg-type]
        float(lats.max()) + margin,  # type: ignore[arg-type]
        float(lons.max()) + margin,  # type: ignore[arg-type]
    )


def convert_gtfs_to_osm(
    gtfs_path: str, osm_path: str, **options: dict[str, bool | int | str]
) -> bool:
    """
    Convert a GTFS feed to OSM relations.

    Args:
        gtfs_path: Path to the GTFS feed zip file
        osm_path: Path to write the OSM XML file
        **options: Additional options for the conversion
            - exclude_stops: Whether to include stops (default: False)
            - exclude_routes: Whether to include routes (default: False)
            - add_missing_stops: Whether to add stops missing from the database to the output (default: False)
            - stop_search_radius: Radius in meters to search for stops (default: 10)
            - add_route_direction: Whether to add route direction to the output (default: False)
            - route_ref_pattern: Regex pattern to filter routes by (default: None)
            - relation_tags: Dict of tags to add to the route and route_master relations (default: None)
            - feed_id: Identifier of the feed; when set, route relations get gtfs:feed,
              gtfs:route_id and gtfs:trip_id:sample tags (default: None)
            - ptv2_names: Name routes "bus 1: A => B" instead of "Route 1 Long Name" (default: False)
            - modes: OSM route values to convert, e.g. ["bus", "tram"] (default: all supported modes)
            - output_format: "osc" (osmChange, the default), "osm" (the JOSM file format) or "both"
              (JOSM's file, and the osmChange one next to it with the .osc extension)
            - frequency_tags: Add interval, opening_hours and interval:conditional tags estimated
              from frequencies.txt or the timetable (default: False)
            - auto_extract: Download (and cache) the OSM data around the stops and read stops
              and ways from it (default: False); refresh: download it again
            - agency_tags: operator and network from the agency's name (default: False)
            - osm_file: OSM extract (.osm.pbf...) to read stops and ways from instead of
              querying Overpass and Valhalla (default: None, online lookups)
            - active_on: datetime.date; routes without service from that day are left out (default: None, no filter)

    Returns:
        True if conversion was successful

    Raises:
        FileNotFoundError: If the GTFS feed is not found
        ValueError: If the GTFS feed is invalid
        IOError: If writing the OSM file fails
    """
    try:
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        # Load GTFS data
        loader = GTFSFeed(feed_dir=gtfs_path)
        loader.load()

        osm_extract = None
        if options.get("osm_file"):
            osm_extract = OsmExtract(
                options["osm_file"], bbox=feed_bbox(loader.tables["stops"])
            )
        elif options.get("auto_extract"):
            served = loader.tables["stops"].filter(
                pl.col("stop_id").is_in(
                    loader.tables["stop_times"]["stop_id"].unique().to_list()
                )
            )
            bbox = feed_bbox(served)
            if bbox is None:
                raise ValueError("The feed has no stop with a position")
            points = list(
                zip(
                    served["stop_lat"].to_list(),
                    served["stop_lon"].to_list(),
                    strict=True,
                )
            )
            osm_extract = OsmExtract(
                prepare_extract(points, bbox, refresh=bool(options.get("refresh"))),
                bbox=bbox,
            )

        # Build OSM relations
        builder = OSMRelationBuilder(
            exclude_stops=options.get("exclude_stops", False),
            exclude_routes=options.get("exclude_routes", False),
            add_missing_stops=options.get("add_missing_stops", False),
            # route_types=options.get("route_types"),
            # agency_id=options.get("agency_id"),
            search_radius=options.get("stop_search_radius", 10.0),
            route_direction=options.get(
                "add_route_direction", options.get("route_direction", False)
            ),
            route_ref_pattern=options.get("route_ref_pattern"),
            relation_tags=options.get("relation_tags"),
            feed_id=options.get("feed_id"),
            ptv2_names=options.get("ptv2_names", False),
            modes=options.get("modes"),
            active_on=options.get("active_on"),
            osm_extract=osm_extract,
            frequency_tags=bool(options.get("frequency_tags", False)),
            agency_tags=bool(options.get("agency_tags", False)),
        )

        logger.debug(f"OSMRelationBuilder options: {builder}")

        builder.build_relations(loader.tables)
        builder.build_route_masters(loader.tables)
        compare_with_osm(builder)

        # Write to file(s): osmChange by default, JOSM's own format, or both
        output_format = options.get("output_format", "osc")
        if output_format == "osc":
            builder.write_to_file(osm_path)
        else:
            builder.write_josm_file(osm_path)
            if output_format == "both":
                builder.write_to_file(str(Path(osm_path).with_suffix(".osc")))

        return True

    except Exception as e:
        logger.error(f"Conversion failed: {str(e)}")
        raise
