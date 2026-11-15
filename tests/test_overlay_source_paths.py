"""The overlay copies what differs from HEAD, and nothing else.

The analysis checkout is reset to HEAD immediately before the overlay, so every
tracked file already holds its committed content. Copying all of them produced
an identical checkout at 400x the work on a real repository: 1316 copies where
3 files differed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import repo_context_forge


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    for name in ("a.py", "b.py", "pkg/c.py"):
        (repo / name).write_text("committed\n", encoding="utf-8")
    git(repo, "init", "-q", ".")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def test_a_clean_worktree_overlays_nothing(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    assert repo_context_forge.overlay_source_paths(repo) == []
    # every tracked file is still reported by the full listing, which other
    # callers rely on; only the overlay's own set narrows.
    assert len(repo_context_forge.source_worktree_files(repo)) == 3


def test_only_what_differs_from_head_is_overlaid(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    (repo / "a.py").write_text("modified\n", encoding="utf-8")
    (repo / "untracked.py").write_text("new\n", encoding="utf-8")
    git(repo, "add", "pkg/c.py")
    (repo / "pkg" / "c.py").write_text("staged then changed\n", encoding="utf-8")

    assert sorted(repo_context_forge.overlay_source_paths(repo)) == [
        "a.py", "pkg/c.py", "untracked.py",
    ]


def test_a_rename_contributes_a_path_that_exists(tmp_path: Path) -> None:
    """Porcelain reports a rename as `R old -> new`, which is not a path; reading
    it from diff --name-only keeps both halves usable."""
    repo = build_repo(tmp_path)
    git(repo, "mv", "b.py", "renamed.py")

    paths = repo_context_forge.overlay_source_paths(repo)
    assert "renamed.py" in paths
    assert all(" -> " not in path for path in paths)
    assert (repo / "renamed.py").exists()


def test_the_overlaid_checkout_matches_a_full_copy(tmp_path: Path) -> None:
    """The narrowed set must leave the analysis checkout byte-identical to what
    copying every tracked file produced, which is the whole contract."""
    repo = build_repo(tmp_path)
    (repo / "a.py").write_text("modified\n", encoding="utf-8")
    (repo / "untracked.py").write_text("new\n", encoding="utf-8")
    git(repo, "rm", "-q", "b.py")

    checkouts = []
    for name, paths in (("narrow", repo_context_forge.overlay_source_paths(repo)),
                        ("full", repo_context_forge.source_worktree_files(repo))):
        checkout = tmp_path / name
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(repo), str(checkout)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["rm", "-rf", str(checkout / ".git")], check=True)
        for path in repo_context_forge.locally_deleted_files(repo):
            target = checkout / path
            if target.exists():
                target.unlink()
        for path in paths:
            source, target = repo / path, checkout / path
            if not source.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        checkouts.append(checkout)

    diff = subprocess.run(["diff", "-rq", str(checkouts[0]), str(checkouts[1])],
                          capture_output=True, text=True)
    assert diff.stdout == "", diff.stdout
