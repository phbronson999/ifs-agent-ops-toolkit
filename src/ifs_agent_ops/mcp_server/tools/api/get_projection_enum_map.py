import json
import xml.etree.ElementTree as ET
from typing import Optional

from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.errors import AuthenticationRequired, IfsApiError
from ifs_agent_ops.ifs.metadata.edmx import parse_edmx
from ifs_agent_ops.ifs.paths import metadata_path
from ifs_agent_ops.mcp_server.presenters import UNAUTHORIZED_SHORT, auth_message


async def handler(
    projection_name: str,
    entity_set: Optional[str],
    include_reference_sets: bool,
    client: IfsClient,
) -> str:
    try:
        xml_text = await client.get_text(metadata_path(projection_name), accept="application/xml")
    except AuthenticationRequired as err:
        return json.dumps({
            "error": "authentication_required",
            "message": auth_message(err, unauthorized=UNAUTHORIZED_SHORT),
        }, indent=2)
    except IfsApiError as err:
        return json.dumps({
            "error": "metadata_fetch_failed",
            "status": err.status,
            "message": err.text[:500],
        }, indent=2)
    except Exception as exc:
        return json.dumps({"error": "metadata_fetch_failed", "message": str(exc)}, indent=2)

    try:
        model = parse_edmx(xml_text)
    except ET.ParseError as exc:
        return json.dumps({"error": "metadata_parse_failed", "message": str(exc)}, indent=2)

    set_types = {name: es.entity_type for name, es in model.entity_sets.items()}

    if entity_set:
        if entity_set not in set_types:
            return json.dumps({
                "error": "entity_set_not_found",
                "projection": projection_name,
                "entity_set": entity_set,
                "available": sorted(set_types.keys()),
            }, indent=2)
        sets_to_include = {entity_set: set_types[entity_set]}
    else:
        sets_to_include = {
            k: v for k, v in set_types.items()
            if include_reference_sets or not k.startswith("Reference_")
        }

    result_sets = []
    for es_name, et_fqn in sorted(sets_to_include.items()):
        enum_props = {
            prop: {"enum_type": enum.fqn, "members": enum.members}
            for prop, enum in model.enum_properties(et_fqn).items()
        }
        result_sets.append({
            "entity_set": es_name,
            "entity_type": et_fqn,
            "enum_properties": enum_props,
        })

    return json.dumps({
        "projection": projection_name,
        "entity_sets": result_sets,
    }, indent=2)
