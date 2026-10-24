"""Compare what the feed gives with what OSM already has.

For each route relation built from the feed, find the relation that is in OSM already
(if any) and say how they differ. Nothing here changes OSM: the result tells which
routes are new, which only lack the link with the feed, and which differ (the human
decides about those).

Statuses of a route:

- new: nothing in OSM looks like it
- same: found, and it has all the tags the feed gives
- link: found, and OSM only lacks some tags (the link with the feed, network,
  duration...): they can be added without touching anything else
- different: found, but a tag has another value or the stops are not the same
- ambiguous: several relations of OSM look equally like it
"""

import unicodedata
from dataclasses import dataclass, field

from gtfstoosm.osm import OSMRelation
from gtfstoosm.osm_data import ExistingRelation

# Tags written by the tool that may be added to a relation of OSM when it lacks them
ADDABLE_TAGS = frozenset(
    {
        "gtfs:feed",
        "gtfs:route_id",
        "gtfs:trip_id:sample",
        "network",
        "operator",
        "colour",
        "ref",
        "from",
        "to",
        "duration",
        "interval",
        "interval:conditional",
        "opening_hours",
    }
)
# Tags whose value, when OSM has another one, is reported
COMPARED_TAGS = ADDABLE_TAGS | {"name", "route"}

MIN_OVERLAP = 0.5  # share of stops two relations must have in common to be the same
SURE_OVERLAP = 0.8  # above this the tags need not agree


@dataclass
class RouteMatch:
    """How a route built from the feed compares with OSM."""

    relation: OSMRelation
    status: str = "new"
    existing: ExistingRelation | None = None
    candidates: list[ExistingRelation] = field(default_factory=list)
    missing_tags: dict[str, str] = field(default_factory=dict)  # to add to OSM
    changed_tags: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )  # (OSM, feed)
    stops_only_in_feed: list[int] = field(default_factory=list)
    stops_only_in_osm: list[int] = field(default_factory=list)
    stops_reordered: bool = False
    stops_missing_in_osm: int = 0  # stops of the feed that have no node in OSM
    overlap: float = 0.0


def normalize(text: str) -> str:
    """Case, accents, spacing and the arrow of PTv2 names do not make a difference."""
    decomposed = unicodedata.normalize("NFKD", text.replace("&amp;", "&"))
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().replace("=>", " ").replace("⇒", " ").split())


def stop_nodes(members) -> list[int]:
    """The node ids of the stops of a route, in order (members are (type, ref, role))."""
    return [
        ref
        for kind, ref, role in members
        if kind == "node" and ref > 0 and not role.startswith(("platform_exit", "exit"))
    ]


def _relation_members(relation: OSMRelation) -> list[tuple[str, int, str]]:
    return [(m.type, m.ref, m.role) for m in relation.members]


def _overlap(a: list[int], b: list[int]) -> float:
    union = set(a) | set(b)
    return len(set(a) & set(b)) / len(union) if union else 0.0


def _same_ends(built: OSMRelation, existing: ExistingRelation) -> bool:
    ends = ("from", "to")
    return all(
        normalize(built.tags.get(k, "")) == normalize(existing.tags.get(k, ""))
        for k in ends
        if built.tags.get(k)
    ) and any(built.tags.get(k) for k in ends)


def _order(ours: list[int], theirs: list[int]) -> float:
    """Share of the consecutive pairs of stops of `ours` that `theirs` has in that order."""
    position = {node: index for index, node in enumerate(theirs)}
    pairs = [
        (a, b)
        for a, b in zip(ours, ours[1:], strict=False)
        if a in position and b in position
    ]
    if not pairs:
        return 0.0
    return sum(position[a] < position[b] for a, b in pairs) / len(pairs)


def find_existing(
    built: OSMRelation, existing: list[ExistingRelation], feed_id: str | None
) -> tuple[ExistingRelation | None, list[ExistingRelation], float]:
    """The relation of OSM that is the built one: (match, tied candidates, overlap).

    A relation that carries the link with the feed (gtfs:feed and gtfs:route_id) is
    looked for first; else a relation with the same mode that shares most of the stops.
    Among several, the one with the most stops in common wins, then the one with the same
    ends, then the one that has the stops in the same order (the other direction of a
    line has the same stops).
    """
    mode = built.tags.get("route")
    ours = stop_nodes(_relation_members(built))
    pool = [
        e
        for e in existing
        if e.tags.get("type") == "route" and e.tags.get("route") == mode
    ]

    def score(e: ExistingRelation) -> tuple[float, bool, float]:
        theirs = stop_nodes(e.members)
        return (_overlap(ours, theirs), _same_ends(built, e), _order(ours, theirs))

    linked = [
        e
        for e in pool
        if feed_id
        and e.tags.get("gtfs:feed") == feed_id
        and e.tags.get("gtfs:route_id") == built.tags.get("gtfs:route_id")
    ]
    if linked:
        return _best(linked, score)

    ref = built.tags.get("ref")
    network = built.tags.get("network")
    close = []
    for e in pool:
        overlap = score(e)[0]
        if overlap < MIN_OVERLAP:
            continue
        same_line = (
            bool(ref)
            and e.tags.get("ref") == ref
            and (
                not network
                or not e.tags.get("network")
                or e.tags.get("network") == network
            )
        )
        if same_line or overlap >= SURE_OVERLAP:
            close.append(e)
    if not close:
        return None, [], 0.0
    return _best(close, score)


def _best(candidates: list[ExistingRelation], score):
    scored = [(score(e), e) for e in candidates]
    top_score = max(s for s, _ in scored)
    top = [e for s, e in scored if s == top_score]
    if len(top) == 1:
        return top[0], [], top_score[0]
    return None, top, top_score[0]


def compare_route(
    built: OSMRelation, existing: list[ExistingRelation], feed_id: str | None
) -> RouteMatch:
    """Find the relation of OSM for a built route and describe the differences."""
    match, tied, overlap = find_existing(built, existing, feed_id)
    result = RouteMatch(built, candidates=tied, overlap=overlap)
    if tied:
        result.status = "ambiguous"
        return result
    if match is None:
        return result

    result.existing = match
    for key, value in built.tags.items():
        if key not in COMPARED_TAGS or not value:
            continue
        if key not in match.tags:
            if key in ADDABLE_TAGS:
                result.missing_tags[key] = value
        elif normalize(match.tags[key]) != normalize(value):
            result.changed_tags[key] = (match.tags[key], value)

    ours = stop_nodes(_relation_members(built))
    theirs = stop_nodes(match.members)
    result.stops_only_in_feed = [n for n in ours if n not in set(theirs)]
    result.stops_only_in_osm = [n for n in theirs if n not in set(ours)]
    common_ours = [n for n in ours if n in set(theirs)]
    common_theirs = [n for n in theirs if n in set(ours)]
    result.stops_reordered = common_ours != common_theirs
    result.stops_missing_in_osm = sum(
        1 for m in built.members if m.type == "node" and m.ref < 0
    )

    if (
        result.changed_tags
        or result.stops_only_in_feed
        or result.stops_only_in_osm
        or result.stops_reordered
        or result.stops_missing_in_osm
    ):
        result.status = "different"
    elif result.missing_tags:
        result.status = "link"
    else:
        result.status = "same"
    return result
