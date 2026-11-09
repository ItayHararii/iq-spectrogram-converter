"""Entry point: python -m crfs_iq_recorder"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    from .paths import prepare_runtime

    prepare_runtime()
    from .spectrogram_job import JOB_FLAG

    if args[:1] == [JOB_FLAG]:
        # Background spectrogram conversion started by the running app; no window.
        from .spectrogram_job import main as job_main

        return job_main(args)
    from .gui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
