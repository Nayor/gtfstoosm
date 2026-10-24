"""Offline OpenStreetMap data: stops and ways read from a local extract.

This replaces the Overpass and Valhalla lookups. The extract is any file that
osmium reads (.osm.pbf, .osm, .osm.bz2...); cut it to the area of the feed first,
for instance with `osmium extract -b minlon,minlat,maxlon,maxlat region.osm.pbf`.
"""

import logging
import math
from collections import defaultdict

import osmium

from gtfstoosm.modes import SUPPORTED_MODES, is_stop, is_way_for
from gtfstoosm.osm import OSMNode

logger = logging.getLogger(__name__)

EARTH_RADIUS = 6371000.0
CELL_SIZE = 100.0  # metres, size of the cells of the spatial indexes
DENSIFY_STEP = 10.0  # metres between the points of a shape that are matched
MAX_SNAP_DISTANCE = 30.0  # metres between a shape point and the way it is matched to
STAY_MARGIN = 8.0  # metres a point may be farther from the way it is already on


class OsmExtract:
    """Stops and ways of a local OSM extract, with the lookups the converter needs.

    Args:
        path: OSM file to read, or several (objects in more than one are kept once)
        bbox: optional (min_lat, min_lon, max_lat, max_lon); objects entirely outside
            are not kept, which saves memory when the extract is larger than the feed
    """

    def __init__(
        self,
        path: str | list[str],
        bbox: tuple[float, float, float, float] | None = None,
    ):
        self.paths = [path] if isinstance(path, str) else list(path)
        self.bbox = bbox
        self._lat0: float | None = None
        self._cos_lat0 = 1.0

        # (id, lat, lon, tags) of the nodes that are a stop of some mode
        self._stops: list[tuple[int, float, float, dict[str, str]]] = []
        # per way: id, its points as (x, y) metres, and the modes it can carry
        self._ways: list[tuple[int, list[tuple[float, float]], frozenset[str]]] = []
        self._stop_index: dict[tuple[int, int], list[int]] = defaultdict(list)
        self._segment_index: dict[
            str, dict[tuple[int, int], list[tuple[int, int]]]
        ] = {}

        self._load()

    # --- loading -----------------------------------------------------------------

    def _inside(self, lat: float, lon: float) -> bool:
        if self.bbox is None:
            return True
        min_lat, min_lon, max_lat, max_lon = self.bbox
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    def _objects(self):
        """Nodes and ways of every file, each with the locations of its nodes."""
        for path in self.paths:
            yield from osmium.FileProcessor(path).with_locations()

    def _load(self) -> None:
        ways: list[tuple[int, list[tuple[float, float]], frozenset[str]]] = []
        seen_stops: set[int] = set()
        seen_ways: set[int] = set()

        for obj in self._objects():
            if obj.is_node():
                if not obj.tags or not obj.location.valid():
                    continue
                lat, lon = obj.location.lat, obj.location.lon
                tags = {tag.k: tag.v for tag in obj.tags}
                if (
                    obj.id not in seen_stops
                    and self._inside(lat, lon)
                    and any(is_stop(mode, tags) for mode in SUPPORTED_MODES)
                ):
                    seen_stops.add(obj.id)
                    self._stops.append((obj.id, lat, lon, tags))
            elif obj.is_way():
                if obj.id in seen_ways:
                    continue
                tags = {tag.k: tag.v for tag in obj.tags}
                modes = frozenset(m for m in SUPPORTED_MODES if is_way_for(m, tags))
                if not modes:
                    continue
                points = [
                    (n.location.lat, n.location.lon)
                    for n in obj.nodes
                    if n.location.valid()
                ]
                if len(points) >= 2 and any(self._inside(p[0], p[1]) for p in points):
                    seen_ways.add(obj.id)
                    ways.append((obj.id, points, modes))

        if self._stops:
            self._lat0 = sum(s[1] for s in self._stops) / len(self._stops)
        elif ways:
            self._lat0 = ways[0][1][0][0]
        else:
            self._lat0 = 0.0
        self._cos_lat0 = math.cos(math.radians(self._lat0))

        for way_id, points, modes in ways:
            self._ways.append(
                (way_id, [self._project(p[0], p[1]) for p in points], modes)
            )

        for index, (_, lat, lon, _) in enumerate(self._stops):
            self._stop_index[self._cell(*self._project(lat, lon))].append(index)

        logger.info(
            f"Read {len(self._stops)} stops and {len(self._ways)} ways from "
            + ", ".join(self.paths)
        )

    # --- geometry ----------------------------------------------------------------

    def _project(self, lat: float, lon: float) -> tuple[float, float]:
        """Local plane coordinates in metres (equirectangular projection)."""
        x = math.radians(lon) * EARTH_RADIUS * self._cos_lat0
        y = math.radians(lat) * EARTH_RADIUS
        return x, y

    @staticmethod
    def _cell(x: float, y: float) -> tuple[int, int]:
        return int(x // CELL_SIZE), int(y // CELL_SIZE)

    # --- stops -------------------------------------------------------------------

    def stops_near(
        self, points: list[tuple[float, float]], radius: float, mode: str
    ) -> list[OSMNode]:
        """OSM stops of the mode within `radius` metres of any (lat, lon) point."""
        found: dict[int, OSMNode] = {}
        reach = int(radius // CELL_SIZE) + 1

        for lat, lon in points:
            x, y = self._project(lat, lon)
            cx, cy = self._cell(x, y)
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    for index in self._stop_index.get((cx + dx, cy + dy), ()):
                        stop_id, stop_lat, stop_lon, tags = self._stops[index]
                        if stop_id in found or not is_stop(mode, tags):
                            continue
                        sx, sy = self._project(stop_lat, stop_lon)
                        if math.hypot(sx - x, sy - y) <= radius * 1.5:
                            found[stop_id] = OSMNode(
                                id=stop_id, lat=stop_lat, lon=stop_lon, tags=tags
                            )
        return list(found.values())

    # --- ways --------------------------------------------------------------------

    def _segments_for(self, mode: str) -> dict[tuple[int, int], list[tuple[int, int]]]:
        """Spatial index of the segments of the ways that can carry the mode."""
        if mode not in self._segment_index:
            index: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
            for way_index, (_, points, modes) in enumerate(self._ways):
                if mode not in modes:
                    continue
                for seg in range(len(points) - 1):
                    (x1, y1), (x2, y2) = points[seg], points[seg + 1]
                    steps = max(1, int(math.hypot(x2 - x1, y2 - y1) // (CELL_SIZE / 2)))
                    cells = {
                        self._cell(
                            x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps
                        )
                        for i in range(steps + 1)
                    }
                    for cell in cells:
                        index[cell].append((way_index, seg))
            self._segment_index[mode] = index
        return self._segment_index[mode]

    @staticmethod
    def _distance_to_segment(
        x: float, y: float, a: tuple[float, float], b: tuple[float, float]
    ) -> float:
        (x1, y1), (x2, y2) = a, b
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        if length2 == 0:
            return math.hypot(x - x1, y - y1)
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
        return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))

    def _densify(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Shape points in metres, with a point at least every DENSIFY_STEP."""
        dense: list[tuple[float, float]] = []
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
            steps = max(1, int(math.hypot(x2 - x1, y2 - y1) // DENSIFY_STEP))
            dense.extend(
                (x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
                for i in range(steps)
            )
        if points:
            dense.append(points[-1])
        return dense

    def match_ways(self, shape: list[tuple[float, float]], mode: str) -> list[int]:
        """OSM way ids a shape of (lat, lon) points follows, in order.

        Each point of the shape (densified) is snapped to the nearest segment of a way
        that can carry the mode; a point stays on the way it is already on unless
        another is clearly closer. Runs of a single point are dropped as noise at
        junctions. Points with no way within MAX_SNAP_DISTANCE are skipped.
        """
        index = self._segments_for(mode)
        if not index:
            return []

        reach = int(MAX_SNAP_DISTANCE // CELL_SIZE) + 1
        runs: list[list[int]] = []  # [way_index, number of points]
        current: int | None = None

        for x, y in self._densify([self._project(lat, lon) for lat, lon in shape]):
            cx, cy = self._cell(x, y)
            best: dict[int, float] = {}
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    for way_index, seg in index.get((cx + dx, cy + dy), ()):
                        points = self._ways[way_index][1]
                        d = self._distance_to_segment(
                            x, y, points[seg], points[seg + 1]
                        )
                        if d <= MAX_SNAP_DISTANCE and d < best.get(way_index, 1e9):
                            best[way_index] = d
            if not best:
                continue

            nearest = min(best, key=lambda w: best[w])
            if current in best and best[current] <= best[nearest] + STAY_MARGIN:
                nearest = current

            if runs and runs[-1][0] == nearest:
                runs[-1][1] += 1
            else:
                runs.append([nearest, 1])
            current = nearest

        # Drop single-point runs (junction noise), then merge what became adjacent
        kept = [run for run in runs if run[1] > 1] or runs
        way_ids: list[int] = []
        for way_index, _ in kept:
            way_id = self._ways[way_index][0]
            if not way_ids or way_ids[-1] != way_id:
                way_ids.append(way_id)
        return way_ids
