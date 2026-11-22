"""Power Platform CLI (pac) access for the deployment adapter.

Every call goes through one :class:`Runner`, so tests substitute a fake and nothing else in the adapter
spawns processes. Reads use ``--json`` where pac supports it and FetchXML (``pac env fetch``, a
column-aligned table) otherwise. The sign-in is pac's own (``pac auth create``); the toolkit never handles
Power Platform credentials.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape


class PacError(RuntimeError):
    pass


class Runner:
    """Runs pac; returns stdout. Raises PacError on a non-zero exit or when pac is missing."""

    def __init__(self, timeout: float = 900.0) -> None:
        self.timeout = timeout

    def __call__(self, args: list[str]) -> str:
        exe = shutil.which("pac")
        if exe is None:
            raise PacError("Power Platform CLI (pac) not found; see: ifs-agent-ops doctor --for power-platform")
        try:
            out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=self.timeout,
                                 encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise PacError(f"pac {args[0]} {args[1] if len(args) > 1 else ''} timed out") from exc
        text = (out.stdout or "") + (out.stderr or "")
        if out.returncode != 0 or re.search(r"^Error:", text, re.MULTILINE):
            error = next((ln for ln in text.splitlines() if ln.startswith("Error:")), text.strip()[-500:])
            raise PacError(f"pac {' '.join(args[:2])}: {error}")
        return out.stdout or ""


def parse_table(text: str) -> list[dict[str, str]]:
    """Parse pac's column-aligned table output (``pac env fetch``): columns start where header names start."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    # the header is the first line made only of attribute names (lowercase); "Connected as ..." lines are not
    i = next((i for i, ln in enumerate(lines) if ln and re.fullmatch(r"[a-z0-9_]+(\s+[a-z0-9_]+)*", ln)), None)
    if i is None:
        return []
    starts = [m.start() for m in re.finditer(r"\S+", lines[i])]
    names = lines[i].split()
    return [dict(zip(names, (row[s:(starts[j + 1] if j + 1 < len(starts) else None)].strip()
                             for j, s in enumerate(starts))))
            for row in lines[i + 1:] if row.strip()]


def _attr(value: str) -> str:
    """An XML attribute value in single quotes: pac is a .cmd on Windows, which mangles double quotes."""
    return "'" + escape(value, {"'": "&apos;", '"': "&quot;"}) + "'"


def _json_list(text: str) -> list:
    start = text.find("[")
    if start < 0:
        raise PacError(f"pac returned no JSON: {text.strip()[:200]}")
    return json.loads(text[start:text.rindex("]") + 1])


class Pac:
    """Typed pac operations against one environment (``--environment <url>``)."""

    def __init__(self, environment_url: Optional[str] = None, runner: Optional[Runner] = None) -> None:
        self.env = environment_url
        self.run = runner or Runner()

    def _env(self) -> list[str]:
        return ["--environment", self.env] if self.env else []

    # ── reads ────────────────────────────────────────────────────────────────
    def environments(self) -> list[dict]:
        return _json_list(self.run(["env", "list", "--json"]))

    def whoami(self) -> str:
        return self.run(["org", "who", *self._env()])

    def solutions(self) -> list[dict]:
        return _json_list(self.run(["solution", "list", "--json", *self._env()]))

    def fetch(self, entity: str, attributes: list[str], conditions: dict[str, str] | None = None,
              like: bool = False) -> list[dict[str, str]]:
        """Rows of ``entity`` where every condition matches (eq, or like when ``like``)."""
        attrs = "".join(f"<attribute name={_attr(a)}/>" for a in attributes)
        op = "like" if like else "eq"
        conds = "".join(f"<condition attribute={_attr(k)} operator='{op}' value={_attr(v)}/>"
                        for k, v in (conditions or {}).items())
        xml = f"<fetch><entity name={_attr(entity)}>{attrs}" + (f"<filter>{conds}</filter>" if conds else "") \
              + "</entity></fetch>"
        return parse_table(self.run(["env", "fetch", "--xml", xml, *self._env()]))

    def download_connector(self, connector_id: str) -> tuple[dict, dict]:
        """(apiDefinition, apiProperties.properties) as deployed. Note: pac omits ``capabilities``."""
        with tempfile.TemporaryDirectory() as tmp:
            self.run(["connector", "download", "--connector-id", connector_id, "--outputDirectory", tmp, *self._env()])
            folder = Path(tmp)
            definition = json.loads((folder / "apiDefinition.json").read_text(encoding="utf-8-sig"))
            props = json.loads((folder / "apiProperties.json").read_text(encoding="utf-8-sig"))
        return definition, props.get("properties", {})

    # ── writes ───────────────────────────────────────────────────────────────
    def import_solution(self, zip_path: Path) -> str:
        return self.run(["solution", "import", "--path", str(zip_path), *self._env()])

    def add_component(self, solution: str, component_id: str, component_type: int) -> str:
        return self.run(["solution", "add-solution-component", "--solutionUniqueName", solution,
                         "--component", component_id, "--componentType", str(component_type), *self._env()])
