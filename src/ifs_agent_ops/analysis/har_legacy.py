"""Compatibility entry points for the legacy HAR parser API.

``parse_har`` / ``summarise_har`` keep their original signatures and output; they are now
thin wrappers over ``recordings.har`` (evidence) and ``analysis.grouping`` (grouped view).
"""
from __future__ import annotations

from ifs_agent_ops.analysis.grouping import OperationGroup, ParsedHar, group_calls
from ifs_agent_ops.analysis.summary import summarise_har
from ifs_agent_ops.recordings.har import load_har

__all__ = ["OperationGroup", "ParsedHar", "parse_har", "summarise_har"]


def parse_har(file_path: str) -> ParsedHar:
    return group_calls(load_har(file_path))
