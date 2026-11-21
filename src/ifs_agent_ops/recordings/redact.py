"""Repository-safe transformations applied on top of ``sanitize`` at package import.

``normalize_hosts`` (always at import): every host seen in request URLs is replaced
everywhere in the HAR by a stable placeholder: IFS hosts (serving projections or Keycloak)
become ``ifs.example`` (``ifs-2.example``, ...), any other host ``external-N.example``. The real
endpoint belongs to the environment profile, not to committed evidence.

``redact_values`` (optional, default for packages): business values are replaced by
pseudo-values that

- keep **identity**: the same original value always maps to the same pseudo-value, so data
  flow (response ``OrderNo`` -> later request path ``OrderNo``) still links;
- keep **shape**: digits stay digits, letters stay letters (case kept), punctuation and length
  are kept, so formats such as ``2026-05-17`` vs ``2026-05-17T10:00:00Z`` remain visible;
- keep **JSON types**: numbers stay numbers.

Kept as-is because they are structure, not business data: field names, ``@odata.*``
annotations (key values inside ``@odata.context``/``@odata.id``/``...Link`` URLs are redacted), values whose
sibling ``<field>@odata.type`` is an ``#IfsApp.*`` enum type and ``luname`` values (and those
same values wherever else they occur), ``IfsApp.*'Member'`` literals,
ETags / ``If-Match``, booleans, null, empty and one-character values, field names in
``$select``/``$expand``/``$orderby`` (literals nested in them are redacted), ``$top``/``$skip``/
``$count``/``$format``, and HAR bookkeeping fields (timestamps, timings, sizes).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import string
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from ifs_agent_ops.recordings import multipart
from ifs_agent_ops.recordings.har import decode_text_bodies

PLACEHOLDER_IFS_HOST = "ifs.example"
_IFS_PATH_RE = re.compile(r"/main/ifsapplications/|/auth/realms/", re.I)
_KEY_VALUE_RE = re.compile(r"(\w+)=(?:'((?:[^']|'')*)'|([^,)'/]+))")
# One pass over URL/OData text. Alternatives: quoted literal (not an IfsApp...'Member' enum
# literal), unquoted numeric key value (Key=42 before , or )), ISO date/datetime, comparison number.
_LITERAL_RE = re.compile(
    r"(?P<q>(?<![A-Za-z0-9_.])'(?P<qv>(?:[^']|'')*)')"
    r"|(?P<k>\b(?P<kn>\w+)=(?P<kv>-?\d+(?:\.\d+)?)(?=[,)]))"
    r"|(?P<d>\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?\b)"
    r"|(?P<c>\b(?P<op>eq|ne|gt|ge|lt|le)\s+(?!\d{4}-\d{2}-\d{2})(?P<cv>-?\d+(?:\.\d+)?)\b)"
)
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$")
_FILTER_STRING_RE = re.compile(r"(?<![A-Za-z0-9_.])'((?:[^']|'')*)'")
_FILTER_NUMBER_RE = re.compile(r"\b(eq|ne|gt|ge|lt|le)\s+(-?\d+(?:\.\d+)?)\b")
_FILTER_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?\b")
# Query options whose values are never business data.
_KEEP_QUERY = {"$top", "$skip", "$count", "$format"}
# Query options that are structural but may nest $filter literals ($expand=X($filter=...)):
# literal redaction is applied, field names are untouched.
_LITERAL_QUERY = {"$filter", "$search", "$select", "$expand", "$orderby", "$apply",
                  "select-fields"}  # IFS web client: field-name list, like $select
_HEADER_VALUE_KEEP = {"if-match", "if-none-match", "etag", "content-type", "accept", "odata-version",
                      "odata-maxversion", "prefer", "content-length", "accept-encoding", "accept-language",
                      "user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest",
                      "sec-fetch-mode", "sec-fetch-site", "cache-control", "content-encoding", "vary",
                      ":method", ":scheme", "priority", "x-requested-with", "x-ifs-accept-warnings", "dnt"}
_LINK_ANNOTATIONS = {"@odata.id", "@odata.editLink", "@odata.readLink"}
_STRUCTURAL_KEYS = {"luname"}  # IFS logical unit names
_URL_HEADERS = {":path", "referer", "location", "content-location", "odata-entityid"}  # values are URLs/paths
_KEYREF_RE = re.compile(r"^(?:[A-Z0-9_]+=[^^]*\^)+$")  # IFS keyref: COL=value^COL=value^
_LINK_SUFFIX_RE = re.compile(r"@odata\.(navigationLink|associationLink|editLink|readLink|mediaReadLink|mediaEditLink|id)$")
_MIN_SUBSTITUTION_LEN = 3


# ── host normalisation ───────────────────────────────────────────────────────

def host_map(har: dict) -> dict[str, str]:
    ifs, other = [], []
    for e in har.get("log", {}).get("entries", []) or []:
        url = (e.get("request") or {}).get("url") or ""
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            continue
        bucket = ifs if _IFS_PATH_RE.search(url) else other
        if host not in ifs and host not in other:
            bucket.append(host)
    other = [h for h in other if h not in ifs]
    mapping = {h: (PLACEHOLDER_IFS_HOST if i == 0 else f"ifs-{i + 1}.example") for i, h in enumerate(ifs)}
    mapping.update({h: f"external-{i + 1}.example" for i, h in enumerate(other)})
    return mapping


def _replace_in_strings(obj: Any, fn) -> Any:
    if isinstance(obj, dict):
        return {k: _replace_in_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_in_strings(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def normalize_hosts(har: dict) -> tuple[dict, dict[str, str]]:
    """Return (HAR with hosts replaced, {real host: placeholder}). Input is not modified."""
    har = json.loads(json.dumps(har))
    decode_text_bodies(har)  # a host inside a base64 body would survive the replacement and the leak check
    mapping = host_map(har)
    if not mapping:
        return json.loads(json.dumps(har)), mapping
    pattern = re.compile("|".join(re.escape(h) for h in sorted(mapping, key=len, reverse=True)), re.I)
    out = _replace_in_strings(har, lambda s: pattern.sub(lambda m: mapping[m.group(0).lower()], s))
    return out, mapping


# ── value redaction ──────────────────────────────────────────────────────────

class ValueRedactor:
    def __init__(self, seed: bytes, *, collect: bool = False) -> None:
        self._rng = random.Random(hashlib.sha256(seed).digest())
        self.mapping: dict[str, str] = {}
        self._used: set[str] = set()
        self._avoid: set[str] = set()
        self.collect = collect          # pass 1: record values, change nothing
        self.collected: set[str] = set()
        # Values seen as IFS enum members or LU names anywhere are structural everywhere, so a
        # request body echoing an enum value (not annotated there) keeps linking to its source.
        self.structural: set[str] = set()
        self.free_texts: list[str] = []  # collect mode: unstructured text seen (for leak checks)

    def prepare(self, originals: set[str]) -> None:
        """Pass 2 setup: assign every pseudo-value up front, none equal to any original value."""
        self._avoid = set(originals)
        for value in sorted(originals):
            self._assign(value)

    @staticmethod
    def keep(value: str) -> bool:
        return len(value) <= 1 or value.lower() in {"true", "false", "null"} or value.startswith("IfsApp.")

    def pseudo(self, value: str) -> str:
        if self.keep(value) or value in self.structural:
            return value
        if _KEYREF_RE.match(value):
            # IFS keyref (COL=value^...): keep column names, map each value like any other
            # occurrence of it, wherever the keyref appears (payload field or URL key).
            return "".join(f"{col}={self.pseudo(val)}^" for col, val in
                           (pair.split("=", 1) for pair in value.split("^") if pair))
        if self.collect:
            self.collected.add(value)
            return value
        return self.mapping[value] if value in self.mapping else self._assign(value)

    def _assign(self, value: str) -> str:
        if self.keep(value):
            return value
        candidate = value
        for _ in range(200):
            out = []
            iso = bool(_ISO_DATETIME_RE.match(value))  # keep T/Z/offset: format is evidence
            for ch in value:
                if ch.isdigit():
                    out.append(self._rng.choice(string.digits))
                elif iso:
                    out.append(ch)
                elif ch.isalpha() and ch.isascii():
                    pool = string.ascii_uppercase if ch.isupper() else string.ascii_lowercase
                    out.append(self._rng.choice(pool))
                elif ch.isalpha():
                    out.append(self._rng.choice(string.ascii_letters))
                else:
                    out.append(ch)
            candidate = "".join(out)
            if candidate[0] == "0" and value[0] != "0" and candidate[0].isdigit():
                candidate = str(self._rng.randint(1, 9)) + candidate[1:]
            if candidate != value and candidate not in self._used and candidate not in self._avoid:
                break
        self.mapping[value] = candidate
        self._used.add(candidate)
        return candidate

    # JSON payloads
    def json_value(self, obj: Any, parent: Optional[dict] = None, key: Optional[str] = None) -> Any:
        if isinstance(obj, dict):
            return {k: self.json_value(v, obj, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.json_value(v, None, key) for v in obj]
        if key is not None and parent is not None:
            if isinstance(obj, str) and (key in _LINK_ANNOTATIONS or _LINK_SUFFIX_RE.search(key)
                                         or key == "@odata.context"):
                # URLs that can carry key values (@odata.id, @odata.context, ...@odata.navigationLink)
                return self.path(obj)  # one redaction per location: key predicates only
            if key.startswith("@odata.") or key.endswith("@odata.type") or "@odata." in key:
                return obj
            type_hint = parent.get(f"{key}@odata.type")
            if (isinstance(type_hint, str) and type_hint.startswith("#IfsApp.")) or key.lower() in _STRUCTURAL_KEYS:
                if self.collect and isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
                    self.structural.add(str(obj))
                return obj  # enum member / LU name: structural
        if obj is None or isinstance(obj, bool):
            return obj
        if isinstance(obj, (int, float)):
            text = repr(obj) if isinstance(obj, float) else str(obj)
            new = self.pseudo(text)
            try:
                return type(obj)(new)
            except ValueError:
                return obj
        if isinstance(obj, str):
            return self.pseudo(obj)
        return obj

    def json_text(self, text: Optional[str]) -> Optional[str]:
        """Redact a JSON document; a bare scalar is redacted as a value, non-JSON as free text."""
        if not text:
            return text
        try:
            data = json.loads(text)
        except Exception:
            return self.free_text(text)
        if isinstance(data, (dict, list)):
            return json.dumps(self.json_value(data), ensure_ascii=False, separators=(",", ":"))
        if data is None or isinstance(data, bool):
            return text
        return json.dumps(self.json_value(data), ensure_ascii=False)

    # URLs
    def path(self, path: str) -> str:
        return self.literals(path)

    def literals(self, text: str) -> str:
        """Redact every literal in URL/OData text in a single pass (never twice)."""
        def repl(m: re.Match) -> str:
            if m.group("q") is not None:
                return f"'{self.pseudo(m.group('qv'))}'"
            if m.group("k") is not None:
                return f"{m.group('kn')}={self.pseudo(m.group('kv'))}"
            if m.group("d") is not None:
                return self.pseudo(m.group("d"))
            return f"{m.group('op')} {self.pseudo(m.group('cv'))}"
        return _LITERAL_RE.sub(repl, text)

    filter_expr = literals

    def url(self, url: str) -> str:
        parts = urlsplit(url)
        path = quote(self.path(unquote(parts.path)), safe="/()=',@$:!*+;-._~")
        pairs = []
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            if k in _KEEP_QUERY:
                pairs.append((k, v))
            elif k in _LITERAL_QUERY:
                pairs.append((k, self.filter_expr(v)))
            else:
                pairs.append((k, self.pseudo(v)))
        query = urlencode(pairs, quote_via=quote, safe="$',()=*@:/") if pairs else ""
        fragment = quote(self.literals(unquote(parts.fragment)), safe="/()=',@$:!*+;-._~?&") if parts.fragment else ""
        return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))

    # Free text (trace events, headers, page titles) may embed values inline.
    def free_text(self, text: str) -> str:
        if not hasattr(self, "_pattern"):
            originals = [v for v in self.mapping if len(v) >= _MIN_SUBSTITUTION_LEN]
            self._pattern = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(
                re.escape(v) for v in sorted(originals, key=len, reverse=True)) + r")(?![A-Za-z0-9])") if originals else None
        if self.collect:
            self.free_texts.append(text)
            return text
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda m: self.mapping[m.group(1)], text)

    def free_text_tree(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self.free_text_tree(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.free_text_tree(v) for v in obj]
        return self.free_text(obj) if isinstance(obj, str) else obj


def _redact_headers(r: ValueRedactor, msg) -> None:
    for i, (name, value) in enumerate(msg.headers):
        lname = name.lower()
        if lname in _URL_HEADERS:
            msg.set_header_value(i, r.url(value))
        elif lname not in _HEADER_VALUE_KEEP and lname not in ("content-type", "content-transfer-encoding",
                                                                 "content-id", "content-length"):
            msg.set_header_value(i, r.free_text(value))


def _redact_multipart(r: ValueRedactor, text: str) -> str:
    """$batch bodies: protocol structure is kept verbatim; embedded request URLs, headers and
    JSON bodies are redacted with the same rules as top-level calls."""
    mp = multipart.parse(text)
    if mp is None:
        return r.free_text(text)

    def one(msg):
        if msg.is_request and msg.url:
            msg.set_url(r.url(msg.url))
        _redact_headers(r, msg)
        msg.body = _redact_body(r, msg.body) or msg.body
        return msg

    multipart.map_http_parts(mp, one)
    return multipart.render(mp)


def _redact_body(r: ValueRedactor, text: Optional[str]) -> Optional[str]:
    """Redact a body. IFS server-trace envelopes: the payload is redacted structurally and the
    rest of the envelope (trace events) as free text. $batch multipart bodies are parsed."""
    if not text:
        return text
    if multipart.is_multipart(text):
        return _redact_multipart(r, text)
    try:
        data = json.loads(text)
    except Exception:
        return r.free_text(text)
    if isinstance(data, dict) and "ifs-trace" in data and isinstance(data.get("response"), dict):
        resp = data["response"]
        body = resp.get("body")
        uri = (data.get("request") or {}).get("uri")
        headers = resp.get("headers")
        if isinstance(headers, dict):
            headers = {k: (r.url(v) if isinstance(v, str) and k.lower() in _URL_HEADERS
                           else v if not isinstance(v, str) or k.lower() in _HEADER_VALUE_KEEP
                           else r.free_text(v)) for k, v in headers.items()}
        data = r.free_text_tree({k: v for k, v in data.items() if k not in ("response", "request")}) | {
            "request": {**(data.get("request") or {}), **({"uri": r.url(uri)} if isinstance(uri, str) else {})},
            "response": {**resp, **({"body": _redact_body(r, body)} if isinstance(body, str) else {}),
                         **({"headers": headers} if isinstance(headers, dict) else {})},
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return r.json_text(text)


def _apply(r: ValueRedactor, har: dict) -> dict:
    out = json.loads(json.dumps(har))
    decode_text_bodies(out)  # base64 text bodies (e.g. $batch responses from the extension) hold values too
    log = out.get("log", {})
    for page in log.get("pages", []) or []:
        if isinstance(page.get("title"), str):
            page["title"] = r.free_text(page["title"])
    for e in log.get("entries", []) or []:
        req, resp = e.get("request") or {}, e.get("response") or {}
        content = resp.get("content") or {}
        if content.get("encoding") != "base64" and isinstance(content.get("text"), str):
            content["text"] = _redact_body(r, content["text"])
        if isinstance(req.get("url"), str):
            req["url"] = r.url(req["url"])
        for q in req.get("queryString") or []:
            name = q.get("name", "")
            if name in _LITERAL_QUERY:
                q["value"] = r.filter_expr(q.get("value", ""))
            elif name not in _KEEP_QUERY:
                q["value"] = r.pseudo(q.get("value", ""))
        post = req.get("postData") or {}
        if isinstance(post.get("text"), str):
            post["text"] = _redact_body(r, post["text"])
        for p in post.get("params") or []:
            p["value"] = r.pseudo(p.get("value", ""))
        for headers in (req.get("headers"), resp.get("headers")):
            for h in headers or []:
                if not (isinstance(h, dict) and isinstance(h.get("value"), str)):
                    continue
                hname = str(h.get("name", "")).lower()
                if hname in _URL_HEADERS:
                    h["value"] = r.url(h["value"])
                elif hname not in _HEADER_VALUE_KEEP:
                    h["value"] = r.free_text(h["value"])
        if isinstance(resp.get("redirectURL"), str) and resp["redirectURL"]:
            resp["redirectURL"] = r.url(resp["redirectURL"])
    return out


def business_values(har: dict) -> tuple[set[str], list[str]]:
    """(values in business-value positions, unstructured texts) of a HAR, using the same rules
    as redaction. Used to check a redacted recording for surviving originals."""
    detector = ValueRedactor(b"", collect=True)
    _apply(detector, har)
    return detector.collected, detector.free_texts


def redact_values(har: dict, seed: bytes) -> tuple[dict, dict[str, str]]:
    """Return (HAR with business values redacted, {original: pseudo} for the values replaced).

    Pass 1 collects every value that will be replaced; pass 2 assigns all pseudo-values up
    front (none equal to any original) and applies them, so identity holds across the file.
    """
    collector = ValueRedactor(seed, collect=True)
    _apply(collector, har)
    r = ValueRedactor(seed)
    r.structural = collector.structural
    r.prepare(collector.collected - collector.structural)
    return _apply(r, har), dict(r.mapping)
