"""Pin a projection's ``$metadata`` (and ``$openapi?V2``) into a package.

Snapshots make drafting and generation reproducible: they read the package's pinned copy,
never a live system. Like recordings, snapshots are environment-independent: the real IFS host
becomes ``ifs.example`` and the Keycloak realm becomes ``example`` (the OpenAPI document
embeds both in ``host`` and ``securityDefinitions``). The write is refused if either survives.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml

from ifs_agent_ops.ifs.client import IfsClient
from ifs_agent_ops.ifs.metadata.edmx import EdmxModel, parse_edmx
from ifs_agent_ops.ifs.paths import metadata_path, openapi_v2_path
from ifs_agent_ops.recordings.redact import PLACEHOLDER_IFS_HOST
from ifs_agent_ops.workspace.package import Package

METADATA_FILE = "metadata.xml"
OPENAPI_FILE = "openapi.v2.json"
SNAPSHOT_FILE = "snapshot.yaml"
PLACEHOLDER_REALM = "example"


class SnapshotRefused(RuntimeError):
    pass


def normalize(text: str, host: str, realm: str) -> str:
    if host:
        text = re.sub(re.escape(host), PLACEHOLDER_IFS_HOST, text, flags=re.I)
    if realm:
        text = re.sub(r"(/realms/)" + re.escape(realm) + r"(?=[/\"'?#]|$)", r"\g<1>" + PLACEHOLDER_REALM, text)
    return text


def snapshot_dir(package: Package, projection: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_]+", projection):
        raise SnapshotRefused(f"Invalid projection name {projection!r}")
    return package.root / "metadata" / projection


def write_snapshot(package: Package, projection: str, *, metadata_xml: str, openapi_json: Optional[str],
                   base_url: str, realm: str, source: str) -> Path:
    host = (urlsplit(base_url).hostname or "").lower()
    files = {METADATA_FILE: normalize(metadata_xml, host, realm)}
    if openapi_json is not None:
        files[OPENAPI_FILE] = normalize(openapi_json, host, realm)
    parse_edmx(files[METADATA_FILE])  # refuse unparseable metadata
    for name, text in files.items():
        if host and host in text.lower():
            raise SnapshotRefused(f"{name}: real host still present after normalisation; not written")
        if realm and re.search(r"/realms/" + re.escape(realm) + r"\b", text):
            raise SnapshotRefused(f"{name}: realm still present after normalisation; not written")

    target = snapshot_dir(package, projection)
    target.mkdir(parents=True, exist_ok=True)
    manifest = {"projection": projection, "source": source,
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "toolkit_version": _toolkit_version(), "files": {}}
    for name, text in files.items():
        (target / name).write_text(text, encoding="utf-8", newline="\n")
        data = text.encode("utf-8")
        manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    (target / SNAPSHOT_FILE).write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    return target


async def fetch_snapshot(client: IfsClient, package: Package, projection: str, *, realm: str,
                         include_openapi: bool = True, source: str = "live") -> Path:
    metadata_xml = await client.get_text(metadata_path(projection), accept="application/xml")
    openapi_json = await client.get_text(openapi_v2_path(projection), accept="application/json") \
        if include_openapi else None
    return write_snapshot(package, projection, metadata_xml=metadata_xml, openapi_json=openapi_json,
                          base_url=client.base_url, realm=realm, source=source)


def load_metadata(package: Package, projection: str) -> EdmxModel:
    path = snapshot_dir(package, projection) / METADATA_FILE
    if not path.exists():
        raise FileNotFoundError(f"No metadata snapshot for {projection} in {package.name}; run "
                                f"`ifs-agent-ops metadata snapshot --package {package.name} --projection {projection}`")
    return parse_edmx(path.read_text(encoding="utf-8"))


def _toolkit_version() -> str:
    try:
        return version("ifs-agent-ops-toolkit")
    except PackageNotFoundError:
        return "unknown"
