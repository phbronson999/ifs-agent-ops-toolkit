"""Parse an IFS projection ``$metadata`` (OData v4 EDMX) document into a neutral model.

This is the single EDMX parser for the toolkit. The enum-map MCP tool, analysis, the
operation model and generators (e.g. connector $filter annotations) all read this model
rather than re-parsing XML with their own assumptions.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

NS = {
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "edm": "http://docs.oasis-open.org/odata/ns/edm",
}


@dataclass
class EnumType:
    fqn: str
    namespace: str
    name: str
    members: list[str]


@dataclass
class Property:
    name: str
    type: str  # as declared, e.g. "Edm.String" or "IfsApp.X.SomeEnum"
    nullable: bool = True


@dataclass
class NavigationProperty:
    name: str
    type: str  # as declared, e.g. "Collection(IfsApp.X.Line)"
    target: str  # entity type FQN with any Collection(...) removed
    is_collection: bool


@dataclass
class EntityType:
    fqn: str
    namespace: str
    name: str
    key: list[str] = field(default_factory=list)
    properties: list[Property] = field(default_factory=list)
    navigation: list[NavigationProperty] = field(default_factory=list)


@dataclass
class EntitySet:
    name: str
    entity_type: str  # FQN


@dataclass
class EdmxModel:
    enum_types: dict[str, EnumType] = field(default_factory=dict)
    entity_types: dict[str, EntityType] = field(default_factory=dict)
    entity_sets: dict[str, EntitySet] = field(default_factory=dict)

    def enum_properties(self, entity_type_fqn: str) -> dict[str, EnumType]:
        """Properties of an entity type whose declared type is an enum, in document order."""
        et = self.entity_types.get(entity_type_fqn)
        if et is None:
            return {}
        return {p.name: self.enum_types[p.type] for p in et.properties if p.type in self.enum_types}

    def entity_type_of_set(self, entity_set: str) -> Optional[EntityType]:
        es = self.entity_sets.get(entity_set)
        return self.entity_types.get(es.entity_type) if es else None


def _strip_collection(type_name: str) -> tuple[str, bool]:
    if type_name.startswith("Collection(") and type_name.endswith(")"):
        return type_name[len("Collection("):-1], True
    return type_name, False


def parse_edmx(xml_text: str) -> EdmxModel:
    """Parse EDMX text. Raises ``xml.etree.ElementTree.ParseError`` on malformed XML."""
    root = ET.fromstring(xml_text)
    model = EdmxModel()
    schemas = root.findall(".//edm:Schema", NS)

    # Enums first so property types can be recognised regardless of schema order.
    for schema in schemas:
        namespace = schema.get("Namespace", "")
        for el in schema.findall("edm:EnumType", NS):
            name = el.get("Name", "")
            fqn = f"{namespace}.{name}"
            model.enum_types[fqn] = EnumType(
                fqn=fqn, namespace=namespace, name=name,
                members=[m.get("Name", "") for m in el.findall("edm:Member", NS)],
            )

    for schema in schemas:
        namespace = schema.get("Namespace", "")
        for el in schema.findall("edm:EntityType", NS):
            name = el.get("Name", "")
            et = EntityType(fqn=f"{namespace}.{name}", namespace=namespace, name=name)
            et.key = [k.get("Name", "") for k in el.findall("edm:Key/edm:PropertyRef", NS)]
            for p in el.findall("edm:Property", NS):
                et.properties.append(Property(
                    name=p.get("Name", ""), type=p.get("Type", ""),
                    nullable=p.get("Nullable", "true").lower() != "false",
                ))
            for n in el.findall("edm:NavigationProperty", NS):
                declared = n.get("Type", "")
                target, is_coll = _strip_collection(declared)
                et.navigation.append(NavigationProperty(
                    name=n.get("Name", ""), type=declared, target=target, is_collection=is_coll,
                ))
            model.entity_types[et.fqn] = et

        for container in schema.findall("edm:EntityContainer", NS):
            for es in container.findall("edm:EntitySet", NS):
                name = es.get("Name", "")
                model.entity_sets[name] = EntitySet(name=name, entity_type=es.get("EntityType", ""))

    return model
