"""ifs-agent-ops command line interface (placeholder until Phase 5+ commands land)."""
import argparse
from importlib.metadata import PackageNotFoundError, version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ifs-agent-ops")
    parser.add_argument("--version", action="store_true", help="print the toolkit version")
    args = parser.parse_args(argv)
    if args.version:
        try:
            print(version("ifs-agent-ops-toolkit"))
        except PackageNotFoundError:
            print("unknown")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
