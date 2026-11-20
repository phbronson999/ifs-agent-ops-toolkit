"""IFS projection URL builders and OData query-string helpers."""
from __future__ import annotations

from urllib.parse import parse_qs

PROJECTION_ROOT = "/main/ifsapplications/projection/v1"


def projection_path(projection: str) -> str:
    return f"{PROJECTION_ROOT}/{projection}.svc"


def metadata_path(projection: str) -> str:
    return f"{projection_path(projection)}/$metadata"


def openapi_v2_path(projection: str) -> str:
    return f"{projection_path(projection)}/$openapi?V2"


def strip_pagination(endpoint: str) -> str:
    """Remove $top/$skip from an endpoint's query string (other params kept, decoded)."""
    if "?" not in endpoint:
        return endpoint
    base, qs = endpoint.split("?", 1)
    params = parse_qs(qs, keep_blank_values=True)
    params.pop("$top", None)
    params.pop("$skip", None)
    remaining = "&".join(f"{k}={v}" for k, vs in params.items() for v in vs)
    return f"{base}?{remaining}" if remaining else base


def append_param(endpoint: str, param: str) -> str:
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}{param}"
