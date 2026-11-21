import pytest


@pytest.fixture(autouse=True)
def fixed_reviewer(monkeypatch):
    """Approvals record git user.name (or the OS login); pin it so tests do not depend on the machine."""
    from ifs_agent_ops.operations import approval

    monkeypatch.setattr(approval, "reviewer_name", lambda root=None: "Pat Reviewer")
    try:
        from ifs_agent_ops.tui import app as tui_app
    except ImportError:  # textual not installed
        return
    monkeypatch.setattr(tui_app, "reviewer_name", lambda root=None: "Pat Reviewer")
