"""Command-line entry point. P1 only exposes the version."""

from __future__ import annotations

import argparse
import sys

from ilk2 import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ilk2")
    parser.add_argument("--version", action="version", version=f"ilk2 {__version__}")
    parser.parse_args(argv)
    parser.print_help(sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
