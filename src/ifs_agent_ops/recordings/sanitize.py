"""Remove credentials and bulk noise from a HAR before it is kept as evidence.

Removes:
- ``_initiator`` (DevTools call stacks; often most of a HAR's size, no API evidence)
- values of credential headers (``Authorization``, ``Cookie``, ``Set-Cookie``,
  ``X-XSRF-TOKEN`` and any header whose name looks like a token/secret/session/auth header)
- cookie values in ``request.cookies`` / ``response.cookies``
- OAuth/secret parameters in URLs, query strings and form bodies
  (``access_token``, ``id_token``, ``refresh_token``, ``code``, ``state``, ``session_state``,
  ``client_secret``, ``password``)
- the same keys inside JSON request/response bodies
- anything shaped like a JWT, in any string
- the entry's ``serverIPAddress``
- in IFS server-trace envelopes (``ifs-trace``): ``server.environment`` values (user, hosts,
  addresses, paths; ``authType`` kept), SQL bind values, and ``textWithBinds`` (SQL with values
  inlined). The trace events themselves and parameterised SQL text are kept as evidence.

Keeps everything else, including business data values. Header *names* are kept with a
redacted value so the evidence still shows which credentials a call carried.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from typing import Any

from ifs_agent_ops.recordings.har import decode_text_bodies

REDACTED = "<redacted>"
BINARY_REMOVED = "<binary body removed>"

_CREDENTIAL_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie",
                       "x-xsrf-token", "x-csrf-token"}
_CREDENTIAL_HEADER_RE = re.compile(r"(?i)(token|secret|session|api-?key|auth)")
# OAuth/secret parameter names in URLs, query strings and form bodies.
_SECRET_KEYS = {"access_token", "id_token", "refresh_token", "code", "state", "session_state",
                "client_secret", "password"}
# JSON body keys treated as secrets. Narrower than _SECRET_KEYS: IFS entities have ordinary
# business fields named Code/State, which must survive as evidence.
_SECRET_JSON_KEYS = {"access_token", "id_token", "refresh_token", "client_secret", "password"}
# IFS server-trace envelope: infrastructure details kept as keys with redacted values.
_TRACE_ENV_KEEP = {"authType"}
_URL_PARAM_RE = re.compile(
    r"(?i)([?&#;](?:" + "|".join(sorted(_SECRET_KEYS)) + r")=)[^&#\s\"']+")
_FORM_PARAM_RE = re.compile(
    r"(?i)((?:^|&)(?:" + "|".join(sorted(_SECRET_KEYS)) + r")=)[^&]*")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
# Header names that match _CREDENTIAL_HEADER_RE but carry no secret.
_HEADER_ALLOW = {"x-ifs-request-id", ":authority"}


def _is_credential_header(name: str) -> bool:
    n = name.lower()
    return n in _CREDENTIAL_HEADERS or (n not in _HEADER_ALLOW and bool(_CREDENTIAL_HEADER_RE.search(n)))


class _Sanitizer:
    def __init__(self) -> None:
        self.report: Counter = Counter()

    def text(self, s: str) -> str:
        s2, n = _JWT_RE.subn("<redacted-jwt>", s)
        self.report["jwt"] += n
        s3, n = _URL_PARAM_RE.subn(lambda m: m.group(1) + REDACTED, s2)
        self.report["url_param"] += n
        return s3

    def json_keys(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() in _SECRET_JSON_KEYS and v not in (None, ""):
                    out[k] = REDACTED
                    self.report["body_key"] += 1
                else:
                    out[k] = self.json_keys(v)
            return out
        if isinstance(obj, list):
            return [self.json_keys(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj

    def trace_envelope(self, env: dict) -> None:
        """Redact infrastructure details and SQL bind values from an IFS server-trace envelope."""
        environment = (env.get("server") or {}).get("environment")
        if isinstance(environment, dict):
            for k, v in environment.items():
                if k not in _TRACE_ENV_KEEP and v not in (None, ""):
                    environment[k] = REDACTED
                    self.report["trace_environment"] += 1
        for ev in (env.get("ifs-trace") or {}).get("trace") or []:
            if not isinstance(ev, dict):
                continue
            if ev.pop("textWithBinds", None) is not None:
                self.report["trace_sql_with_binds"] += 1
            binds = ev.get("bindParams")
            if isinstance(binds, dict):
                for k in binds:
                    binds[k] = REDACTED
                self.report["trace_bind_values"] += len(binds)
        resp = env.get("response") or {}
        if isinstance(resp.get("body"), str):
            resp["body"] = self.body(resp["body"], "application/json")

    def body(self, text: str, mime: str | None) -> str:
        if not text:
            return text
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            before = self.report.copy()
            if isinstance(parsed, dict) and "ifs-trace" in parsed and isinstance(parsed.get("response"), dict):
                self.trace_envelope(parsed)
            cleaned = self.json_keys(parsed)
            if self.report != before:
                return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
            return text
        if mime and "x-www-form-urlencoded" in mime:
            text, n = _FORM_PARAM_RE.subn(lambda m: m.group(1) + REDACTED, text)
            self.report["form_param"] += n
        return self.text(text)

    def headers(self, headers: list) -> None:
        for h in headers or []:
            if isinstance(h, dict) and _is_credential_header(str(h.get("name", ""))):
                if h.get("value"):
                    h["value"] = REDACTED
                    self.report["header"] += 1
            elif isinstance(h, dict) and isinstance(h.get("value"), str):
                h["value"] = self.text(h["value"])

    def cookies(self, cookies: list) -> None:
        for c in cookies or []:
            if isinstance(c, dict) and c.get("value"):
                c["value"] = REDACTED
                self.report["cookie"] += 1

    def params(self, params: list) -> None:
        for p in params or []:
            if isinstance(p, dict) and str(p.get("name", "")).lower() in _SECRET_KEYS and p.get("value"):
                p["value"] = REDACTED
                self.report["param"] += 1

    def entry(self, e: dict) -> None:
        if e.pop("_initiator", None) is not None:
            self.report["_initiator"] += 1
        req, resp = e.get("request") or {}, e.get("response") or {}
        if isinstance(req.get("url"), str):
            req["url"] = self.text(req["url"])
        self.headers(req.get("headers"))
        self.headers(resp.get("headers"))
        self.cookies(req.get("cookies"))
        self.cookies(resp.get("cookies"))
        self.params(req.get("queryString"))
        post = req.get("postData") or {}
        self.params(post.get("params"))
        if isinstance(post.get("text"), str):
            post["text"] = self.body(post["text"], post.get("mimeType"))
        content = resp.get("content") or {}
        if isinstance(content.get("text"), str):
            if content.get("encoding") == "base64":
                # Still base64 after decode_text_bodies: binary (images, files). It cannot be inspected for
                # secrets or business values and is no evidence of IFS behaviour, so it is not kept.
                del content["encoding"]
                content["text"] = BINARY_REMOVED
                self.report["binary_body_removed"] += 1
            else:
                content["text"] = self.body(content["text"], content.get("mimeType"))
        if isinstance(resp.get("redirectURL"), str):
            resp["redirectURL"] = self.text(resp["redirectURL"])
        if e.get("serverIPAddress"):
            e["serverIPAddress"] = REDACTED
            self.report["server_ip"] += 1


def sanitize_har(har: dict) -> tuple[dict, dict[str, int]]:
    """Return (sanitized copy, counts of what was removed). The input is not modified."""
    out = copy.deepcopy(har)
    decode_text_bodies(out)  # base64 text bodies are inspected like any other; binary ones are reported below
    s = _Sanitizer()
    for page in out.get("log", {}).get("pages", []) or []:
        if isinstance(page.get("title"), str):
            page["title"] = s.text(page["title"])
    for entry in out.get("log", {}).get("entries", []) or []:
        s.entry(entry)
    return out, {k: v for k, v in s.report.items() if v}
