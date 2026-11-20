"""Opt-in parity check on real (uncommitted) HAR recordings.

Runs the legacy parser and the toolkit parser on the same files in-process and compares
results; nothing from the recordings is written anywhere. Skipped unless both are set:

    IFS_LEGACY_SRC  path to the legacy repo's ``src`` folder (contains ``ifs_mcp_server``)
    IFS_REAL_HAR_DIR  folder searched recursively for ``*.har``
"""
import dataclasses
import importlib
import os
import sys
from pathlib import Path

import pytest

LEGACY_SRC = os.environ.get("IFS_LEGACY_SRC")
HAR_DIR = os.environ.get("IFS_REAL_HAR_DIR")
HARS = sorted(Path(HAR_DIR).rglob("*.har")) if HAR_DIR else []

pytestmark = pytest.mark.skipif(not (LEGACY_SRC and HARS), reason="set IFS_LEGACY_SRC and IFS_REAL_HAR_DIR")


@pytest.fixture(scope="module")
def legacy_parser():
    sys.path.insert(0, LEGACY_SRC)
    try:
        return importlib.import_module("ifs_mcp_server.lib.har_parser")
    finally:
        sys.path.remove(LEGACY_SRC)


@pytest.mark.parametrize("har", HARS, ids=lambda p: p.name)
def test_real_har_matches_legacy(legacy_parser, har):
    from ifs_agent_ops.analysis.har_legacy import parse_har, summarise_har

    old = legacy_parser.parse_har(str(har))
    new = parse_har(str(har))
    assert dataclasses.asdict(new) == dataclasses.asdict(old)
    assert summarise_har(new) == legacy_parser.summarise_har(old)
