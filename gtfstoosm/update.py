"""Turn the comparison with OSM into what is written: new objects, and changes to
existing ones that only add what OSM lacks.

What the tool never does to an object of OSM: remove a tag, change the value of a tag,
move a node, change the members of a route. A difference of that kind is reported
(see RouteMatch) and left to the person who reviews the file.

What it may do, as an `action="modify"` of the object that is in OSM:

- add the tags OSM lacks to a route it recognised (the link with the feed, network,
  duration...);
- add the link with the feed (gtfs:stop_id:<feed>) to a stop;
- add a route it created to the route_master that is in OSM already.
"""

import logging
from typing import TYPE_CHECKING

from gtfstoosm.compare import ADDABLE_TAGS, RouteMatch, compare_route
from gtfstoosm.osm import OSMNode, OSMRelation, RelationMember
from gtfstoosm.osm_data import ExistingRelation

if TYPE_CHECKING:
    from gtfstoosm.convert import OSMRelationBuilder

logger = logging.getLogger(__name__)

KEPT_STATUSES = ("same", "link", "different")  # the built relation is not created


def _as_relation(
    existing: ExistingRelation, extra_tags=None, extra_members=()
) -> OSMRelation:
    """The relation of OSM as it is now, plus what is added to it."""
    relation = OSMRelation(
        id=existing.id,
        version=existing.version,
        tags={**existing.tags, **(extra_tags or {})},
    )
    relation.members = [
        RelationMember(type=t, ref=r, role=role) for t, r, role in existing.members
    ]
    relation.members.extend(extra_members)
    return relation


def compare_with_osm(builder: "OSMRelationBuilder") -> None:
    """Compare the relations and the stops built with those of the OSM extract.

    The routes OSM has already are not created again; the builder keeps the result in
    `matches` (routes) and `modifications` (what to change in existing objects).
    """
    extract = builder.osm_extract
    if extract is None or not builder.compare_existing:
        return

    routes = [r for r in builder.relations if r.tags.get("type") == "route"]
    builder.matches = [
        compare_route(r, extract.relations, builder.feed_id) for r in routes
    ]

    # The built route -> the route of OSM that it is
    replaced: dict[int, int] = {}
    for match in builder.matches:
        if match.status in KEPT_STATUSES and match.existing is not None:
            replaced[match.relation.id] = match.existing.id
        if match.status == "link" and match.existing is not None:
            builder.modifications.append(
                _as_relation(match.existing, match.missing_tags)
            )

    dropped = {m.relation.id for m in builder.matches if m.status != "new"}
    builder.relations = [r for r in builder.relations if r.id not in dropped]

    _compare_masters(builder, replaced, dropped)
    _link_stops(builder)

    counts = {}
    for match in builder.matches:
        counts[match.status] = counts.get(match.status, 0) + 1
    logger.info(
        "Routes compared with OSM: "
        + ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    )
    for match in builder.matches:
        if match.status in ("different", "ambiguous"):
            logger.warning(_describe(match))


def _compare_masters(
    builder: "OSMRelationBuilder", replaced: dict[int, int], dropped: set[int]
) -> None:
    extract = builder.osm_extract
    assert extract is not None
    existing_masters = [
        e for e in extract.relations if e.tags.get("type") == "route_master"
    ]
    kept: list[OSMRelation] = []

    for relation in builder.relations:
        if relation.tags.get("type") != "route_master":
            kept.append(relation)
            continue

        # Routes that OSM has take the place of the built ones; ambiguous ones are left out
        members = []
        for member in relation.members:
            if member.ref in replaced:
                members.append(
                    RelationMember(
                        type="relation", ref=replaced[member.ref], role=member.role
                    )
                )
            elif member.ref not in dropped:
                members.append(member)
        if not members:
            continue

        wanted = {m.ref for m in members}
        found = None
        for master in existing_masters:
            in_master = {ref for kind, ref, _ in master.members if kind == "relation"}
            linked = (
                builder.feed_id
                and master.tags.get("gtfs:feed") == builder.feed_id
                and master.tags.get("gtfs:route_id")
                == relation.tags.get("gtfs:route_id")
            )
            if wanted & in_master or linked:
                found = master
                break

        if found is None:
            relation.members = members
            kept.append(relation)
            continue

        known = {ref for kind, ref, _ in found.members if kind == "relation"}
        added = [m for m in members if m.ref not in known]
        missing = {
            k: v
            for k, v in relation.tags.items()
            if k in ADDABLE_TAGS and k not in found.tags and v
        }
        if added or missing:
            builder.modifications.append(_as_relation(found, missing, added))

    builder.relations = kept


def _link_stops(builder: "OSMRelationBuilder") -> None:
    """Add gtfs:stop_id:<feed> to the stops of OSM that the feed's stops were matched with."""
    if not builder.feed_id:
        return
    key = f"gtfs:stop_id:{builder.feed_id}"
    for node_id, stop_ids in sorted(builder.stop_links.items()):
        node = builder.matched_nodes.get(node_id)
        if node is None or node_id < 0:
            continue
        value = ";".join(sorted(stop_ids))
        if key in node.tags:
            if node.tags[key] != value:
                logger.warning(
                    f"Stop {node_id} of OSM has {key}={node.tags[key]}, the feed says {value}"
                )
            continue
        builder.modifications.append(
            OSMNode(
                id=node_id,
                version=node.version,
                lat=node.lat,
                lon=node.lon,
                tags={**node.tags, key: value},
            )
        )


def _describe(match: RouteMatch) -> str:
    name = match.relation.tags.get("name", "")
    ref = match.relation.tags.get("ref", "")
    if match.status == "ambiguous":
        ids = ", ".join(f"r{c.id}" for c in match.candidates)
        return f"Route {ref} ({name}): several relations of OSM look like it: {ids}"
    existing = match.existing
    assert existing is not None
    parts = []
    for tag, (osm, feed) in match.changed_tags.items():
        parts.append(f"{tag}: OSM '{osm}', feed '{feed}'")
    if match.stops_only_in_feed:
        parts.append(
            f"{len(match.stops_only_in_feed)} stops not in the relation of OSM"
        )
    if match.stops_only_in_osm:
        parts.append(f"{len(match.stops_only_in_osm)} stops of OSM not in the feed")
    if match.stops_missing_in_osm:
        parts.append(
            f"{match.stops_missing_in_osm} stops of the feed have no node in OSM"
        )
    if match.stops_reordered:
        parts.append("stops in another order")
    return f"Route {ref} ({name}) differs from relation r{existing.id}: " + "; ".join(
        parts
    )
