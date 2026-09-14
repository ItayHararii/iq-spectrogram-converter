"""Entry point: python -m crfs_iq_recorder"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    del argv
    from .paths import prepare_runtime

    prepare_runtime()
    from .gui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
