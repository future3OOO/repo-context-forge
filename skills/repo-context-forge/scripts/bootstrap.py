#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    plugin_root = Path(__file__).resolve().parents[3]
    bootstrap = plugin_root / "scripts" / "codex_context_bootstrap.py"
    args = list(argv)
    if "--enforce-intake" not in args:
        args.append("--enforce-intake")
    return subprocess.call([sys.executable, str(bootstrap), *args])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
