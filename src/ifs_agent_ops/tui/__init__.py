"""Guided review and approval TUI (``ifs-agent-ops`` with no arguments, or ``ifs-agent-ops tui``)."""
from __future__ import annotations

import sys


def interactive() -> bool:
    """A human at a terminal: both stdin and stdout are terminals (scripts, agents and pipes are not)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run() -> int:
    if not interactive():
        print("refused: the review TUI needs an interactive terminal (use the operations/capabilities "
              "commands from scripts)", file=sys.stderr)
        return 2
    from ifs_agent_ops.tui.app import ReviewApp

    ReviewApp().run()
    return 0
