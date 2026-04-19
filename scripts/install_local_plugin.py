#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


PLUGIN_NAME = "repo-context-forge"


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


def ensure_plugin_link(plugin_root: Path, link_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        if link_path.resolve() == plugin_root.resolve():
            return
        raise RuntimeError(f"plugin path already exists and points elsewhere: {link_path}")
    os.symlink(plugin_root, link_path, target_is_directory=True)


def main() -> int:
    plugin_root = Path(__file__).resolve().parents[1]
    marketplace_path = Path.home() / ".agents" / "plugins" / "marketplace.json"
    link_path = Path.home() / "plugins" / PLUGIN_NAME

    ensure_plugin_link(plugin_root, link_path)
    marketplace = update_marketplace(load_marketplace(marketplace_path))
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace_path.write_text(
        json.dumps(marketplace, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"registered {PLUGIN_NAME} in {marketplace_path}")
    print(f"plugin link: {link_path} -> {plugin_root}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
