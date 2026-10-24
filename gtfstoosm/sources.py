"""Where the OSM data comes from: which file covers the stops of a feed.

Two providers publish extracts that are kept up to date:

- OSM France (download.openstreetmap.fr): continents, countries and, for many of them,
  their regions (départements and régions of France, the cantons of Switzerland,
  the Länder of Germany, the provinces of Italy...). Small files, so it comes first.
- Geofabrik (download.geofabrik.de): countries and some regions of the whole world.

For each stop the smallest region that contains it is chosen, among the files the
tool is allowed to download. A provider's robots.txt is respected: OSM France forbids
robots to fetch `*.pbf` files. When its smaller file is therefore left aside, the log
says where to get it: put it in the cache (`regions/` folder, under the name given) and
it is used.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from gtfstoosm.extracts import (
    GEOFABRIK_INDEX,
    INDEX_MAX_AGE_DAYS,
    Region,
    download,
    read_regions,
    regions_for_points,
)

logger = logging.getLogger(__name__)

OSMFR = "https://download.openstreetmap.fr"
OSMFR_LISTING_MAX_AGE_DAYS = 30  # the list of regions and their outlines change rarely
ROBOTS_MAX_AGE_DAYS = 1
CELL = 0.05  # degrees: stops in the same cell are looked up once
GOOD_ENOUGH = (
    2.0  # square degrees: a region this small (a département) needs no comparison
)
ROBOT_NAME = "gtfstoosm"


# --- robots.txt --------------------------------------------------------------------


def _rule_regex(pattern: str) -> re.Pattern:
    """A robots.txt path pattern: `*` matches anything, a final `$` the end."""
    anchored = pattern.endswith("$")
    body = re.escape(pattern.removesuffix("$")).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def robots_allows(robots_txt: str, path: str, robot: str = ROBOT_NAME) -> bool:
    """Whether robots.txt lets `robot` fetch `path` (the longest matching rule wins).

    Handles the `*` and `$` of the de facto standard, which Python's own parser does not.
    """
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    reading_agents = False
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if not reading_agents:
                if agents:
                    groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            reading_agents = True
        elif field in ("allow", "disallow"):
            reading_agents = False
            if value:
                rules.append((field == "allow", value))
    if agents:
        groups.append((agents, rules))

    specific = [g for g in groups if any(robot in a for a in g[0])]
    applicable = specific or [g for g in groups if "*" in g[0]]
    best: tuple[int, bool] | None = None
    for _, group_rules in applicable:
        for allow, pattern in group_rules:
            if _rule_regex(pattern).match(path):
                length = len(pattern)
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
    return True if best is None else best[1]


# --- a file that covers some region ------------------------------------------------


@dataclass
class Source:
    """A region file a provider offers."""

    provider: str
    id: str
    name: str
    url: str
    area: float  # square degrees, to compare regions
    allowed: bool = True  # whether the provider lets robots download it

    @property
    def key(self) -> str:
        return f"{self.provider}-{self.id}"


# --- OSM France --------------------------------------------------------------------


def parse_poly(text: str) -> list[list[list[list[float]]]]:
    """Polygons of an Osmosis .poly file, as lists of rings (outer ring first)."""
    polygons: list[list[list[list[float]]]] = []
    lines = [line.strip() for line in text.splitlines()]
    index = 1  # the first line is the name of the file
    while index < len(lines):
        if lines[index] == "END" or not lines[index]:
            index += 1
            continue
        hole = lines[index].startswith("!")
        index += 1
        ring: list[list[float]] = []
        while index < len(lines) and lines[index] != "END":
            parts = lines[index].split()
            if len(parts) >= 2:
                ring.append([float(parts[0]), float(parts[1])])
            index += 1
        index += 1
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if hole and polygons:
            polygons[-1].append(ring)
        else:
            polygons.append([ring])
    return polygons


class OsmFrance:
    """The tree of extracts of download.openstreetmap.fr.

    `extracts/<path>/` lists the files and sub-directories of a region, and
    `polygons/<path>.poly` is the outline of each region.
    """

    name = "osmfr"

    def __init__(self, cache: Path, refresh: bool = False):
        self.cache = cache / "osmfr"
        self.refresh = refresh
        self._robots: str | None = None

    def allowed(self, url: str) -> bool:
        if self._robots is None:
            try:
                robots = download(
                    f"{OSMFR}/robots.txt",
                    self.cache / "robots.txt",
                    ROBOTS_MAX_AGE_DAYS,
                    self.refresh,
                )
                self._robots = robots.read_text(encoding="utf-8", errors="replace")
            except RuntimeError as err:
                logger.warning(f"Cannot read robots.txt of {OSMFR}: {err}")
                self._robots = "User-agent: *\nDisallow: /"
        return robots_allows(self._robots, url.removeprefix(OSMFR))

    def _text(self, url: str, name: str) -> str:
        path = download(
            url, self.cache / name, OSMFR_LISTING_MAX_AGE_DAYS, self.refresh
        )
        return path.read_text(encoding="utf-8", errors="replace")

    def _listing(self, parts: list[str]) -> tuple[list[str], set[str]]:
        """(regions with a file, regions with sub-regions) in a directory."""
        folder = "/".join(parts)
        url = f"{OSMFR}/extracts/{folder}/" if folder else f"{OSMFR}/extracts/"
        html = self._text(
            url, "listing-" + (folder.replace("/", "_") or "root") + ".html"
        )
        hrefs = re.findall(r'href="([^"?][^"]*)"', html)
        files = sorted(
            {
                h.removesuffix(".osm.pbf")
                for h in hrefs
                if h.endswith(".osm.pbf") and not h.endswith("-latest.osm.pbf")
            }
        )
        directories = {
            h.removesuffix("/")
            for h in hrefs
            if h.endswith("/") and not h.startswith(("/", "?", "."))
        }
        return files, directories

    def _region(self, parts: list[str]) -> Region | None:
        folder = "/".join(parts)
        try:
            text = self._text(
                f"{OSMFR}/polygons/{folder}.poly",
                "poly-" + folder.replace("/", "_") + ".poly",
            )
        except RuntimeError:
            return None
        polygons = parse_poly(text)
        if not polygons:
            return None
        return Region(
            parts[-1],
            parts[-1],
            f"{OSMFR}/extracts/{folder}.osm.pbf",
            polygons,
        )

    def find(self, lat: float, lon: float) -> Source | None:
        """The smallest region of OSM France that contains the point."""
        found: Region | None = None
        found_parts: list[str] = []
        parts: list[str] = []
        while True:
            try:
                files, directories = self._listing(parts)
            except RuntimeError as err:
                logger.warning(f"Cannot list OSM France: {err}")
                break
            best: tuple[Region, list[str]] | None = None
            for name in files:
                candidate = self._region([*parts, name])
                if candidate and candidate.contains(lat, lon):
                    if best is None or candidate.area < best[0].area:
                        best = (candidate, [*parts, name])
            if best is None:
                break
            found, found_parts = best
            if found_parts[-1] not in directories:
                break
            parts = found_parts
        if found is None:
            return None
        return Source(
            "osmfr",
            "_".join(found_parts),
            "/".join(found_parts),
            found.url,
            found.area,
            allowed=self.allowed(found.url),
        )


# --- Geofabrik ---------------------------------------------------------------------


class Geofabrik:
    name = "geofabrik"

    def __init__(self, cache: Path, refresh: bool = False):
        self.cache = cache
        self.refresh = refresh
        self._regions: list[Region] | None = None

    def find(self, lat: float, lon: float) -> Source | None:
        if self._regions is None:
            index = download(
                GEOFABRIK_INDEX,
                self.cache / "geofabrik-index.json",
                INDEX_MAX_AGE_DAYS,
                self.refresh,
            )
            self._regions = read_regions(index)
        [region] = regions_for_points(self._regions, [(lat, lon)]) or [None]
        if region is None:
            return None
        return Source(
            "geofabrik", region.id, region.name or region.id, region.url, region.area
        )


# --- choosing ----------------------------------------------------------------------


def choose_sources(
    points: list[tuple[float, float]],
    cache: Path,
    refresh: bool = False,
    providers: list | None = None,
) -> list[Source]:
    """The region files that cover the stops, smallest first choice per area.

    For each area of CELL degrees, the smallest region among those the providers offer
    and the tool may download. A smaller one that a provider's robots.txt forbids is
    reported, and used if it has been put in the cache by hand.
    """
    providers = providers or [OsmFrance(cache, refresh), Geofabrik(cache, refresh)]

    cells: dict[tuple[int, int], tuple[float, float]] = {}
    for lat, lon in points:
        cells.setdefault((int(lat // CELL), int(lon // CELL)), (lat, lon))

    chosen: dict[str, Source] = {}
    for lat, lon in cells.values():
        # Providers are asked in turn, and the next ones only if the region found so
        # far is large: this spares Geofabrik's index when OSM France has a small file
        candidates: list[Source] = []
        usable: list[Source] = []
        for provider in providers:
            source = provider.find(lat, lon)
            if source is None:
                continue
            candidates.append(source)
            if source.allowed or _manual_file(cache, source) is not None:
                usable.append(source)
            if usable and min(s.area for s in usable) <= GOOD_ENOUGH:
                break
        if not candidates:
            logger.warning(f"No OSM region found around {lat}, {lon}")
            continue
        refused = [s for s in candidates if s not in usable]
        if not usable:
            continue
        best = min(usable, key=lambda s: s.area)
        for source in refused:
            if source.area < best.area:
                _hint(cache, source, best)
        chosen[best.key] = best
    return list(chosen.values())


def manual_names(source: Source) -> list[str]:
    """File names under which a file put in the cache by hand is recognised.

    The name the tool asks for, and the one the site gives it (a browser keeps it).
    """
    last = source.name.rsplit("/", 1)[-1]
    return [f"{source.key}.osm.pbf", f"{last}.osm.pbf", f"{last}-latest.osm.pbf"]


def _manual_file(cache: Path, source: Source) -> Path | None:
    """A file the user put in the cache for a source that robots may not download."""
    for name in manual_names(source):
        path = cache / "regions" / name
        if path.exists():
            return path
    return None


def _hint(cache: Path, source: Source, used: Source) -> None:
    folder = cache / "regions"
    folder.mkdir(parents=True, exist_ok=True)
    logger.warning(
        f"A smaller file exists for this area: {source.url} ({source.provider}, "
        f"{source.name}), but the robots.txt of that site forbids robots to download "
        f".pbf files, so {used.provider} {used.name} is used instead. To use the smaller "
        f"one, download it yourself (with your browser, for instance) and save it, "
        f"keeping its name, in {folder} (or give it with --osm-file)"
    )


def manual_path(cache: Path, source: Source) -> Path | None:
    return _manual_file(cache, source)


__all__ = [
    "Geofabrik",
    "OsmFrance",
    "Source",
    "choose_sources",
    "manual_path",
    "parse_poly",
    "robots_allows",
]
