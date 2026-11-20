import json
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OpenApiPropertyInfo:
    name: str
    type: str
    nullable: bool
    read_only: bool = False
    enum_values: list[str] = field(default_factory=list)
    max_length: Optional[int] = None


@dataclass
class OpenApiEntitySet:
    name: str
    entity_type_name: str
    key_property: Optional[str]
    collection_methods: list[str]
    single_methods: list[str]
    properties: list[OpenApiPropertyInfo]
    required_properties: list[str]


@dataclass
class OpenApiAction:
    name: str
    method: str
    path_pattern: str


@dataclass
class ParsedOpenApi:
    service_name: str
    base_path: str
    entity_sets: list[OpenApiEntitySet]
    actions: list[OpenApiAction]
    total_paths: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_key_property(path_pattern: str) -> Optional[str]:
    m = re.search(r"\('\{(\w+)\}'\)", path_pattern)
    return m.group(1) if m else None


def _resolve_schema(schema_ref: Optional[str], spec: dict) -> dict:
    if not schema_ref:
        return {}
    ref_name = schema_ref.replace("#/definitions/", "").replace("#/components/schemas/", "")
    defs = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
    return (defs.get(ref_name) or {}).get("properties", {})


def _resolve_required(schema_ref: Optional[str], spec: dict) -> list[str]:
    if not schema_ref:
        return []
    ref_name = schema_ref.replace("#/definitions/", "").replace("#/components/schemas/", "")
    defs = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
    return (defs.get(ref_name) or {}).get("required", [])


def _extract_schema_ref(operation: dict) -> Optional[str]:
    # Swagger 2.0 body parameter
    for p in operation.get("parameters", []):
        if p.get("in") == "body" and p.get("schema", {}).get("$ref"):
            return p["schema"]["$ref"]
    # OpenAPI 3.0 requestBody
    content = (operation.get("requestBody") or {}).get("content", {})
    for ct in content.values():
        ref = (ct.get("schema") or {}).get("$ref")
        if ref:
            return ref
    # GET 200 response ref
    resp200 = (operation.get("responses") or {}).get("200", {})
    ref = resp200.get("schema", {}).get("$ref")
    if ref:
        return ref
    items_ref = (
        resp200.get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("properties", {})
        .get("value", {})
        .get("items", {})
        .get("$ref")
    )
    return items_ref


def _build_property_info(name: str, prop_def: dict) -> OpenApiPropertyInfo:
    ptype = prop_def.get("type") or ("object" if prop_def.get("$ref") else "string")
    enum_vals = [str(v) for v in prop_def.get("enum", [])]
    return OpenApiPropertyInfo(
        name=name,
        type=ptype,
        nullable=bool(prop_def.get("x-nullable") or prop_def.get("nullable")),
        read_only=bool(prop_def.get("readOnly")),
        enum_values=enum_vals,
        max_length=prop_def.get("maxLength"),
    )


def _entity_set_name_from_path(raw_path: str) -> Optional[str]:
    segment = raw_path.split("/")[1] if "/" in raw_path else None
    if not segment:
        return None
    if segment.startswith("$") or segment.startswith("IfsApp"):
        return None
    return segment.split("(")[0]


# ---------------------------------------------------------------------------
# Main exports
# ---------------------------------------------------------------------------

def parse_openapi(file_path: str) -> ParsedOpenApi:
    with open(file_path, encoding="utf-8") as f:
        spec = json.load(f)

    service_name: str = (spec.get("info") or {}).get("title", "UnknownService")

    base_path: str = spec.get("basePath", "")
    if not base_path:
        servers = spec.get("servers", [])
        if servers:
            base_path = servers[0].get("url", "")

    paths: dict = spec.get("paths", {})
    total_paths = len(paths)

    http_methods = {"get", "post", "patch", "put", "delete"}

    entity_map: dict[str, dict] = {}
    actions: list[OpenApiAction] = []

    for raw_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        methods = [k for k in path_item if k in http_methods]

        # Detect IfsApp action paths
        if "IfsApp." in raw_path or re.search(r"\w+_\w+\(", raw_path):
            action_m = re.search(r"IfsApp\.[^(]+\.(\w+)", raw_path)
            action_name = (
                action_m.group(1) if action_m
                else raw_path.split("/")[1].split("(")[0] if "/" in raw_path
                else raw_path
            )
            for m in methods:
                actions.append(OpenApiAction(
                    name=action_name,
                    method=m.upper(),
                    path_pattern=raw_path,
                ))
            continue

        entity_set_name = _entity_set_name_from_path(raw_path)
        if not entity_set_name:
            continue

        if entity_set_name not in entity_map:
            entity_map[entity_set_name] = {
                "collection_methods": set(),
                "single_methods": set(),
                "key_property": None,
                "schema_ref": None,
            }

        entry = entity_map[entity_set_name]
        is_singleton = "(" in raw_path

        for m in methods:
            operation = path_item[m]
            upper_m = m.upper()
            if is_singleton:
                entry["single_methods"].add(upper_m)
                if not entry["key_property"]:
                    entry["key_property"] = _extract_key_property(raw_path)
            else:
                entry["collection_methods"].add(upper_m)

            if not entry["schema_ref"]:
                entry["schema_ref"] = _extract_schema_ref(operation)

    entity_sets: list[OpenApiEntitySet] = []
    for name, entry in entity_map.items():
        entity_type_name = name[:-3] if name.endswith("Set") else name
        raw_props = _resolve_schema(entry["schema_ref"], spec)
        required = _resolve_required(entry["schema_ref"], spec)
        properties = [_build_property_info(pn, pd) for pn, pd in raw_props.items()]

        entity_sets.append(OpenApiEntitySet(
            name=name,
            entity_type_name=entity_type_name,
            key_property=entry["key_property"],
            collection_methods=sorted(entry["collection_methods"]),
            single_methods=sorted(entry["single_methods"]),
            properties=properties,
            required_properties=required,
        ))

    return ParsedOpenApi(
        service_name=service_name,
        base_path=base_path,
        entity_sets=entity_sets,
        actions=actions,
        total_paths=total_paths,
    )


def summarise_openapi(parsed: ParsedOpenApi) -> str:
    lines = [
        f"OpenAPI spec for **{parsed.service_name}**",
        f"Base path: `{parsed.base_path}`",
        f"Total paths in spec: {parsed.total_paths}",
        "",
        f"Found **{len(parsed.entity_sets)}** entity set(s) and **{len(parsed.actions)}** action(s).",
        "",
    ]

    for es in parsed.entity_sets:
        all_methods = (
            [f"{m} (collection)" for m in es.collection_methods]
            + [f"{m} (single)" for m in es.single_methods]
        )
        lines.append(f"### {es.name}")
        lines.append(f"- Entity type: `{es.entity_type_name}`")
        if es.key_property:
            lines.append(f"- Key property: `{es.key_property}`")
        lines.append(f"- Operations: {', '.join(all_methods)}")
        lines.append(f"- Properties: {len(es.properties)} total, {len(es.required_properties)} required")
        if es.required_properties:
            lines.append(f"- Required: {', '.join(es.required_properties)}")
        enums = [p for p in es.properties if p.enum_values]
        if enums:
            lines.append(f"- Enum fields: {', '.join(p.name for p in enums)}")
        lines.append("")

    if parsed.actions:
        lines.append("### Actions / Functions")
        for a in parsed.actions:
            lines.append(f"- **{a.method} {a.name}** — `{a.path_pattern}`")
        lines.append("")

    return "\n".join(lines)
