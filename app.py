#!/usr/bin/env python3
"""AI-Transcoder entry point.

Thin launcher around the ``wz_vod_hls`` CLI so the whole pipeline can be run with:

    python app.py --config config.json
    python app.py --input s3://bucket/key.mp4 --output my_asset

Inputs may be local paths or ``s3://`` URIs. Every flag accepted by
``wz_vod_hls`` is accepted here unchanged; run ``python app.py --help``.

To run the HTTP service instead, start ``python -m api.app``.
"""
import os
import sys

# Make sure the repository root is importable no matter where app.py is invoked from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wz_vod_hls import main  # noqa: E402


def _preflight():
    """Fail fast with a readable message when the config file is missing."""
    config_path = "config.json"
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            config_path = argv[i + 1]
            break
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            break
    if not os.path.exists(config_path):
        print(f"error: config file '{config_path}' not found.\n"
              f"Copy config.example.json to config.json and edit it, or pass "
              f"--config /path/to/your.json",
              file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    elif not _preflight():
        sys.exit(1)
    sys.exit(main())
