"""A table of the lines of a feed: what each one brings and how sure it is.

The table helps to decide in which order to review and upload the lines: the ones with
a high score first. It also shows the new stops that several lines share (the lines
that share some are best sent in the same JOSM session, where JOSM gives the stop its
real id once the first line is uploaded) and a JOSM search expression that selects
each line.
"""

import csv
import logging
from dataclasses import dataclass, field

from gtfstoosm.compare import RouteMatch
from gtfstoosm.osm import OSMRelation

logger = logging.getLogger(__name__)

SURE = 85  # score from which a line is "sure"
CHECK = 60  # score from which it is "check" (below: "difficult")


@dataclass
class RouteFacts:
    """What was found out while building a route, used to score it."""

    stops: int = 0  # stops of the stop sequence
    missing_stops: int = 0  # stops left out of the relation: OSM lacks them
    max_distance: float = 0.0  # largest distance to the OSM stop a stop was matched to
    name_mismatches: int = 0  # matched stops whose name is not the OSM stop's
    coverage: float | None = None  # share of the shape found on ways, None if unknown
    has_shape: bool = True
    no_stop_times: bool = False


@dataclass
class LineRow:
    """One line of the feed (its variants together)."""

    ref: str
    name: str
    mode: str
    route_id: str
    variants: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    stops: int = 0
    new_stops: int = 0
    shared_with: list[str] = field(
        default_factory=list
    )  # other lines sharing new stops
    score: int = 100
    level: str = "sure"
    reasons: list[str] = field(default_factory=list)
    search: str = ""


def level_of(score: int) -> str:
    if score >= SURE:
        return "sure"
    if score >= CHECK:
        return "check"
    return "difficult"


def score_route(
    status: str, facts: RouteFacts, linked: bool = True
) -> tuple[int, list[str]]:
    """Score (0-100) of a route and why it is not 100.

    `linked` tells whether the route of OSM was found through our link tags (the feed
    and route id) rather than through the ref and the stops.
    """
    score = 100
    reasons: list[str] = []

    def minus(points: int, why: str) -> None:
        nonlocal score
        score -= points
        reasons.append(why)

    if status == "different":
        minus(40, "differs from the relation of OSM")
    elif status == "ambiguous":
        minus(50, "several relations of OSM look like it")
    elif status in ("link", "same") and not linked:
        minus(10, "found in OSM without the link tags")

    if facts.no_stop_times:
        minus(30, "no stop_times in the feed: no stops")
    if facts.missing_stops:
        minus(
            min(30, 10 * facts.missing_stops),
            f"{facts.missing_stops} stops not in OSM left out",
        )
    if facts.max_distance > 20:
        minus(20, f"a stop matched {facts.max_distance:.0f} m away")
    elif facts.max_distance > 10:
        minus(10, f"a stop matched {facts.max_distance:.0f} m away")
    if facts.name_mismatches:
        minus(
            min(20, 5 * facts.name_mismatches),
            f"{facts.name_mismatches} stops with another name",
        )

    if not facts.has_shape:
        minus(10, "no shape: no ways")
    elif facts.coverage is None:
        minus(5, "ways not checked against the shape")
    elif facts.coverage < 0.8:
        minus(30, f"ways cover {facts.coverage:.0%} of the shape")
    elif facts.coverage < 0.95:
        minus(15, f"ways cover {facts.coverage:.0%} of the shape")

    return max(0, score), reasons


def _search(relation: OSMRelation, feed_id: str | None) -> str:
    """A JOSM search expression that selects the relation(s) of a line."""

    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    route_id = relation.tags.get("gtfs:route_id")
    if feed_id and route_id:
        return f'type:relation "gtfs:feed"={quote(feed_id)} "gtfs:route_id"={quote(route_id)}'
    return f"type:relation new ref={quote(relation.tags.get('ref', ''))}"


def line_table(
    matches: list[RouteMatch],
    facts: dict[int, RouteFacts],
    feed_id: str | None,
) -> list[LineRow]:
    """The lines of the feed, best score first."""
    rows: dict[tuple[str, str, str], LineRow] = {}
    new_stops: dict[tuple[str, str, str], set[int]] = {}
    scores: dict[tuple[str, str, str], list[int]] = {}

    for match in matches:
        relation = match.relation
        key = (
            relation.tags.get("ref", ""),
            relation.tags.get("route", ""),
            relation.tags.get("gtfs:route_id", ""),
        )
        row = rows.setdefault(
            key,
            LineRow(
                ref=key[0],
                name=relation.tags.get("name", ""),
                mode=key[1],
                route_id=key[2],
                search=_search(relation, feed_id),
            ),
        )
        row.variants += 1
        row.statuses[match.status] = row.statuses.get(match.status, 0) + 1
        row.stops = max(row.stops, sum(1 for m in relation.members if m.type == "node"))

        if match.status == "new":
            new_stops.setdefault(key, set()).update(
                m.ref for m in relation.members if m.type == "node" and m.ref < 0
            )

        linked = bool(
            match.existing
            and feed_id
            and match.existing.tags.get("gtfs:feed") == feed_id
        )
        score, reasons = score_route(
            match.status, facts.get(relation.id, RouteFacts()), linked
        )
        scores.setdefault(key, []).append(score)
        row.reasons.extend(r for r in reasons if r not in row.reasons)

    # Stops that do not exist yet and that several lines use
    users: dict[int, set[tuple[str, str, str]]] = {}
    for key, stops in new_stops.items():
        for stop in stops:
            users.setdefault(stop, set()).add(key)
    for key, row in rows.items():
        row.new_stops = len(new_stops.get(key, ()))
        sharing = {
            other
            for stop in new_stops.get(key, ())
            for other in users[stop]
            if other != key
        }
        row.shared_with = sorted(rows[other].ref for other in sharing)
        row.score = min(scores[key])
        row.level = level_of(row.score)

    return sorted(rows.values(), key=lambda r: (-r.score, r.ref))


def write_csv(rows: list[LineRow], path: str) -> None:
    """The table as a CSV file (semicolon-separated, UTF-8 with BOM: opens in a spreadsheet)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(
            [
                "score", "level", "ref", "name", "mode", "route_id", "variants",
                "new", "link", "same", "different", "ambiguous", "stops",
                "new_stops", "shared_with", "reasons", "josm_search",
            ]
        )  # fmt: skip
        for row in rows:
            writer.writerow(
                [
                    row.score, row.level, row.ref, row.name, row.mode, row.route_id,
                    row.variants,
                    *(row.statuses.get(s, 0) for s in ("new", "link", "same", "different", "ambiguous")),
                    row.stops, row.new_stops, " ".join(row.shared_with),
                    "; ".join(row.reasons), row.search,
                ]
            )  # fmt: skip


def log_table(rows: list[LineRow], limit: int = 30) -> None:
    """The table in the log: best lines first."""
    if not rows:
        return
    counts = {
        level: sum(1 for r in rows if r.level == level)
        for level in ("sure", "check", "difficult")
    }
    logger.info(
        f"{len(rows)} lines: {counts['sure']} sure, {counts['check']} to check, "
        f"{counts['difficult']} difficult"
    )
    for row in rows[:limit]:
        shared = (
            f", new stops shared with {', '.join(row.shared_with)}"
            if row.shared_with
            else ""
        )
        status = ", ".join(f"{n} {s}" for s, n in sorted(row.statuses.items()))
        why = f" ({'; '.join(row.reasons)})" if row.reasons else ""
        logger.info(
            f"  {row.score:3d} {row.level:9s} {row.mode} {row.ref}: {status}, {row.new_stops} new stops{shared}{why}"
        )
    if len(rows) > limit:
        logger.info(f"  ... and {len(rows) - limit} more in the table")


__all__ = [
    "LineRow",
    "RouteFacts",
    "line_table",
    "log_table",
    "score_route",
    "write_csv",
]
