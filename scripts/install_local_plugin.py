#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


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


def load_marketplace(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "name": "local-codex-plugins",
            "interface": {"displayName": "Local Codex Plugins"},
            "plugins": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def plugin_entry() -> dict[str, Any]:
    return {
        "name": PLUGIN_NAME,
        "source": {
            "source": "local",
            "path": f"./plugins/{PLUGIN_NAME}",
        },
        "policy": {
            "installation": "INSTALLED_BY_DEFAULT",
            "authentication": "ON_USE",
        },
        "category": "Coding",
    }


def update_marketplace(marketplace: dict[str, Any]) -> dict[str, Any]:
    plugins = marketplace.setdefault("plugins", [])
    if not isinstance(plugins, list):
        raise RuntimeError("marketplace plugins field must be a list")
    entry = plugin_entry()
    for index, existing in enumerate(plugins):
        if isinstance(existing, dict) and existing.get("name") == PLUGIN_NAME:
            plugins[index] = entry
            return marketplace
    plugins.append(entry)
    return marketplace


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


def point_symlink(link: Path, target: Path) -> None:
    if link.is_symlink() and os.readlink(link) == str(target):
        return
    if link.exists() and not link.is_symlink():
        raise RuntimeError(f"refusing to replace non-symlink path: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    staging = link.parent / f".{link.name}.tmp"
    staging.unlink(missing_ok=True)
    os.symlink(target, staging, target_is_directory=True)
    os.replace(staging, link)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install a commit-addressed runtime snapshot and activate it through the current symlink.",
    )
    parser.add_argument(
        "--commit",
        default="HEAD",
        help="commit to install, resolved in this checkout (default: HEAD)",
    )
    args = parser.parse_args()

    source_repo = Path(__file__).resolve().parents[1]
    sha = run_git(source_repo, ["rev-parse", f"{args.commit}^{{commit}}"])
    snapshot = ensure_snapshot(source_repo, sha)
    point_symlink(CURRENT_LINK, snapshot)
    link_path = Path.home() / "plugins" / PLUGIN_NAME
    point_symlink(link_path, CURRENT_LINK)

    marketplace_path = Path.home() / ".agents" / "plugins" / "marketplace.json"
    marketplace = update_marketplace(load_marketplace(marketplace_path))
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace_path.write_text(
        json.dumps(marketplace, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"installed snapshot {sha}")
    print(f"current: {CURRENT_LINK} -> {snapshot}")
    print(f"plugin link: {link_path} -> {CURRENT_LINK}")
    print(f"registered {PLUGIN_NAME} in {marketplace_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
