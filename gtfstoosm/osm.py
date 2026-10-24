import datetime
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# An ampersand that does not start an XML entity (some names are escaped already)
_BARE_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def escape_attribute(value: str) -> str:
    """Escape a text for an XML attribute, leaving entities that are already there."""
    return (
        _BARE_AMPERSAND.sub("&amp;", str(value))
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class OSMElement(BaseModel):
    id: int
    tags: dict[str, str] = Field(default_factory=dict)

    def add_tag(self, key: str, value: str) -> None:
        """Add a tag to the element."""
        if key in self.tags:
            raise ValueError(f"Key {key} already exists with value: {value}")
        if value and value != "":
            self.tags[key] = value

    def modify_tag(self, key: str, value: str) -> None:
        """Modify a tag in the element."""
        if key not in self.tags:
            raise ValueError(f"Key {key} does not yet exist")
        if value and value != "":
            self.tags[key] = value

    def tags_to_josm_xml(self, indent: str = "    ") -> str:
        """The tags, one per line, as JOSM writes them."""
        return "".join(
            f'{indent}<tag k="{escape_attribute(key)}" v="{escape_attribute(value)}" />\n'
            for key, value in self.tags.items()
        )

    def josm_attributes(self, action: str | None) -> str:
        """The attributes after the id: version and action of an existing object."""
        if self.id < 0:
            return ""  # a negative id is how JOSM knows an object is new
        version = getattr(self, "version", 1)
        return f' version="{version}"' + (f' action="{action}"' if action else "")

    def tags_to_xml(self) -> str:
        """Create an XML tag element."""
        return "\n".join(
            [
                f'<tag k="{escape_attribute(key)}" v="{escape_attribute(value)}"></tag>'
                for key, value in self.tags.items()
            ]
        )


class OSMNode(OSMElement):
    lat: float
    lon: float
    visible: bool = True
    version: int = 1
    changeset: int = 1
    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    user: str = "gtfstoosm"
    uid: int = 1

    def to_josm_xml(self, action: str | None = None) -> str:
        """The node in the JOSM file format (see to_xml for the osmChange one)."""
        return (
            f'  <node id="{self.id}"{self.josm_attributes(action)} '
            f'lat="{self.lat}" lon="{self.lon}">\n'
            f"{self.tags_to_josm_xml()}  </node>\n"
        )

    def to_xml(self) -> str:
        osm_text = f'<node id="{self.id}" lat="{self.lat}" lon="{self.lon}">'
        osm_text += self.tags_to_xml()
        osm_text += "</node>"
        return osm_text


class OSMWay(OSMElement):
    nodes: list[int] = Field(default_factory=list)
    visible: bool = True
    version: int = 1
    changeset: int = 1
    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    user: str = "gtfstoosm"
    uid: int = 1

    def add_node(self, node_id: int) -> None:
        """Add a node to the way."""
        self.nodes.append(node_id)

    def to_xml(self) -> str:
        osm_text = f'<way id="{self.id}" visible="{self.visible}">'
        osm_text += "\n".join([f"<nd ref='{node_id}'></nd>" for node_id in self.nodes])
        osm_text += self.tags_to_xml()
        return osm_text + "</way>"


class RelationMember(BaseModel):
    type: Literal["node", "way", "relation"]
    ref: int
    role: str

    def to_josm_xml(self) -> str:
        return (
            f'    <member type="{self.type}" ref="{self.ref}" '
            f'role="{escape_attribute(self.role)}" />\n'
        )

    def to_xml(self) -> str:
        """Create an XML member element."""
        return (
            f'<member type="{self.type}" ref="{self.ref}" role="{self.role}"></member>'
        )


class OSMRelation(OSMElement):
    members: list[RelationMember] = Field(default_factory=list)
    visible: bool = True
    version: int = 1
    changeset: int = 1
    timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    user: str = "gtfstoosm"
    uid: int = 1

    def add_member(
        self, osm_type: Literal["node", "way", "relation"], ref: int, role: str = ""
    ) -> None:
        """Add a member to the relation."""
        # Validate type
        if osm_type not in ["node", "way", "relation"]:
            raise ValueError(f"Invalid member type: {osm_type}")

        self.members.append(RelationMember(type=osm_type, ref=ref, role=role))

    def to_josm_xml(self, action: str | None = None) -> str:
        """The relation in the JOSM file format (see to_xml for the osmChange one)."""
        members = "".join(member.to_josm_xml() for member in self.members)
        return (
            f'  <relation id="{self.id}"{self.josm_attributes(action)}>\n'
            f"{members}{self.tags_to_josm_xml()}  </relation>\n"
        )

    def to_xml(self) -> str:
        """Create an XML relation element."""
        osm_text = f'<relation id="{self.id}" visible="{self.visible}">'

        osm_text += "\n".join([member.to_xml() for member in self.members])
        osm_text += self.tags_to_xml()

        return osm_text + "</relation>"
