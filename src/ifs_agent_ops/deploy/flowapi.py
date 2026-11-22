"""Cloud flows in a target environment: Dataverse (workflow rows) and the Power Automate API (trigger URLs).

pac has no command that returns a flow's definition or its trigger URL, so the adapter reads them with the
Azure CLI sign-in (``az login``; the same person who is signed in to pac). Tokens are requested per call
from ``az account get-access-token`` and never stored.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Callable, Optional

import httpx

FLOW_RESOURCE = "https://service.flow.microsoft.com"
FLOW_API = "https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments"
WORKFLOW_FIELDS = ["workflowid", "name", "clientdata", "statecode", "statuscode", "resourceid", "category"]
STATE_ON = 1


class FlowApiError(RuntimeError):
    pass


def az_token(resource: str) -> str:
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        raise FlowApiError("the Azure CLI (az) is not installed or not on PATH; install it and run az login "
                           "--allow-no-subscriptions")
    proc = subprocess.run([az, "account", "get-access-token", "--resource", resource, "--query", "accessToken",
                           "-o", "tsv"], capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise FlowApiError("could not get an Azure CLI token (run az login --allow-no-subscriptions): "
                           + (proc.stderr.strip().splitlines() or ["no output"])[-1])
    return proc.stdout.strip()


Transport = Callable[[str, str, Optional[dict]], dict]   # (method, url, json body) -> response json


class FlowApi:
    """Read workflows from Dataverse and trigger URLs from the Power Automate API."""

    def __init__(self, environment_url: str, environment_id: str, transport: Optional[Transport] = None,
                 token: Callable[[str], str] = az_token) -> None:
        self.environment_url = environment_url.rstrip("/")
        self.environment_id = environment_id
        self._transport = transport
        self._token = token

    def _call(self, method: str, url: str, resource: str, body: Optional[dict] = None) -> dict:
        if self._transport is not None:
            return self._transport(method, url, body)
        headers = {"Authorization": f"Bearer {self._token(resource)}", "Accept": "application/json",
                   "OData-MaxVersion": "4.0", "OData-Version": "4.0"}
        resp = httpx.request(method, url, headers=headers, json=body, timeout=120)
        if resp.status_code >= 400:
            raise FlowApiError(f"{method} {url.split('?')[0]} -> {resp.status_code}: {resp.text[:2000]}")
        return resp.json() if resp.content else {}

    def workflow(self, workflow_id: str) -> Optional[dict]:
        url = (f"{self.environment_url}/api/data/v9.2/workflows?$select={','.join(WORKFLOW_FIELDS)}"
               f"&$filter=workflowid eq {workflow_id}")
        rows = self._call("GET", url, self.environment_url).get("value", [])
        return rows[0] if rows else None

    def set_state(self, workflow_id: str, on: bool) -> None:
        url = f"{self.environment_url}/api/data/v9.2/workflows({workflow_id})"
        self._call("PATCH", url, self.environment_url, {"statecode": 1 if on else 0, "statuscode": 2 if on else 1})

    def callback_url(self, resource_id: str, trigger: str = "manual") -> str:
        url = (f"{FLOW_API}/{self.environment_id}/flows/{resource_id}/triggers/{trigger}/listCallbackUrl"
               "?api-version=2016-11-01")
        data = self._call("POST", url, FLOW_RESOURCE, {})
        value = data.get("response", {}).get("value") or data.get("value")
        if not value:
            raise FlowApiError(f"no callback URL for flow {resource_id}: {json.dumps(data)[:200]}")
        return value
