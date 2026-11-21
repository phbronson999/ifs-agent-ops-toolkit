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
class ComplexType:
    fqn: str
    name: str
    properties: list[Property] = field(default_factory=list)


@dataclass
class Operation:
    """An OData Action or Function declaration (bound or unbound)."""
    fqn: str
    name: str
    kind: str  # "action" | "function"
    is_bound: bool
    parameters: list[Property] = field(default_factory=list)  # for bound ops the first is the binding parameter
    return_type: Optional[str] = None

    @property
    def binding_type(self) -> Optional[str]:
        return self.parameters[0].type if self.is_bound and self.parameters else None

    @property
    def call_parameters(self) -> list[Property]:
        """Parameters a caller supplies (the binding parameter excluded)."""
        return self.parameters[1:] if self.is_bound else list(self.parameters)


@dataclass
class EdmxModel:
    enum_types: dict[str, EnumType] = field(default_factory=dict)
    entity_types: dict[str, EntityType] = field(default_factory=dict)
    entity_sets: dict[str, EntitySet] = field(default_factory=dict)
    complex_types: dict[str, ComplexType] = field(default_factory=dict)
    operations: dict[str, list[Operation]] = field(default_factory=dict)  # fqn -> overloads
    action_imports: dict[str, str] = field(default_factory=dict)    # import name -> action fqn
    function_imports: dict[str, str] = field(default_factory=dict)  # import name -> function fqn

    def enum_properties(self, entity_type_fqn: str) -> dict[str, EnumType]:
        """Properties of an entity type whose declared type is an enum, in document order."""
        et = self.entity_types.get(entity_type_fqn)
        if et is None:
            return {}
        return {p.name: self.enum_types[p.type] for p in et.properties if p.type in self.enum_types}

    def entity_type_of_set(self, entity_set: str) -> Optional[EntityType]:
        es = self.entity_sets.get(entity_set)
        return self.entity_types.get(es.entity_type) if es else None

    def structure(self, type_fqn: str):
        """The entity or complex type for a (possibly Collection(...)) type name, if declared."""
        base, _ = _strip_collection(type_fqn)
        return self.entity_types.get(base) or self.complex_types.get(base)

    def operation_import(self, name: str) -> Optional[Operation]:
        """Unbound action/function exposed at the service root under ``name``."""
        fqn = self.action_imports.get(name) or self.function_imports.get(name)
        overloads = self.operations.get(fqn or "", [])
        return next((o for o in overloads if not o.is_bound), overloads[0] if overloads else None)

    def bound_operation(self, fqn: str, binding_type: Optional[str] = None) -> Optional[Operation]:
        """Bound action/function by full name, preferring the overload bound to ``binding_type``."""
        overloads = [o for o in self.operations.get(fqn, []) if o.is_bound]
        if binding_type:
            match = next((o for o in overloads if _strip_collection(o.binding_type or "")[0] == binding_type), None)
            if match:
                return match
        return overloads[0] if overloads else None


def _strip_collection(type_name: str) -> tuple[str, bool]:
    if type_name.startswith("Collection(") and type_name.endswith(")"):
        return type_name[len("Collection("):-1], True
    return type_name, False


def parse_edmx(xml_text: str) -> EdmxModel:
    """Parse EDMX text. Raises ``xml.etree.ElementTree.ParseError`` on malformed XML."""
    root = ET.fromstring(xml_text)
    model = EdmxModel()
    schemas = root.findall(".//edm:Schema", NS)

    # Enums first so property types can be recognized regardless of schema order.
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

        for el in schema.findall("edm:ComplexType", NS):
            name = el.get("Name", "")
            model.complex_types[f"{namespace}.{name}"] = ComplexType(
                fqn=f"{namespace}.{name}", name=name, properties=[_prop(p) for p in el.findall("edm:Property", NS)])

        for tag, kind in (("Action", "action"), ("Function", "function")):
            for el in schema.findall(f"edm:{tag}", NS):
                name = el.get("Name", "")
                ret = el.find("edm:ReturnType", NS)
                op = Operation(
                    fqn=f"{namespace}.{name}", name=name, kind=kind,
                    is_bound=el.get("IsBound", "false").lower() == "true",
                    parameters=[_prop(p) for p in el.findall("edm:Parameter", NS)],
                    return_type=ret.get("Type") if ret is not None else None,
                )
                model.operations.setdefault(op.fqn, []).append(op)

        for container in schema.findall("edm:EntityContainer", NS):
            for es in container.findall("edm:EntitySet", NS):
                name = es.get("Name", "")
                model.entity_sets[name] = EntitySet(name=name, entity_type=es.get("EntityType", ""))
            for ai in container.findall("edm:ActionImport", NS):
                model.action_imports[ai.get("Name", "")] = ai.get("Action", "")
            for fi in container.findall("edm:FunctionImport", NS):
                model.function_imports[fi.get("Name", "")] = fi.get("Function", "")

    return model


def _prop(el) -> Property:
    return Property(name=el.get("Name", ""), type=el.get("Type", ""),
                    nullable=el.get("Nullable", "true").lower() != "false")
