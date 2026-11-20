"""Record expected outputs by running the LEGACY ifs_mcp_server package against test fixtures.

Usage:
    python scripts/capture_legacy_snapshots.py <path-to-legacy-repo>/src

Writes tests/snapshots/*. Run once per deliberate baseline change; the test suite then
asserts that ifs_agent_ops reproduces these outputs exactly.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    legacy_src = Path(sys.argv[1]).resolve()
    if not (legacy_src / "ifs_mcp_server").is_dir():
        print(f"ifs_mcp_server package not found under {legacy_src}")
        return 2
    sys.path.insert(0, str(legacy_src))
    sys.path.insert(0, str(ROOT / "tests"))
    import snapshot_cases as cases

    cases.SNAPSHOTS.mkdir(exist_ok=True)
    for case in cases.ALL_CASES:
        for name, content in case("legacy").items():
            (cases.SNAPSHOTS / name).write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
