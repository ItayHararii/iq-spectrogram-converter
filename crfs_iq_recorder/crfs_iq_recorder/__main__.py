"""Entry point: python -m crfs_iq_recorder [--demo]"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crfs-iq-recorder",
        description="Local desktop client for CRFS RFeye EMP remote IQ recording.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use simulated HTTP responses. Does not contact a sensor or send recording commands.",
    )
    args = parser.parse_args(argv)
    from .gui import run

    return run(demo=args.demo)


if __name__ == "__main__":
    sys.exit(main())
