"""Dependency pruning (plan §5a C7): keep the ``$ref`` closure of the kept paths, drop the rest.

Unlike the reducer notebook, a ``$ref`` that resolves to nothing is collected as an error, never
silently ignored.
"""
from __future__ import annotations

_SECTIONS = ("definitions", "parameters", "responses")


def _refs(node, out: list[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append(ref)
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def closure(paths: dict, components: dict[str, dict]) -> tuple[dict[str, list[str]], list[str]]:
    """Referenced names per section (in first-reference order) and dangling references."""
    keep: dict[str, list[str]] = {s: [] for s in _SECTIONS}
    dangling: list[str] = []
    pending: list[str] = []
    _refs(paths, pending)
    seen: set[str] = set()
    while pending:
        ref = pending.pop(0)
        if ref in seen:
            continue
        seen.add(ref)
        parts = ref.split("/")
        if len(parts) != 3 or parts[0] != "#" or parts[1] not in _SECTIONS:
            dangling.append(ref)
            continue
        section, name = parts[1], parts[2]
        target = components.get(section, {}).get(name)
        if target is None:
            dangling.append(ref)
            continue
        keep[section].append(name)
        found: list[str] = []
        _refs(target, found)
        pending.extend(found)
    return keep, dangling


def prune(paths: dict, components: dict[str, dict], tags: list[dict]) -> tuple[dict, list[dict], list[str], dict]:
    """Pruned components (in source order), used tags, dangling refs and drop counts."""
    keep, dangling = closure(paths, components)
    out = {s: {k: v for k, v in components.get(s, {}).items() if k in set(keep[s])} for s in _SECTIONS}
    used_tags = {t for item in paths.values() for o in item.values() if isinstance(o, dict) for t in o.get("tags", [])}
    kept_tags = [t for t in tags if t.get("name") in used_tags]
    dropped = {s: len(components.get(s, {})) - len(out[s]) for s in _SECTIONS}
    dropped["tags"] = len(tags) - len(kept_tags)
    return out, kept_tags, dangling, dropped
