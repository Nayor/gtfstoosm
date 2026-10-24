"""Transport modes: mapping of GTFS route types to OSM route values and stop tags."""

# OSM route=* values this tool can write
SUPPORTED_MODES = (
    "bus",
    "trolleybus",
    "tram",
    "train",
    "light_rail",
    "subway",
    "monorail",
    "funicular",
    "ferry",
    "aerialway",
)

# Modes whose route follows the road network: only those can have their ways found
# with Valhalla. Rail, ferry and cable modes are written with their stops only.
ROAD_MODES = frozenset({"bus", "trolleybus"})

RAIL_MODES = frozenset(
    {"tram", "train", "light_rail", "subway", "monorail", "funicular"}
)

# Basic GTFS route types
_BASIC_ROUTE_TYPES = {
    0: "tram",
    1: "subway",
    2: "train",
    3: "bus",
    4: "ferry",
    5: "tram",  # cable tram
    6: "aerialway",  # aerial lift: gondola, cable car
    7: "funicular",
    11: "trolleybus",
    12: "monorail",
}

# Extended GTFS route types (hierarchical vehicle types), as (first, last, mode).
# Ranges are checked in order, so exceptions come first.
_EXTENDED_ROUTE_TYPES = (
    (400, 400, "light_rail"),  # urban railway
    (401, 402, "subway"),  # metro, underground
    (403, 404, "light_rail"),  # urban railway
    (405, 405, "monorail"),
    (100, 199, "train"),  # railway services
    (200, 299, "bus"),  # coach services
    (300, 399, "train"),  # suburban railway
    (500, 699, "subway"),  # metro, underground
    (700, 799, "bus"),
    (800, 899, "trolleybus"),
    (900, 999, "tram"),
    (1000, 1099, "ferry"),  # water transport
    (1200, 1299, "ferry"),
    (1300, 1399, "aerialway"),  # aerial lift
    (1400, 1499, "funicular"),
    # 1100 air, 1500 taxi, 1600 self drive, 1700 miscellaneous: not public transport
    # routes that OSM maps this way, left out
)


def osm_mode(route_type: int | str | None) -> str | None:
    """Return the OSM route=* value for a GTFS route_type, None if it is not supported.

    Both basic (0-12) and extended (100-1799) route types are understood.
    """
    try:
        code = int(route_type)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None

    if code in _BASIC_ROUTE_TYPES:
        return _BASIC_ROUTE_TYPES[code]
    for first, last, mode in _EXTENDED_ROUTE_TYPES:
        if first <= code <= last:
            return mode
    return None


def new_stop_tags(mode: str) -> dict[str, str]:
    """Tags of a stop created because the GTFS stop is not in OSM."""
    if mode in RAIL_MODES:
        return {"public_transport": "platform", "railway": "platform"}
    if mode in ROAD_MODES:
        return {"public_transport": "platform", "highway": "bus_stop"}
    return {"public_transport": "platform"}


def stop_selectors(mode: str) -> list[str]:
    """Overpass node filters that find existing stops of a mode."""
    if mode in ROAD_MODES:
        return ['["highway"="bus_stop"]', '["public_transport"="platform"]']
    generic = [
        '["public_transport"~"^(platform|stop_position|station)$"]',
    ]
    if mode in RAIL_MODES:
        return ['["railway"~"^(tram_stop|halt|station|stop|platform)$"]'] + generic
    if mode == "ferry":
        return ['["amenity"="ferry_terminal"]'] + generic
    if mode == "aerialway":
        return ['["aerialway"="station"]'] + generic
    return generic
