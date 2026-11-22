"""Agent generator (Phase 13): approved capabilities -> a Microsoft 365 declarative agent (Agents Toolkit project).

One agent per package, written to ``<package>/agent/m365-agent/``, following the proven path: declarative agent
-> API plugin (OpenAPI, ``OAuthPluginVault``) -> the deployed flows' authenticated HTTP triggers (Phase 12) ->
connector -> on-premises gateway -> IFS. Each capability action is one plugin function; ``prepare`` is not
consequential (Copilot calls it without asking), ``submit`` is (Copilot asks before it writes).

Environment-specific values are placeholders, filled per target by ``deploy/agent.py`` into a machine-local copy
where ``atk provision`` runs: ``%%FLOW_SERVER%%`` and ``%%TRIGGER_PATH:<flow>%%`` (from the target's recorded
trigger URLs) and ``%%TENANT_ID%%`` (the sign-in tenant). Values written by the Agents Toolkit itself (app id, OAuth
registration id) use its own ``${{VAR}}`` placeholders and live in the target's env file.
"""
from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from ifs_agent_ops.generators.flow import flow_dir, flow_name
from ifs_agent_ops.generators.gating import GenerationRefused, gate_capability
from ifs_agent_ops.operations.capability import Action, Capability
from ifs_agent_ops.operations.capability_store import load_capabilities
from ifs_agent_ops.operations.store import load_operations
from ifs_agent_ops.workspace.package import Package

GENERATOR = "ifs-agent-ops agent generator v1"
MARKER = "<!-- ifs-agent-ops:generated agent -->"
AGENT_DIR = "agent/m365-agent"
REPORT_FILE = "report.md"
SPEC_FILE = "appPackage/apiSpecificationFile/ifs-flows.yaml"
SERVER_TOKEN = "%%FLOW_SERVER%%"
TENANT_TOKEN = "%%TENANT_ID%%"
REGISTRATION_VAR = "IFSFLOWS_REGISTRATION_ID"
FLOW_SCOPE = "https://service.flow.microsoft.com//.default"   # as in the proven agent (double slash included)
MAX_INSTRUCTIONS = 8000


def path_token(flow: str) -> str:
    return f"%%TRIGGER_PATH:{flow}%%"


def agent_root(package: Package) -> Path:
    return package.root / AGENT_DIR


def function_name(capability: str, action: str) -> str:
    """create-customer-order + prepare -> createCustomerOrderPrepare."""
    words = re.split(r"[^A-Za-z0-9]+", f"{capability} {action}")
    words = [w for w in words if w]
    return words[0].lower() + "".join(w[:1].upper() + w[1:] for w in words[1:])


def _sentences(text: str, n: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    return " ".join(parts[:n])


# ── instructions ─────────────────────────────────────────────────────────────

def _instructions(package: Package, caps: list[Capability]) -> str:
    title = package.display_name or package.name
    L = ["# Role",
         f"You are the {title} assistant. You work in IFS Cloud only through the actions listed below. Users are "
         "employees who handle orders and documents from customers.", "",
         "# Accuracy",
         "- Use only values from the user, the user's documents and action results. Never invent customer numbers, "
         "part numbers, codes, prices or order numbers.",
         "- If an action returns `ok: false`, say what failed using `error.meaning` and the IFS message, and stop. "
         "Do not retry with guessed values.",
         "- Use tables for lists of candidates, lines and results.", ""]
    for cap in caps:
        actions = {a.name: a for a in cap.actions}
        read = [a for a in cap.actions if not a.requires_confirmation]
        write = [a for a in cap.actions if a.requires_confirmation]
        L += [f"# {cap.title}", _sentences(cap.description, 2), "", "Inputs:"]
        for i in cap.inputs:
            req = "required" if i.required else "optional"
            L.append(f"- `{i.name}` ({req}): {_sentences(i.description, 1)}")
            for it in i.items:
                L.append(f"  - `{i.name}[].{it.name}` ({'required' if it.required else 'optional'}): "
                         f"{_sentences(it.description, 1)}")
        L += ["", "Steps:"]
        n = 1
        for a in read:
            fn = function_name(cap.id, a.name)
            L.append(f"{n}. Call `{fn}` with every input you have. If it returns `questions`, ask the user each "
                     "question (use its prompt) and show the candidates from `proposal.results` and "
                     f"`proposal.lines` as a table. Call `{fn}` again with the answers until `questions` is empty "
                     "and `ok` is true.")
            n += 1
        for a in write:
            fn = function_name(cap.id, a.name)
            shown = ", ".join(f"`{c}`" for c in a.confirm) or "the inputs"
            L.append(f"{n}. Show the proposal for confirmation: {shown}, and every warning. Ask the user to confirm.")
            n += 1
            L.append(f"{n}. Only after the user explicitly confirms, call `{fn}` with exactly the confirmed inputs "
                     "and `confirmed: true`. Never call it before that.")
            n += 1
            outs = [o for o in cap.outputs if o.name in a.returns]
            if outs:
                L.append(f"{n}. Report the result: " + "; ".join(
                    f"`{o.name}` ({_sentences(o.description, 1).rstrip('.')})" if o.description else f"`{o.name}`"
                    for o in outs) + ". Show IFS messages from `warnings`. If `ok` is false after a write, the "
                    "outputs show what was already created: tell the user, so they can finish or cancel it in IFS.")
                n += 1
        if cap.deferred:
            L += ["", "Not supported (say so if asked): " + "; ".join(
                d.split(":")[0].split("(")[0].strip().rstrip(".") for d in cap.deferred) + "."]
        L.append("")
    text = "\n".join(L).strip() + "\n"
    if len(text) > MAX_INSTRUCTIONS:
        raise GenerationRefused(f"agent instructions are {len(text)} characters (limit {MAX_INSTRUCTIONS})")
    return text


# ── plugin + OpenAPI ─────────────────────────────────────────────────────────

def _response_schema(action: Action) -> dict:
    props = {"ok": {"type": "boolean"}, "error": {"type": "object", "nullable": True},
             "capability": {"type": "string"}, "revision": {"type": "integer"}, "action": {"type": "string"},
             "warnings": {"type": "array", "items": {"type": "object"}}}
    if "proposal" in action.returns:
        props["proposal"] = {"type": "object"}
    if "questions" in action.returns:
        props["questions"] = {"type": "array", "items": {"type": "object"}}
    if action.requires_confirmation:
        props["outputs"] = {"type": "object"}
    return {"type": "object", "properties": props}


def _spec(package: Package, caps: list[Capability], flows: dict[tuple[str, str], dict]) -> dict:
    paths = {}
    for cap in caps:
        for a in cap.actions:
            trigger = flows[(cap.id, a.name)]["properties"]["definition"]["triggers"]["manual"]["inputs"]
            paths[path_token(flow_name(cap.id, a.name))] = {"post": {
                "operationId": function_name(cap.id, a.name),
                "summary": f"{cap.title}: {a.name}",
                "description": _sentences(a.description, 2) or cap.title,
                "x-openai-isConsequential": bool(a.requires_confirmation),
                "parameters": [{"name": "api-version", "in": "query", "required": True,
                                "schema": {"type": "string", "enum": ["1"], "default": "1"}}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": trigger["schema"]}}},
                "responses": {"200": {"description": "Result of the action",
                                      "content": {"application/json": {"schema": _response_schema(a)}}}},
                "security": [{"ifsFlows": []}]}}
    login = f"https://login.microsoftonline.com/{TENANT_TOKEN}/oauth2/v2.0"
    return {"openapi": "3.0.1",
            "info": {"title": package.display_name or package.name,
                     "description": "IFS Cloud capabilities through Power Automate flows.", "version": package.version},
            "servers": [{"url": SERVER_TOKEN}],
            "paths": paths,
            "components": {"securitySchemes": {"ifsFlows": {"type": "oauth2", "flows": {"authorizationCode": {
                "authorizationUrl": f"{login}/authorize", "tokenUrl": f"{login}/token",
                "scopes": {FLOW_SCOPE: "Access Power Automate"}}}}}}}


def _plugin(package: Package, caps: list[Capability]) -> dict:
    functions = []
    for cap in caps:
        for a in cap.actions:
            functions.append({"name": function_name(cap.id, a.name),
                              "description": (_sentences(a.description, 3) or cap.title)[:1000]})
    namespace = re.sub(r"[^a-z0-9]", "", package.name.lower())[:30] or "ifs"
    title = package.display_name or package.name
    return {"$schema": "https://developer.microsoft.com/json-schemas/copilot/plugin/v2.4/schema.json",
            "schema_version": "v2.4",
            "name_for_human": title[:20],
            "description_for_human": f"{title} in IFS Cloud"[:100],
            "description_for_model": "Works with IFS Cloud through Power Automate flows: " + "; ".join(
                f"{c.title} ({', '.join(a.name for a in c.actions)})" for c in caps),
            "contact_email": "${{DEVELOPER_EMAIL}}",
            "namespace": namespace,
            "capabilities": {},
            "functions": functions,
            "runtimes": [{"type": "OpenApi",
                          "auth": {"type": "OAuthPluginVault", "reference_id": "${{" + REGISTRATION_VAR + "}}"},
                          "spec": {"url": SPEC_FILE.split("appPackage/", 1)[1]},
                          "run_for_functions": [f["name"] for f in functions]}]}


def _declarative(package: Package, caps: list[Capability]) -> dict:
    title = package.display_name or package.name
    starters = [{"text": f"{c.title} from a customer's document"[:100]} for c in caps][:6]
    return {"$schema": "https://developer.microsoft.com/json-schemas/copilot/declarative-agent/v1.9/schema.json",
            "version": "v1.9",
            "name": f"{title}" + "${{APP_NAME_SUFFIX}}",
            "description": (package.description or f"{title} in IFS Cloud")[:1000],
            "instructions": "$[file('instruction.txt')]",
            "conversation_starters": starters,
            "actions": [{"id": "ifsFlows", "file": "ai-plugin.json"}]}


def app_version(package_version: str) -> str:
    """The app's version from the package version, major + 1: Microsoft rejects app versions starting with 0, and
    this keeps it increasing whenever the package version does (0.1.0 -> 1.1.0, 1.2.3 -> 2.2.3)."""
    parts = [int(p) for p in re.findall(r"\d+", package_version or "0")][:3]
    parts += [0] * (3 - len(parts))
    return f"{parts[0] + 1}.{parts[1]}.{parts[2]}"


def _manifest(package: Package) -> dict:
    title = package.display_name or package.name
    return {"$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.30/MicrosoftTeams.schema.json",
            "manifestVersion": "1.30",
            "version": app_version(package.version),
            "id": "${{TEAMS_APP_ID}}",
            "developer": {"name": "${{DEVELOPER_NAME}}", "websiteUrl": "${{DEVELOPER_WEBSITE_URL}}",
                          "privacyUrl": "${{DEVELOPER_PRIVACY_URL}}", "termsOfUseUrl": "${{DEVELOPER_TERMS_URL}}"},
            "icons": {"color": "color.png", "outline": "outline.png"},
            "name": {"short": (title[:22] + "${{APP_NAME_SUFFIX}}"), "full": title[:100]},
            "description": {"short": f"{title} in IFS Cloud"[:80],
                            "full": (package.description or f"{title} in IFS Cloud through Power Automate.")[:4000]},
            "accentColor": "#FFFFFF",
            "composeExtensions": [],
            "permissions": ["identity", "messageTeamMembers"],
            "copilotAgents": {"declarativeAgents": [{"id": "declarativeAgent", "file": "declarativeAgent.json"}]},
            "validDomains": []}


def _agents_yml(package: Package) -> str:
    title = package.display_name or package.name
    zip_ = "./appPackage/build/appPackage.${{TEAMSFX_ENV}}.zip"
    common = [
        {"uses": "teamsApp/zipAppPackage", "with": {"manifestPath": "./appPackage/manifest.json",
                                                     "outputZipPath": zip_, "outputFolder": "./appPackage/build"}},
        {"uses": "teamsApp/validateAppPackage", "with": {"appPackagePath": zip_}},
        {"uses": "teamsApp/update", "with": {"appPackagePath": zip_}}]
    data = {
        "version": "v1.12",
        "environmentFolderPath": "./env",
        "provision": [
            {"uses": "teamsApp/create", "with": {"name": title + "${{APP_NAME_SUFFIX}}"},
             "writeToEnvironmentFile": {"teamsAppId": "TEAMS_APP_ID"}},
            {"uses": "oauth/register", "with": {"name": "ifsFlows", "appId": "${{TEAMS_APP_ID}}",
                                                "apiSpecPath": "./" + SPEC_FILE, "flow": "authorizationCode"},
             "writeToEnvironmentFile": {"configurationId": REGISTRATION_VAR}},
            *common,
            {"uses": "copilotAgent/publish", "with": {"appPackagePath": zip_, "scope": "${{AGENT_SCOPE}}"},
             "writeToEnvironmentFile": {"titleId": "M365_TITLE_ID", "appId": "M365_APP_ID",
                                        "shareLink": "SHARE_LINK"}}],
        "publish": [*common,
                    {"uses": "copilotAgent/publish", "with": {"appPackagePath": zip_, "scope": "tenant"},
                     "writeToEnvironmentFile": {"titleId": "M365_PUBLISHED_TITLE_ID",
                                                "appId": "M365_PUBLISHED_APP_ID"}}]}
    header = ("# yaml-language-server: $schema=https://aka.ms/m365-agents-toolkits/v1.12/yaml.schema.json\n"
              f"# Generated by {GENERATOR}; do not edit. Provision a rendered copy (ifs-agent-ops agent render).\n")
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


# ── icons ────────────────────────────────────────────────────────────────────

def _png(size: int, rgba: tuple[int, int, int, int], border: Optional[tuple] = None) -> bytes:
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            edge = border and (x < size // 8 or y < size // 8 or x >= size - size // 8 or y >= size - size // 8)
            row += bytes(border if edge else rgba)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


# ── generation ───────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    path: Path
    action: str      # created | updated | unchanged | removed | would-create | would-update | would-remove | refused
    message: str = ""


@dataclass
class Generation:
    results: list[AgentResult]
    functions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def build(package: Package) -> tuple[dict[str, bytes], list[str]]:
    caps_all = load_capabilities(package)
    ops = load_operations(package)
    caps = [c for c in caps_all.values() if c.status == "approved"]
    if not caps:
        raise GenerationRefused("no approved capabilities: nothing for the agent to do")
    flows = {}
    for cap in caps:
        gate_capability(cap, ops)
        for a in cap.actions:
            path = flow_dir(package, cap.id) / f"{a.name}.json"
            if not path.exists():
                raise GenerationRefused(f"flow {path.relative_to(package.root)} is not generated (ifs-agent-ops flow "
                                        f"generate --package {package.name})")
            flows[(cap.id, a.name)] = json.loads(path.read_text(encoding="utf-8"))
    files: dict[str, bytes] = {}

    def text(name: str, content: str) -> None:
        files[name] = content.encode("utf-8")

    def js(name: str, data: dict) -> None:
        text(name, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    text("m365agents.yml", _agents_yml(package))
    js("appPackage/manifest.json", _manifest(package))
    js("appPackage/declarativeAgent.json", _declarative(package, caps))
    text("appPackage/instruction.txt", _instructions(package, caps))
    js("appPackage/ai-plugin.json", _plugin(package, caps))
    text(SPEC_FILE, yaml.safe_dump(_spec(package, caps, flows), sort_keys=False, allow_unicode=True, width=120))
    files["appPackage/color.png"] = _png(192, (0, 94, 184, 255))
    files["appPackage/outline.png"] = _png(32, (0, 0, 0, 0), border=(255, 255, 255, 255))
    functions = [function_name(c.id, a.name) for c in caps for a in c.actions]
    text(REPORT_FILE, "\n".join([
        MARKER, f"# Microsoft 365 agent for `{package.name}`", "",
        f"Generated by {GENERATOR} from the approved capabilities; do not edit. Regenerate with "
        f"`ifs-agent-ops agent generate --package {package.name}`; render for a target and provision with "
        f"`ifs-agent-ops agent render --package {package.name} --target <target>`.", "",
        "| Function | Capability | Action | Consequential |", "|---|---|---|---|",
        *[f"| `{function_name(c.id, a.name)}` | {c.id} (revision {c.revision}) | {a.name} | "
          f"{'yes (Copilot asks before calling)' if a.requires_confirmation else 'no'} |" for c in caps for a in c.actions],
        "", "Placeholders filled per target (machine-local copy): `%%FLOW_SERVER%%` and `%%TRIGGER_PATH:<flow>%%` from "
        "the target's recorded flow trigger URLs, `%%TENANT_ID%%` from the sign-in tenant. The Agents Toolkit writes "
        f"`TEAMS_APP_ID` and `{REGISTRATION_VAR}` into the target's env file during provision.", ""]))
    return files, functions


def generate(package: Package, check: bool = False) -> Generation:
    root = agent_root(package)
    try:
        files, functions = build(package)
    except GenerationRefused as exc:
        return Generation([AgentResult(root, "refused", str(exc))], errors=[str(exc)])
    report = root / REPORT_FILE
    if root.is_dir() and any(root.iterdir()) and (not report.exists()
                                                   or MARKER not in report.read_text(encoding="utf-8")):
        msg = f"{root} holds files that were not generated; not overwriting them"
        return Generation([AgentResult(root, "refused", msg)], errors=[msg])
    results = []
    for name, data in files.items():
        path = root / name
        old = path.read_bytes() if path.exists() else None
        if old == data:
            results.append(AgentResult(path, "unchanged"))
        elif check:
            results.append(AgentResult(path, "would-create" if old is None else "would-update"))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            results.append(AgentResult(path, "created" if old is None else "updated"))
    if root.is_dir():
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            if path.relative_to(root).as_posix() not in files:
                if check:
                    results.append(AgentResult(path, "would-remove"))
                else:
                    path.unlink()
                    results.append(AgentResult(path, "removed"))
    return Generation(results, functions)
