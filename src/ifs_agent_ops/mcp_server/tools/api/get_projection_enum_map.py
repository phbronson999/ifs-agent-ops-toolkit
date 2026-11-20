import json
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

from ifs_agent_ops.config import get_api_base_url
from ifs_agent_ops.ifs.auth.oauth_manager import OAuthManager
from ifs_agent_ops.ifs.auth.session_manager import get_current_session_id

_NS = {
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "edm":  "http://docs.oasis-open.org/odata/ns/edm",
}


async def handler(
    projection_name: str,
    entity_set: Optional[str],
    include_reference_sets: bool,
    oauth_manager: OAuthManager,
) -> str:
    session_id = get_current_session_id()
    if not session_id:
        return json.dumps({
            "error": "authentication_required",
            "message": (
                "No active session. Call the start_oauth tool now to open the login "
                "window — do not ask the user to do this manually."
            ),
        }, indent=2)

    try:
        access_token = await oauth_manager.get_access_token(session_id)
    except Exception as exc:
        return json.dumps({"error": "authentication_required", "message": str(exc)}, indent=2)

    endpoint = f"/main/ifsapplications/projection/v1/{projection_name}.svc/$metadata"
    url = f"{get_api_base_url()}{endpoint}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/xml",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code in (401, 403):
            return json.dumps({
                "error": "authentication_required",
                "message": "Session expired or unauthorised. Call start_oauth to re-authenticate.",
            }, indent=2)

        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return json.dumps({
            "error": "metadata_fetch_failed",
            "status": exc.response.status_code,
            "message": exc.response.text[:500],
        }, indent=2)
    except Exception as exc:
        return json.dumps({"error": "metadata_fetch_failed", "message": str(exc)}, indent=2)

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        return json.dumps({"error": "metadata_parse_failed", "message": str(exc)}, indent=2)

    # Pass 1 — collect all EnumType definitions across all Schema elements.
    enum_defs: dict[str, list[str]] = {}  # fqn -> member names
    for schema in root.findall(".//edm:Schema", _NS):
        namespace = schema.get("Namespace", "")
        for enum_el in schema.findall("edm:EnumType", _NS):
            name = enum_el.get("Name", "")
            fqn = f"{namespace}.{name}"
            members = [m.get("Name", "") for m in enum_el.findall("edm:Member", _NS)]
            enum_defs[fqn] = members

    # Pass 2 — collect EntityType enum-typed properties and EntitySet -> EntityType map.
    entity_enum_props: dict[str, dict[str, dict]] = {}  # entity_type_name -> {prop -> {enum_type, members}}
    entity_set_map: dict[str, str] = {}  # entity_set_name -> fully qualified entity_type

    for schema in root.findall(".//edm:Schema", _NS):
        namespace = schema.get("Namespace", "")

        for et_el in schema.findall("edm:EntityType", _NS):
            et_name = et_el.get("Name", "")
            props: dict[str, dict] = {}
            for prop in et_el.findall("edm:Property", _NS):
                prop_type = prop.get("Type", "")
                if prop_type in enum_defs:
                    props[prop.get("Name", "")] = {
                        "enum_type": prop_type,
                        "members": enum_defs[prop_type],
                    }
            entity_enum_props[f"{namespace}.{et_name}"] = props

        container = schema.find("edm:EntityContainer", _NS)
        if container is not None:
            for es_el in container.findall("edm:EntitySet", _NS):
                es_name = es_el.get("Name", "")
                et_fqn = es_el.get("EntityType", "")
                entity_set_map[es_name] = et_fqn

    # Filter or enumerate entity sets.
    if entity_set:
        if entity_set not in entity_set_map:
            return json.dumps({
                "error": "entity_set_not_found",
                "projection": projection_name,
                "entity_set": entity_set,
                "available": sorted(entity_set_map.keys()),
            }, indent=2)
        sets_to_include = {entity_set: entity_set_map[entity_set]}
    else:
        sets_to_include = {
            k: v for k, v in entity_set_map.items()
            if include_reference_sets or not k.startswith("Reference_")
        }

    result_sets = []
    for es_name, et_fqn in sorted(sets_to_include.items()):
        enum_props = entity_enum_props.get(et_fqn, {})
        result_sets.append({
            "entity_set": es_name,
            "entity_type": et_fqn,
            "enum_properties": enum_props,
        })

    return json.dumps({
        "projection": projection_name,
        "entity_sets": result_sets,
    }, indent=2)
