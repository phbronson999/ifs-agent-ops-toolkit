"""Runtime cases (Phase 12c): calls through the deployed flows, checked against expectations.

``<package>/validation/<capability>.runtime.yaml`` (committed; TEST data only, like the findings)::

    capability: create-customer-order
    cases:
      - name: known customer and part
        action: prepare
        inputs: {customer_no: "3822", customer_po_no: AGENT-TEST-1, wanted_delivery_date: "2026-12-15",
                 order_coordinator: "18", lines: [{catalog_no: "900664", quantity: 1000}]}
        expect:
          ok: true
          questions: []                          # exact set of question ids
          warnings: [check-customer-po]          # exact set of warning steps ("ifs" for IFS messages)
          matches: {find-customer: 1, list-coordinators: ">0"}
          lines:
            - {check-part: 1, search-part: {includes: {CatalogNo: "900664"}}}
      - name: create an order
        action: submit                           # writes: run only with deploy validate --allow-writes
        inputs: {...}
        expect: {ok: true, outputs: {order_no: present, line_numbers: {length: 1}}}

A match is a row count (``1``), ``">0"``, ``"none"`` or ``{includes: {Field: value}}`` (some row has it). The call
is made to the flow's HTTP trigger (recorded in the target) with the Azure CLI sign-in; nothing else is needed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx
import yaml

from ifs_agent_ops.deploy.flowapi import FlowApiError, az_token
from ifs_agent_ops.workspace.package import Package

FLOW_TRIGGER_AUDIENCE = "https://service.flow.microsoft.com/"   # the trailing slash matters (no slash: 403)
CALL_TIMEOUT = 150.0


class CaseError(ValueError):
    pass


@dataclass
class Case:
    name: str
    action: str
    inputs: dict
    expect: dict = field(default_factory=dict)


@dataclass
class CaseResult:
    capability: str
    case: str
    action: str
    status: str                 # PASS | FAIL | NOT RUN
    detail: str = ""
    seconds: float = 0.0
    response: Optional[dict] = None


def cases_path(package: Package, capability: str) -> Path:
    return package.root / "validation" / f"{capability}.runtime.yaml"


def load_cases(package: Package, capability: str) -> list[Case]:
    path = cases_path(package, capability)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for i, c in enumerate(data.get("cases") or []):
        if not isinstance(c, dict) or not c.get("name") or not c.get("action"):
            raise CaseError(f"{path}: case {i + 1} needs name and action")
        out.append(Case(str(c["name"]), str(c["action"]), dict(c.get("inputs") or {}), dict(c.get("expect") or {})))
    return out


# ── expectations ─────────────────────────────────────────────────────────────

def _rows(value) -> list:
    return value.get("value", []) if isinstance(value, dict) and isinstance(value.get("value"), list) else (
        [value] if value else [])


def _match(rows: list, want) -> Optional[str]:
    n = len(rows)
    if isinstance(want, int) and not isinstance(want, bool):
        return None if n == want else f"{n} row(s), expected {want}"
    if want == ">0" and n == 0:
        return "no rows, expected some"
    if want == "none" and n:
        return f"{n} row(s), expected none"
    if isinstance(want, dict) and "includes" in want:
        need = {k: str(v) for k, v in want["includes"].items()}
        if not any(all(str(r.get(k)) == v for k, v in need.items()) for r in rows):
            return f"no row with {need} among {n}"
    return None


def check(expect: dict, response: dict) -> list[str]:
    """Problems with a flow response against a case's expectations (empty list: passed)."""
    problems = []
    if "ok" in expect and response.get("ok") != expect["ok"]:
        problems.append(f"ok is {response.get('ok')}, expected {expect['ok']}"
                        + (f" (error: {response.get('error')})" if response.get("error") else ""))
    if "questions" in expect:
        got = sorted({q.get("id") for q in response.get("questions") or []})
        if got != sorted(set(expect["questions"])):
            problems.append(f"questions {got}, expected {sorted(set(expect['questions']))}")
    if "warnings" in expect:
        got = sorted({w.get("step") if w.get("source") == "step" else "ifs" for w in response.get("warnings") or []})
        if got != sorted(set(expect["warnings"])):
            problems.append(f"warnings {got}, expected {sorted(set(expect['warnings']))}")
    results = (response.get("proposal") or {}).get("results") or {}
    for step, want in (expect.get("matches") or {}).items():
        why = _match(_rows(results.get(step)), want)
        if why:
            problems.append(f"{step}: {why}")
    lines = (response.get("proposal") or {}).get("lines") or []
    for i, line_expect in enumerate(expect.get("lines") or []):
        if i >= len(lines):
            problems.append(f"line {i + 1}: missing in the response")
            continue
        for step, want in line_expect.items():
            why = _match(_rows((lines[i].get("results") or {}).get(step)), want)
            if why:
                problems.append(f"line {i + 1} {step}: {why}")
    outputs = response.get("outputs") or {}
    for name, want in (expect.get("outputs") or {}).items():
        value = outputs.get(name)
        if want == "present" and value in (None, "", []):
            problems.append(f"output {name} is empty")
        elif isinstance(want, dict) and "length" in want and len(value or []) != want["length"]:
            problems.append(f"output {name} has {len(value or [])} entries, expected {want['length']}")
    return problems


# ── calls ────────────────────────────────────────────────────────────────────

Caller = Callable[[str, dict], dict]


def call_flow(url: str, body: dict) -> dict:
    token = az_token(FLOW_TRIGGER_AUDIENCE)
    resp = httpx.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=CALL_TIMEOUT)
    if resp.status_code != 200:
        raise FlowApiError(f"trigger answered HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def run_cases(capability: str, cases: list[Case], triggers: dict[str, str], writes: set[str], allow_writes: bool,
              caller: Caller = call_flow) -> list[CaseResult]:
    """Run each case through its action's flow. Cases of actions that write run only with allow_writes."""
    from ifs_agent_ops.generators.flow import flow_name

    out = []
    for case in cases:
        name = flow_name(capability, case.action)
        url = triggers.get(name)
        if url is None:
            out.append(CaseResult(capability, case.name, case.action, "FAIL",
                                  f"no trigger URL for {name} in the target (deploy apply records it)"))
            continue
        if case.action in writes and not allow_writes:
            out.append(CaseResult(capability, case.name, case.action, "NOT RUN",
                                  "writes to IFS: run with --allow-writes"))
            continue
        body = dict(case.inputs)
        if case.action in writes:
            body.setdefault("confirmed", True)
        started = time.monotonic()
        try:
            response = caller(url, body)
        except (FlowApiError, httpx.HTTPError, ValueError) as exc:
            out.append(CaseResult(capability, case.name, case.action, "FAIL", str(exc),
                                  round(time.monotonic() - started, 1)))
            continue
        problems = check(case.expect, response)
        out.append(CaseResult(capability, case.name, case.action, "FAIL" if problems else "PASS",
                              "; ".join(problems) or "as expected", round(time.monotonic() - started, 1), response))
    return out
