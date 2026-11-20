"""TLS verification settings for calls to IFS.

Default: verify certificates against the operating system trust store (via ``truststore``).
IFS instances commonly use certificates issued by an internal/corporate CA that is trusted
by the OS but absent from Python's bundled CA list (certifi), so OS-store verification keeps
TLS on without per-machine CA configuration.

Environment:
  IFS_TLS_VERIFY   "true" (default) or "false" to disable verification (not recommended).
  IFS_CA_BUNDLE    path to a PEM CA bundle; used instead of the OS trust store when set.
"""
from __future__ import annotations

import os
import ssl
from typing import Union

VerifyType = Union[bool, str, ssl.SSLContext]

_FALSE = {"0", "false", "no", "off"}


def os_trust_store_context() -> ssl.SSLContext:
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def verify_from_env() -> VerifyType:
    if os.environ.get("IFS_TLS_VERIFY", "true").strip().lower() in _FALSE:
        return False
    bundle = os.environ.get("IFS_CA_BUNDLE", "").strip()
    if bundle:
        return bundle
    return os_trust_store_context()
