#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path


PLUGIN_NAME = "repo-context-forge"
SNAPSHOT_BASE = Path.home() / ".local" / "share" / PLUGIN_NAME
CURRENT_LINK = SNAPSHOT_BASE / "current"


def run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def ensure_snapshot(source_repo: Path, sha: str) -> Path:
    snapshot = SNAPSHOT_BASE / sha
    if not snapshot.exists():
        SNAPSHOT_BASE.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", str(source_repo), str(snapshot)],
            check=True,
        )
        run_git(snapshot, ["checkout", "--quiet", "--detach", sha])
        origin = subprocess.run(
            ["git", "-C", str(source_repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
        if origin.returncode == 0:
            run_git(snapshot, ["remote", "set-url", "origin", origin.stdout.strip()])
    head = run_git(snapshot, ["rev-parse", "HEAD"])
    if head != sha:
        raise RuntimeError(f"snapshot {snapshot} is at {head}, not requested commit {sha}")
    if run_git(snapshot, ["status", "--porcelain"]):
        raise RuntimeError(f"snapshot {snapshot} has local modifications; refusing to activate it")
    return snapshot


def require_replaceable(link: Path) -> None:
    if link.exists() and not link.is_symlink():
        raise RuntimeError(f"refusing to replace non-symlink path: {link}")


def point_symlink(link: Path, target: Path) -> None:
    if link.is_symlink() and os.readlink(link) == str(target):
        return
    require_replaceable(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    staging = link.parent / f".{link.name}.tmp"
    staging.unlink(missing_ok=True)
    os.symlink(target, staging, target_is_directory=True)
    os.replace(staging, link)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install a commit-addressed engine snapshot and activate it through the current symlink.",
    )
    parser.add_argument(
        "--commit",
        default="HEAD",
        help="commit to install, resolved in this checkout (default: HEAD)",
    )
    args = parser.parse_args()

    source_repo = Path(__file__).resolve().parents[1]
    SNAPSHOT_BASE.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_BASE / ".install.lock", "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        sha = run_git(source_repo, ["rev-parse", f"{args.commit}^{{commit}}"])
        snapshot = ensure_snapshot(source_repo, sha)
        require_replaceable(CURRENT_LINK)
        point_symlink(CURRENT_LINK, snapshot)
    print(f"installed snapshot {sha}")
    print(f"current: {CURRENT_LINK} -> {snapshot}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
