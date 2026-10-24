"""Find, download and cut the OpenStreetMap data around a feed.

The region files come from OSM France and Geofabrik (see sources.py). They are big, so
they are kept in a cache directory and downloaded as little as possible:

- a file less than MAX_AGE_DAYS old is used without asking the server anything;
- an older one is checked with a conditional request (If-None-Match and
  If-Modified-Since), which costs nothing when the file has not changed;
- `refresh=True` downloads it again whatever its age.

The smallest region(s) that contain the stops of the feed are chosen, then cut to the area of the feed with osmium-tool, and the result
is cached too.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "gtfstoosm (+https://github.com/Nayor/gtfstoosm)"
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"
MAX_AGE_DAYS = 7  # a region file this young is used without checking the server
INDEX_MAX_AGE_DAYS = 30
CELL = 0.05  # degrees: stops in the same cell are looked up once
CACHE_ENV = "GTFSTOOSM_CACHE"
CHUNK = 1024 * 1024


def cache_dir() -> Path:
    """Where downloads are kept: $GTFSTOOSM_CACHE, else ~/.cache/gtfstoosm."""
    configured = os.environ.get(CACHE_ENV)
    path = Path(configured) if configured else Path.home() / ".cache" / "gtfstoosm"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- downloads ---------------------------------------------------------------------


def _metadata_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".json")


def _read_metadata(dest: Path) -> dict:
    try:
        return json.loads(_metadata_path(dest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_metadata(dest: Path, metadata: dict) -> None:
    _metadata_path(dest).write_text(json.dumps(metadata), encoding="utf-8")


def download(
    url: str,
    dest: Path,
    max_age_days: float = MAX_AGE_DAYS,
    refresh: bool = False,
) -> Path:
    """Make sure `dest` holds a recent copy of `url`, downloading as little as possible."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    metadata = _read_metadata(dest) if dest.exists() else {}
    age = time.time() - metadata.get("fetched_at", 0)

    if dest.exists() and not refresh and age < max_age_days * 86400:
        logger.info(f"Using {dest.name}, downloaded {age / 3600:.0f} h ago")
        return dest

    headers = {"User-Agent": USER_AGENT}
    if dest.exists() and not refresh:
        if metadata.get("etag"):
            headers["If-None-Match"] = metadata["etag"]
        if metadata.get("last_modified"):
            headers["If-Modified-Since"] = metadata["last_modified"]

    try:
        response = requests.get(url, headers=headers, stream=True, timeout=(10, 120))
    except requests.RequestException as err:
        if dest.exists():
            logger.warning(f"Cannot check {url} ({err}), using the copy in the cache")
            return dest
        raise RuntimeError(f"Cannot download {url}: {err}") from err

    with response:
        if response.status_code == 304 and dest.exists():
            logger.info(f"{dest.name} has not changed on the server")
            metadata["fetched_at"] = time.time()
            _write_metadata(dest, metadata)
            return dest
        if response.status_code != 200:
            if dest.exists():
                logger.warning(
                    f"{url} answered HTTP {response.status_code}, using the copy in the cache"
                )
                return dest
            raise RuntimeError(f"Cannot download {url}: HTTP {response.status_code}")

        logger.info(f"Downloading {url}")
        partial = dest.with_name(dest.name + ".part")
        received = 0
        with open(partial, "wb") as f:
            for chunk in response.iter_content(CHUNK):
                f.write(chunk)
                received += len(chunk)
                if received // (100 * CHUNK) != (received - len(chunk)) // (
                    100 * CHUNK
                ):
                    logger.info(f"  {received // CHUNK} MB")
        os.replace(partial, dest)
        _write_metadata(
            dest,
            {
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "fetched_at": time.time(),
                "url": url,
            },
        )
    return dest


# --- regions -----------------------------------------------------------------------

Ring = list[list[float]]


@dataclass
class Region:
    """A region of Geofabrik's index."""

    id: str
    name: str
    url: str
    polygons: list[list[Ring]]  # polygons, each an outer ring then holes, (lon, lat)

    def __post_init__(self) -> None:
        outer = [ring[0] for ring in self.polygons if ring]
        lons = [p[0] for ring in outer for p in ring]
        lats = [p[1] for ring in outer for p in ring]
        self.bbox = (min(lons), min(lats), max(lons), max(lats))
        self.area = sum(_ring_area(ring) for ring in outer)

    def contains(self, lat: float, lon: float) -> bool:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            return False
        return any(
            _in_ring(lat, lon, rings[0])
            and not any(_in_ring(lat, lon, hole) for hole in rings[1:])
            for rings in self.polygons
        )


def _ring_area(ring: Ring) -> float:
    """Area of a ring in square degrees (shoelace formula)."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def _in_ring(lat: float, lon: float, ring: Ring) -> bool:
    """Whether a point is inside a ring (ray casting)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def read_regions(index_path: Path) -> list[Region]:
    """The regions of a Geofabrik index-v1.json that have an OSM file to download."""
    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)
    regions = []
    for feature in index["features"]:
        properties = feature["properties"]
        url = properties.get("urls", {}).get("pbf")
        geometry = feature.get("geometry")
        if not url or not geometry:
            continue
        polygons = (
            geometry["coordinates"]
            if geometry["type"] == "MultiPolygon"
            else [geometry["coordinates"]]
        )
        regions.append(
            Region(properties["id"], properties.get("name", ""), url, polygons)
        )
    return regions


def regions_for_points(
    regions: list[Region], points: list[tuple[float, float]]
) -> list[Region]:
    """The smallest region containing each (lat, lon) point, without repeats."""
    by_size = sorted(regions, key=lambda region: region.area)
    cells: dict[tuple[int, int], tuple[float, float]] = {}
    for lat, lon in points:
        cells.setdefault((int(lat // CELL), int(lon // CELL)), (lat, lon))

    chosen: dict[str, Region] = {}
    for lat, lon in cells.values():
        for region in by_size:
            if region.contains(lat, lon):
                chosen[region.id] = region
                break
        else:
            logger.warning(f"No region of the Geofabrik index contains {lat}, {lon}")
    return list(chosen.values())


# --- cutting -----------------------------------------------------------------------


def cut(
    sources: list[Path], bbox: tuple[float, float, float, float], dest: Path
) -> bool:
    """Cut the sources to a bounding box with osmium-tool; False if it is not installed.

    bbox is (min_lat, min_lon, max_lat, max_lon).
    """
    osmium = shutil.which("osmium")
    if osmium is None:
        return False

    min_lat, min_lon, max_lat, max_lon = bbox
    area = f"{min_lon},{min_lat},{max_lon},{max_lat}"
    parts = []
    for index, source in enumerate(sources):
        part = dest.with_name(f"{dest.name}.{index}.part.osm.pbf")
        logger.info(f"Cutting {source.name} to {area}")
        subprocess.run(
            [
                osmium,
                "extract",
                "-b",
                area,
                "--overwrite",
                "-o",
                str(part),
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        parts.append(part)

    if len(parts) == 1:
        os.replace(parts[0], dest)
    else:
        subprocess.run(
            [osmium, "merge", "--overwrite", "-o", str(dest), *map(str, parts)],
            check=True,
            capture_output=True,
        )
        for part in parts:
            part.unlink()
    return True


# --- all together ------------------------------------------------------------------


def prepare_extract(
    points: list[tuple[float, float]],
    bbox: tuple[float, float, float, float],
    refresh: bool = False,
    cache: Path | None = None,
    providers: list | None = None,
) -> list[str]:
    """OSM file(s) covering the stops of a feed, ready to be read.

    Returns the cut extract when osmium-tool is available. Otherwise the whole region
    files, which the reader then has to filter, using a lot of memory.

    Args:
        points: (lat, lon) of the stops of the feed
        bbox: (min_lat, min_lon, max_lat, max_lon) of the area to keep
        refresh: download the files again even if the cache is recent
        cache: cache directory (default: see cache_dir)
        providers: where to look for regions (default: OSM France, then Geofabrik)
    """
    from gtfstoosm.sources import choose_sources, manual_path

    cache = cache or cache_dir()
    chosen = choose_sources(points, cache, refresh, providers)
    if not chosen:
        raise RuntimeError(
            "No OSM region found for this feed. Give an extract with --osm-file"
        )
    logger.info("OSM regions: " + ", ".join(f"{s.name} ({s.provider})" for s in chosen))

    sources = []
    for source in chosen:
        manual = None if source.allowed else manual_path(cache, source)
        sources.append(
            manual
            or download(
                source.url,
                cache / "regions" / f"{source.key}.osm.pbf",
                MAX_AGE_DAYS,
                refresh,
            )
        )

    def stamp(path: Path) -> str:
        metadata = _read_metadata(path)
        return str(
            metadata.get("last_modified")
            or metadata.get("etag")
            or path.stat().st_mtime
        )

    key = hashlib.sha1(
        json.dumps(
            [
                [s.key for s in chosen],
                [stamp(path) for path in sources],
                [round(v, 3) for v in bbox],
            ]
        ).encode()
    ).hexdigest()[:16]
    dest = cache / "extracts" / f"{key}.osm.pbf"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        logger.info(f"Using the extract {dest.name} from the cache")
        return [str(dest)]
    if cut(sources, bbox, dest):
        return [str(dest)]

    logger.warning(
        "osmium-tool is not installed: reading the whole region files, which needs a lot "
        "of memory. Install it, or use the Docker image, or give a small extract with --osm-file"
    )
    return [str(source) for source in sources]
