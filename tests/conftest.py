import pytest


@pytest.fixture(autouse=True)
def no_real_test_log(monkeypatch):
    """A runtime built during tests must never write the test log into the real toolkit home."""
    monkeypatch.setenv("IFS_AGENT_OPS_TEST_LOG", "off")


@pytest.fixture(autouse=True)
def isolated_toolkit_home(tmp_path_factory, monkeypatch):
    """Capture sessions, targets and agent copies live in the toolkit home: never read the machine's real one."""
    monkeypatch.setenv("IFS_AGENT_OPS_HOME", str(tmp_path_factory.mktemp("toolkit-home")))


@pytest.fixture(autouse=True)
def isolated_env_file(tmp_path_factory, monkeypatch):
    """The CLI loads the toolkit's .env and `package init` lists new packages in it: never the real file."""
    from ifs_agent_ops import env

    monkeypatch.setattr(env, "REPO_ENV_FILE", tmp_path_factory.mktemp("toolkit-env") / ".env")
    monkeypatch.setattr(env, "_FROM_FILE", {})


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
