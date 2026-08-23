#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import hashlib
import html
import inspect
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import urlsplit

import gitnexus_analysis
import workflow_index

Mode = Literal["pr", "local", "intent", "repo"]
Scope = Literal["pr", "dirty", "all"]
OutputFormat = Literal["markdown", "json", "prompt"]
MapBuildMode = Literal["auto", "always", "never"]
GitNexusMode = Literal["off", "check", "auto"]


DEFAULT_CACHE_DIR = Path.home() / ".cache" / "repo-context-forge"
TOOL_CACHE_DIRS = (".soulforge", ".codex", ".gitnexus", workflow_index.INDEX_DIR)
TOOL_CACHE_PATHS = (*TOOL_CACHE_DIRS, ".claude/skills/gitnexus")
TOOL_CACHE_PREFIXES = tuple(f"{name}/" for name in TOOL_CACHE_PATHS)
MIN_TOKEN_BUDGET = 16_000
MAX_TOKEN_BUDGET = 32_000
DEFAULT_TOKEN_BUDGET = MIN_TOKEN_BUDGET
MAX_GITNEXUS_CHECKS = 20
INTENT_GRAPH_EXCLUDED_NAMES = {"Cargo.lock", "go.sum"}
TaskEvent = Literal["read", "search", "edit", "mention"]
CRITICAL_AREA_STEPS = (
    "State the task or PR contract from the user request, PR title/body when available, and packet targets.",
    "Inspect changed files and top packet targets before narrowing to one symbol, GitNexus check, or review thread.",
    "Map changed behavior and contracts to verification and no-change surfaces; include production, config, API, persistence, integration, and operator surfaces when present.",
    "State any skipped changed or high-ranked target with the reason it is not relevant.",
    "Cover required surface-impact areas in the parent session unless the current user turn explicitly asks for delegated agents.",
    "Only after critical-area coverage, run packet-scoped GitNexus checks and use review comments as supplemental evidence.",
)


@dataclass(frozen=True)
class GitState:
    branch: str
    head: str
    base_ref: str
    merge_base: str | None
    pr_files: list[str]
    staged_files: list[str]
    unstaged_files: list[str]
    untracked_files: list[str]


@dataclass(frozen=True)
class TargetState:
    mode: Mode
    source_repo: Path
    analysis_repo: Path
    base_ref: str
    head_ref: str
    head_sha: str
    source_dirty: bool
    target_dirty: bool
    cache_key: str | None


MapFile = workflow_index.IndexedFile


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line: int
    end_line: int
    signature: str | None
    is_exported: bool
    summary: str | None = None
    summary_source: str | None = None


@dataclass(frozen=True)
class MapBuildResult:
    attempted: bool
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    warning: str | None


def run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    allow_fail: bool = False,
    env: dict[str, str] | None = None,
    suppress_core_dump: bool = False,
    capture_output_to_file: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    command = args
    if suppress_core_dump:
        command = [
            "/bin/sh",
            "-c",
            'printf 0 > /proc/self/coredump_filter && exec "$@"',
            "sh",
            *args,
        ]
    stdout_file = (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        if capture_output_to_file
        else None
    )
    stderr_file = (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        if capture_output_to_file
        else None
    )
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=stdout_file or subprocess.PIPE,
            stderr=stderr_file or subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
        if stdout_file and stderr_file:
            stdout_file.seek(0)
            stderr_file.seek(0)
            proc = subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                stdout_file.read(),
                stderr_file.read(),
            )
        else:
            proc = completed
    finally:
        for output_file in (stdout_file, stderr_file):
            if output_file:
                output_file.close()
    if proc.returncode != 0 and not allow_fail:
        location = f" in {cwd}" if cwd else ""
        raise RuntimeError(
            f"{' '.join(args)} failed{location}:\n{proc.stderr.strip()}"
        )
    return proc


def run_git(repo: Path, args: list[str], *, allow_fail: bool = False) -> str:
    return run_cmd(["git", *args], cwd=repo, allow_fail=allow_fail).stdout.strip()


def split_lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def unique_sorted(paths: Iterable[str]) -> list[str]:
    return sorted({path for path in paths if path})


def unique_ordered(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def is_git_repo(path: Path) -> bool:
    proc = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, allow_fail=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_root(path: Path) -> Path:
    root = run_git(path, ["rev-parse", "--show-toplevel"])
    return Path(root).resolve()


def dirty_paths(repo: Path) -> list[str]:
    return parse_porcelain_paths(porcelain_status(repo))


def porcelain_status(repo: Path) -> str:
    status = run_cmd(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        allow_fail=True,
    ).stdout
    return status.rstrip("\n")


def parse_porcelain_paths(status: str) -> list[str]:
    paths = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        paths.append(line[3:])
    return paths


def is_detached(repo: Path) -> bool:
    return run_git(repo, ["branch", "--show-current"], allow_fail=True) == ""


def worktree_suggestions(repo: Path) -> list[dict[str, str]]:
    raw = run_git(repo, ["worktree", "list", "--porcelain"], allow_fail=True)
    suggestions: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line:
            if current:
                suggestions.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "HEAD":
            current["head"] = value
        elif key == "detached":
            current["detached"] = "true"
    if current:
        suggestions.append(current)
    return suggestions


def filter_tool_cache_dirty_paths(paths: Iterable[str]) -> list[str]:
    return [
        path
        for path in paths
        if path not in TOOL_CACHE_DIRS
        and not path.startswith(TOOL_CACHE_PREFIXES)
    ]


def tool_cache_dirty_paths(paths: Iterable[str]) -> list[str]:
    return [
        path
        for path in paths
        if path in TOOL_CACHE_PATHS
        or path.startswith(TOOL_CACHE_PREFIXES)
    ]


def source_status_proof(before: str, after: str) -> dict[str, object]:
    before_lines = set(before.splitlines())
    after_lines = set(after.splitlines())
    added_lines = sorted(after_lines - before_lines)
    removed_lines = sorted(before_lines - after_lines)
    before_paths = parse_porcelain_paths(before)
    after_paths = parse_porcelain_paths(after)
    added_paths = parse_porcelain_paths("\n".join(added_lines))
    return {
        "unchanged": before == after,
        "before_hash": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "after_hash": hashlib.sha256(after.encode("utf-8")).hexdigest(),
        "before_count": len(before_lines),
        "after_count": len(after_lines),
        "added_paths": added_paths,
        "removed_paths": parse_porcelain_paths("\n".join(removed_lines)),
        "tool_cache_paths_before": tool_cache_dirty_paths(before_paths),
        "tool_cache_paths_after": tool_cache_dirty_paths(after_paths),
        "tool_cache_paths_created": tool_cache_dirty_paths(added_paths),
    }


def is_generated_or_cache_path(path: str) -> bool:
    parts = path.split("/")
    if any(part in TOOL_CACHE_DIRS for part in parts) or "__pycache__" in parts:
        return True
    return (
        path.endswith((".pyc", ".pyo"))
        or path.startswith((".git/", *TOOL_CACHE_PREFIXES))
    )


def reference_only_prefixes(repo: Path) -> list[str]:
    agents_path = repo / "AGENTS.md"
    if not agents_path.exists():
        return []
    content = agents_path.read_text(encoding="utf-8", errors="replace")
    prefixes = []
    for match in re.finditer(r"`([^`]+/)`\s+is\s+reference[- ]only\b", content, re.IGNORECASE):
        prefix = match.group(1).lstrip("/")
        if not is_generated_or_cache_path(prefix):
            prefixes.append(prefix)
    return unique_ordered(prefixes)


def is_reference_only_path(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes)


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = Path(path).name.lower()
    return (
        "test" in parts
        or "tests" in parts
        or name.startswith("test_")
        or name.endswith(
            (
                ".test.js", ".test.jsx", ".test.mjs", ".test.ts", ".test.tsx",
                ".spec.js", ".spec.jsx", ".spec.mjs", ".spec.ts", ".spec.tsx",
            )
        )
    )


def file_role(path: str) -> str:
    if is_generated_or_cache_path(path):
        return "generated"
    if is_test_path(path):
        return "test"
    return "production"


def cleanup_soulforge_gitignore_change(repo: Path) -> None:
    paths = dirty_paths(repo)
    if ".gitignore" not in paths:
        return
    diff = run_git(repo, ["diff", "--", ".gitignore"], allow_fail=True)
    changed_lines = [
        line
        for line in diff.splitlines()
        if line.startswith("+") or line.startswith("-")
    ]
    content_changes = [
        line
        for line in changed_lines
        if not line.startswith("+++") and not line.startswith("---")
    ]
    tool_cache_lines = {
        f"+{name}"
        for directory in TOOL_CACHE_DIRS
        for name in (directory, f"{directory}/")
    }
    if content_changes and all(line in tool_cache_lines for line in content_changes):
        run_git(repo, ["checkout", "--", ".gitignore"])


def is_dirty(repo: Path, *, ignore_tool_cache: bool = False) -> bool:
    paths = dirty_paths(repo)
    if ignore_tool_cache:
        paths = filter_tool_cache_dirty_paths(paths)
    return bool(paths)


def read_git_state(repo: Path, base_ref: str, head_ref: str = "HEAD") -> GitState:
    branch = run_git(repo, ["branch", "--show-current"], allow_fail=True) or "(detached)"
    head = run_git(repo, ["rev-parse", "--short", head_ref])
    merge_base = run_git(repo, ["merge-base", base_ref, head_ref], allow_fail=True) or None

    pr_files = split_lines(
        run_git(repo, ["diff", "--name-only", f"{base_ref}...{head_ref}"], allow_fail=True)
    )
    staged_files = split_lines(
        run_git(repo, ["diff", "--name-only", "--cached"], allow_fail=True)
    )
    unstaged_files = split_lines(run_git(repo, ["diff", "--name-only"], allow_fail=True))
    untracked_files = split_lines(
        run_git(repo, ["ls-files", "--others", "--exclude-standard"], allow_fail=True)
    )

    return GitState(
        branch=branch,
        head=head,
        base_ref=base_ref,
        merge_base=merge_base,
        pr_files=unique_sorted(pr_files),
        staged_files=unique_sorted(staged_files),
        unstaged_files=unique_sorted(unstaged_files),
        untracked_files=unique_sorted(untracked_files),
    )


def selected_files(git_state: GitState, scope: Scope) -> list[str]:
    if scope == "pr":
        return git_state.pr_files
    if scope == "dirty":
        return unique_sorted(
            [*git_state.staged_files, *git_state.unstaged_files, *git_state.untracked_files]
        )
    return unique_sorted(
        [
            *git_state.pr_files,
            *git_state.staged_files,
            *git_state.unstaged_files,
            *git_state.untracked_files,
        ]
    )


def diff_ranges_for_file(
    repo: Path,
    base_ref: str,
    scope: Scope,
    path: str,
    head_ref: str = "HEAD",
) -> list[tuple[int, int]]:
    if scope == "dirty":
        diff_args = ["diff", "--unified=0", "--", path]
    else:
        diff_args = ["diff", "--unified=0", f"{base_ref}...{head_ref}", "--", path]
    diff = run_git(repo, diff_args, allow_fail=True)
    return parse_unified_diff_new_ranges(diff)


def parse_unified_diff_new_ranges(diff: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for line in diff.splitlines():
        if not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        end = start + max(count, 1) - 1
        ranges.append((start, end))
    return ranges


def symbol_overlaps(symbol: Symbol, ranges: list[tuple[int, int]]) -> bool:
    return any(symbol.line <= end and symbol.end_line >= start for start, end in ranges)


def remove_nested_symbols(symbols: list[Symbol]) -> list[Symbol]:
    kept: list[Symbol] = []
    for symbol in sorted(symbols, key=lambda item: (item.line, -(item.end_line - item.line))):
        contained = False
        for other in symbols:
            if other is symbol:
                continue
            if other.line <= symbol.line and other.end_line >= symbol.end_line:
                if (other.end_line - other.line) > (symbol.end_line - symbol.line):
                    contained = True
                    break
        if not contained:
            kept.append(symbol)
    return kept


def tokenize_intent(intent: str) -> list[str]:
    stop = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "we",
        "with",
    }
    return list(dict.fromkeys(
        token
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", intent.lower())
        if len(token) > 2 and token not in stop
    ))


def intent_path_references(intent: str) -> list[str]:
    return unique_ordered(
        match.rstrip("/")
        for match in re.findall(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*", intent)
        if match.rstrip("/")
    )


def synthetic_symbol_summary(path: str, name: str, kind: str) -> str:
    words = " ".join(workflow_index.identifier_words(name)) or name
    parent = Path(path).parent.name.replace("_", " ").replace("-", " ")
    if parent and parent != ".":
        return f"{kind} in {parent}: {words}"
    return f"{kind}: {words}"


def compute_token_budget(conversation_tokens: int | None, explicit_budget: int | None) -> int:
    if explicit_budget is not None:
        return explicit_budget
    if not conversation_tokens or conversation_tokens < 1000:
        return DEFAULT_TOKEN_BUDGET
    scale = min(1.0, conversation_tokens / 100_000)
    return round(MIN_TOKEN_BUDGET + (MAX_TOKEN_BUDGET - MIN_TOKEN_BUDGET) * scale)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def cache_key_for(repo: Path, head_sha: str, extra: str = "") -> str:
    material = f"{repo.resolve()}\0{head_sha}\0{extra}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def analysis_checkout_path(repo: Path, head_sha: str, cache_dir: Path, mode: Mode) -> tuple[Path, str]:
    extra = "repo-analysis" if mode == "repo" else "local-analysis" if mode != "pr" else ""
    key = cache_key_for(repo, head_sha, extra)
    directory = "worktrees" if mode == "pr" else "analysis-checkouts" if mode == "repo" else "analysis-worktrees"
    return (cache_dir / directory / f"{repo.name}-{head_sha[:12]}-{key}").resolve(), key


def default_task_id(repo: Path, head_sha: str, intent: str | None = None) -> str:
    return cache_key_for(repo, head_sha, intent or "default")


def task_state_path(cache_dir: Path, repo: Path, head_sha: str, task_id: str) -> Path:
    repo_key = cache_key_for(repo, head_sha)
    return cache_dir / "tasks" / repo.name / repo_key / f"{task_id}.json"


def empty_task_state(repo: Path, head_sha: str, task_id: str, intent: str | None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repo": str(repo),
        "head_sha": head_sha,
        "task_id": task_id,
        "intent": intent,
        "read_files": [],
        "search_files": [],
        "edited_files": [],
        "mentioned_files": [],
        "previous_targets": [],
        "gitnexus_confirmed_files": [],
        "gitnexus_missing_files": [],
    }


def load_task_state(path: Path) -> dict[str, object]:
    if not path.exists():
        raise RuntimeError(f"task state not found: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"task state is not valid JSON: {path}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise RuntimeError(f"unsupported task state schema: {path}")
    return state


def save_task_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record_task_event(state: dict[str, object], event: TaskEvent, paths: Iterable[str]) -> dict[str, object]:
    field_by_event = {
        "read": "read_files",
        "search": "search_files",
        "edit": "edited_files",
        "mention": "mentioned_files",
    }
    field = field_by_event[event]
    existing = state.get(field)
    if not isinstance(existing, list):
        existing = []
    state[field] = unique_ordered([*(str(item) for item in existing), *paths])
    return state


def task_state_paths(state: dict[str, object], field: str) -> set[str]:
    value = state.get(field)
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if str(item)}


def safe_rmtree(path: Path, cache_root: Path) -> None:
    resolved = path.resolve()
    root = cache_root.resolve()
    if root not in resolved.parents and resolved != root:
        raise RuntimeError(f"refusing to remove path outside cache root: {resolved}")
    shutil.rmtree(resolved)


def require_cache_path(path: Path, cache_root: Path) -> Path:
    resolved = path.resolve()
    root = cache_root.resolve()
    if root not in resolved.parents and resolved != root:
        raise RuntimeError(f"refusing cache operation outside cache root: {resolved}")
    return resolved


def repo_relative_path(path: str) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe repository path: {path}")
    return relative


def source_worktree_files(repo: Path) -> list[str]:
    tracked = split_lines(run_git(repo, ["ls-files"], allow_fail=True))
    untracked = split_lines(
        run_git(repo, ["ls-files", "--others", "--exclude-standard"], allow_fail=True)
    )
    return [
        path
        for path in unique_ordered([*tracked, *untracked])
        if not is_generated_or_cache_path(path)
    ]


def candidate_tree(repo: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="repo-context-forge-index-") as temp_dir:
        index_path = Path(temp_dir) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
        run_cmd(["git", "read-tree", "HEAD"], cwd=repo, env=env)
        run_cmd(["git", "add", "-A", "--", "."], cwd=repo, env=env)
        generated_paths = run_cmd(
            ["git", "ls-files", "-z"], cwd=repo, env=env).stdout.split("\0")
        run_cmd(
            ["git", "rm", "-r", "--cached", "--ignore-unmatch", "--",
             *TOOL_CACHE_PATHS, *(path for path in generated_paths
                                  if path and is_generated_or_cache_path(path))],
            cwd=repo,
            env=env,
            allow_fail=True,
        )
        return run_cmd(["git", "write-tree"], cwd=repo, env=env).stdout.strip()


def locally_deleted_files(repo: Path) -> list[str]:
    unstaged = split_lines(
        run_git(repo, ["diff", "--name-only", "--diff-filter=D"], allow_fail=True)
    )
    staged = split_lines(
        run_git(repo, ["diff", "--name-only", "--cached", "--diff-filter=D"], allow_fail=True)
    )
    return [
        path
        for path in unique_ordered([*unstaged, *staged])
        if not is_generated_or_cache_path(path)
    ]


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def reset_cached_worktree(worktree: Path, cache_dir: Path, *, allow_fail: bool = False) -> None:
    require_cache_path(worktree, cache_dir)
    run_git(worktree, ["reset", "--hard", "HEAD"], allow_fail=allow_fail)
    exclusions = [item for path in TOOL_CACHE_DIRS for item in ("-e", f"{path}/")]
    run_git(worktree, ["clean", "-fd", *exclusions], allow_fail=allow_fail)
    soulforge_cache = worktree / ".soulforge"
    if os.path.lexists(soulforge_cache):
        require_cache_path(soulforge_cache, cache_dir)
        remove_path(soulforge_cache)


def overlay_source_worktree(source_repo: Path, analysis_repo: Path) -> None:
    for path in locally_deleted_files(source_repo):
        target = analysis_repo / repo_relative_path(path)
        remove_path(target)

    for path in source_worktree_files(source_repo):
        relative = repo_relative_path(path)
        source = source_repo / relative
        target = analysis_repo / relative
        if not os.path.lexists(source):
            remove_path(target)
            continue
        if source.is_dir() and not source.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        remove_path(target)
        shutil.copy2(source, target, follow_symlinks=False)


def checkout_uses_external_git_dir(checkout: Path) -> bool:
    raw = run_git(checkout, ["rev-parse", "--git-common-dir"], allow_fail=True)
    if not raw:
        return True
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = checkout / common_dir
    common_dir = common_dir.resolve()
    checkout = checkout.resolve()
    return checkout not in common_dir.parents and common_dir != checkout


def ensure_cached_checkout(source_repo: Path, head_sha: str, checkout: Path, cache_dir: Path) -> None:
    require_cache_path(checkout.parent, cache_dir)
    if checkout.exists():
        remove_checkout = True
        if is_git_repo(checkout) and not checkout_uses_external_git_dir(checkout):
            existing_sha = run_git(checkout, ["rev-parse", "HEAD"], allow_fail=True)
            remove_checkout = existing_sha != head_sha
        if remove_checkout:
            safe_rmtree(checkout, cache_dir)
        else:
            reset_cached_worktree(checkout, cache_dir, allow_fail=True)

    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(
            ["git", "clone", "--no-checkout", "--shared", str(source_repo), str(checkout)]
        )

    run_git(checkout, ["checkout", "-B", "repo-context-forge-target", head_sha])
    reset_cached_worktree(checkout, cache_dir)


def ensure_pr_worktree(source_repo: Path, head_ref: str, cache_dir: Path) -> TargetState:
    head_sha = run_git(source_repo, ["rev-parse", head_ref])
    worktree, key = analysis_checkout_path(source_repo, head_sha, cache_dir, "pr")

    ensure_cached_checkout(source_repo, head_sha, worktree, cache_dir)
    cleanup_soulforge_gitignore_change(worktree)
    target_dirty = is_dirty(worktree, ignore_tool_cache=True)
    if target_dirty:
        raise RuntimeError(f"PR target worktree is dirty: {worktree}")

    return TargetState(
        mode="pr",
        source_repo=source_repo,
        analysis_repo=worktree,
        base_ref="",
        head_ref=head_ref,
        head_sha=head_sha,
        source_dirty=is_dirty(source_repo, ignore_tool_cache=True),
        target_dirty=target_dirty,
        cache_key=key,
    )


def ensure_repo_analysis_checkout(source_repo: Path, head_ref: str, cache_dir: Path) -> TargetState:
    head_sha = run_git(source_repo, ["rev-parse", head_ref])
    checkout, key = analysis_checkout_path(source_repo, head_sha, cache_dir, "repo")

    ensure_cached_checkout(source_repo, head_sha, checkout, cache_dir)
    cleanup_soulforge_gitignore_change(checkout)
    target_dirty = is_dirty(checkout, ignore_tool_cache=True)
    if target_dirty:
        raise RuntimeError(f"repo target checkout is dirty: {checkout}")

    return TargetState(
        mode="repo",
        source_repo=source_repo,
        analysis_repo=checkout,
        base_ref="",
        head_ref=head_ref,
        head_sha=head_sha,
        source_dirty=is_dirty(source_repo, ignore_tool_cache=True),
        target_dirty=target_dirty,
        cache_key=key,
    )


def ensure_local_analysis_worktree(source_repo: Path, head_ref: str, cache_dir: Path) -> TargetState:
    head_sha = run_git(source_repo, ["rev-parse", head_ref])
    worktree, key = analysis_checkout_path(source_repo, head_sha, cache_dir, "local")

    ensure_cached_checkout(source_repo, head_sha, worktree, cache_dir)
    overlay_source_worktree(source_repo, worktree)
    cleanup_soulforge_gitignore_change(worktree)

    source_dirty = is_dirty(source_repo, ignore_tool_cache=True)
    return TargetState(
        mode="local",
        source_repo=source_repo,
        analysis_repo=worktree,
        base_ref="",
        head_ref=head_ref,
        head_sha=head_sha,
        source_dirty=source_dirty,
        target_dirty=source_dirty,
        cache_key=key,
    )


def resolve_target_state(
    repo: Path,
    mode: Mode,
    base_ref: str,
    head_ref: str,
    cache_dir: Path,
) -> TargetState:
    source_repo = repo_root(repo)
    if mode == "pr":
        target = ensure_pr_worktree(source_repo, head_ref, cache_dir)
        return TargetState(
            mode=target.mode,
            source_repo=target.source_repo,
            analysis_repo=target.analysis_repo,
            base_ref=base_ref,
            head_ref=head_ref,
            head_sha=target.head_sha,
            source_dirty=target.source_dirty,
            target_dirty=target.target_dirty,
            cache_key=target.cache_key,
        )

    if mode == "repo":
        target = ensure_repo_analysis_checkout(source_repo, head_ref, cache_dir)
        return TargetState(
            mode=target.mode,
            source_repo=target.source_repo,
            analysis_repo=target.analysis_repo,
            base_ref=base_ref,
            head_ref=head_ref,
            head_sha=target.head_sha,
            source_dirty=target.source_dirty,
            target_dirty=target.target_dirty,
            cache_key=target.cache_key,
        )

    target = ensure_local_analysis_worktree(source_repo, head_ref, cache_dir)
    return TargetState(
        mode=mode,
        source_repo=target.source_repo,
        analysis_repo=target.analysis_repo,
        base_ref=base_ref,
        head_ref=head_ref,
        head_sha=target.head_sha,
        source_dirty=target.source_dirty,
        target_dirty=target.target_dirty,
        cache_key=target.cache_key,
    )


def find_soulforge_binary(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    found = shutil.which("soulforge")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "soulforge"
    return str(fallback) if fallback.exists() else None


def build_soulforge_map(
    repo: Path,
    soulforge_bin: str | None,
    build_mode: MapBuildMode,
    timeout_ms: int,
) -> MapBuildResult:
    db_path = repo / ".soulforge" / "repomap.db"
    if build_mode == "never":
        return MapBuildResult(False, [], None, "", "", None)
    if db_path.exists() and build_mode == "auto":
        return MapBuildResult(False, [], None, "", "", None)
    if not soulforge_bin:
        return MapBuildResult(
            False,
            [],
            None,
            "",
            "",
            "SoulForge binary not found; pass --soulforge-bin or install soulforge",
        )

    command = [
        soulforge_bin,
        "--headless",
        "--quiet",
        "--no-render",
        "--timeout",
        str(timeout_ms),
        "Reply exactly: OK",
    ]
    proc = run_cmd(command, cwd=repo, allow_fail=True)
    warning = None
    if proc.returncode != 0:
        warning = f"SoulForge exited with {proc.returncode}"
    if not db_path.exists():
        warning = (warning + "; " if warning else "") + f"map DB not found at {db_path}"
    return MapBuildResult(True, command, proc.returncode, proc.stdout, proc.stderr, warning)


class SoulForgeMap:
    def __init__(
        self,
        repo: Path,
        native_index: workflow_index.WorkflowIndex | None = None,
    ) -> None:
        self.native_index = native_index or workflow_index.WorkflowIndex(repo, file_role)
        self.db_path = repo / ".soulforge" / "repomap.db"

    @property
    def available(self) -> bool:
        return self.db_path.exists()

    def _connect(self) -> sqlite3.Connection:
        if not self.available:
            raise FileNotFoundError(f"SoulForge map not found: {self.db_path}")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def stats(self) -> dict[str, int | str | bool]:
        if not self.available:
            return {"available": False, "path": str(self.db_path)}
        with self._connect() as conn:
            result: dict[str, int | str | bool] = {
                "available": True,
                "path": str(self.db_path),
            }
            for table in ("files", "symbols", "edges", "refs", "cochanges", "calls", "summaries"):
                if self._has_table(conn, table):
                    result[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return result

    def top_files(self, limit: int) -> list[MapFile]:
        if self.native_index.is_available:
            return self.native_index.ranked_files(limit)
        if not self.available:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT path, pagerank, symbol_count, line_count
                FROM files
                ORDER BY pagerank DESC, symbol_count DESC, path ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            MapFile(
                path=str(row["path"]),
                pagerank=float(row["pagerank"] or 0),
                symbol_count=int(row["symbol_count"] or 0),
                line_count=int(row["line_count"] or 0),
                rank=index + 1,
            )
            for index, row in enumerate(rows)
        ]

    def files_by_path(self, paths: Iterable[str]) -> dict[str, MapFile]:
        wanted = list(paths)
        if not wanted:
            return {}
        if self.native_index.is_available:
            return self.native_index.lookup_files(wanted)
        if not self.available:
            return {}
        sql_marks = ",".join("?" for _ in wanted)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH ranked AS (
                  SELECT path, pagerank, symbol_count, line_count,
                         ROW_NUMBER() OVER (ORDER BY pagerank DESC, symbol_count DESC, path ASC) AS rank
                  FROM files
                )
                SELECT path, pagerank, symbol_count, line_count, rank
                FROM ranked
                WHERE path IN ({sql_marks})
                """,
                wanted,
            ).fetchall()
        return {
            str(row["path"]): MapFile(
                path=str(row["path"]),
                pagerank=float(row["pagerank"] or 0),
                symbol_count=int(row["symbol_count"] or 0),
                line_count=int(row["line_count"] or 0),
                rank=int(row["rank"]),
            )
            for row in rows
        }

    def symbols_for_file(self, path: str, limit: int = 12) -> list[Symbol]:
        if self.native_index.is_available:
            return [self._native_symbol(entry) for entry in self.native_index.file_symbols(path, limit)]
        if not self.available:
            return []
        with self._connect() as conn:
            summary_join = ""
            summary_select = "NULL AS summary"
            source_select = "NULL AS summary_source"
            if self._has_table(conn, "semantic_summaries"):
                summary_join = """
                LEFT JOIN semantic_summaries ss ON ss.symbol_id = s.id
                  AND ss.source = (
                    SELECT source
                    FROM semantic_summaries
                    WHERE symbol_id = s.id
                    ORDER BY CASE source
                      WHEN 'llm' THEN 0
                      WHEN 'lsp' THEN 1
                      WHEN 'ast' THEN 2
                      WHEN 'synthetic' THEN 3
                      ELSE 4
                    END, source ASC
                    LIMIT 1
                  )
                """
                summary_select = "ss.summary AS summary"
                source_select = "ss.source AS summary_source"
            rows = conn.execute(
                f"""
                SELECT s.name, s.kind, s.line, s.end_line, s.signature, s.is_exported,
                       {summary_select}, {source_select}
                FROM symbols s
                JOIN files f ON f.id = s.file_id
                {summary_join}
                WHERE f.path = ?
                ORDER BY s.is_exported DESC, s.line ASC
                LIMIT ?
                """,
                (path, limit),
            ).fetchall()
        symbols: list[Symbol] = []
        for row in rows:
            name = str(row["name"])
            kind = str(row["kind"])
            summary = str(row["summary"]) if row["summary"] is not None else None
            source = str(row["summary_source"]) if row["summary_source"] is not None else None
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    line=int(row["line"] or 0),
                    end_line=int(row["end_line"] or row["line"] or 0),
                    signature=str(row["signature"]) if row["signature"] is not None else None,
                    is_exported=bool(row["is_exported"]),
                    summary=summary or synthetic_symbol_summary(path, name, kind),
                    summary_source=source or "synthetic_fallback",
                )
            )
        return symbols

    def dependent_count_for_file(self, path: str) -> int:
        return len(self.file_links(path, direction="dependents", limit=-1))

    def file_links(
        self,
        path: str,
        *,
        direction: Literal["dependents", "dependencies"],
        limit: int = 8,
    ) -> list[dict[str, float | str]]:
        if not self.available:
            return []
        if direction == "dependents":
            source_join = "other.id = e.source_file_id"
            edge_filter = "e.target_file_id = f.id"
        else:
            source_join = "other.id = e.target_file_id"
            edge_filter = "e.source_file_id = f.id"
        with self._connect() as conn:
            if not self._has_table(conn, "edges"):
                return []
            rows = conn.execute(
                f"""
                SELECT other.path AS path, SUM(e.weight) AS weight
                FROM files f
                JOIN edges e ON {edge_filter}
                JOIN files other ON {source_join}
                WHERE f.path = ? AND other.path != f.path
                GROUP BY other.path
                ORDER BY weight DESC, other.path ASC
                LIMIT ?
                """,
                (path, limit),
            ).fetchall()
        return [
            {"path": str(row["path"]), "weight": float(row["weight"] or 0)}
            for row in rows
            if not is_generated_or_cache_path(str(row["path"]))
        ]

    def transitive_dependent_count_for_file(self, path: str, limit: int = 200) -> int:
        if not self.available:
            return 0
        with self._connect() as conn:
            if not self._has_table(conn, "edges"):
                return 0
            rows = conn.execute(
                """
                WITH RECURSIVE upstream(file_id, depth) AS (
                  SELECT e.source_file_id, 1
                  FROM edges e
                  JOIN files f ON f.id = e.target_file_id
                  WHERE f.path = ?
                  UNION
                  SELECT e.source_file_id, upstream.depth + 1
                  FROM edges e
                  JOIN upstream ON upstream.file_id = e.target_file_id
                  WHERE upstream.depth < 4
                )
                SELECT DISTINCT f.path
                FROM upstream
                JOIN files f ON f.id = upstream.file_id
                LIMIT ?
                """,
                (path, limit),
            ).fetchall()
        return sum(
            1
            for row in rows
            if str(row["path"]) != path and not is_generated_or_cache_path(str(row["path"]))
        )

    def exported_symbols_at_risk(self, path: str, limit: int = 8) -> list[dict[str, object]]:
        if not self.available:
            return []
        with self._connect() as conn:
            if not self._has_table(conn, "symbols"):
                return []
            has_refs = self._has_table(conn, "refs")
            has_calls = self._has_table(conn, "calls")
            refs_select = (
                """
                (
                  SELECT COUNT(DISTINCT rf.path)
                  FROM refs r
                  JOIN files rf ON rf.id = r.file_id
                  WHERE r.source_file_id = f.id
                    AND r.name = s.name
                    AND rf.path != f.path
                ) AS ref_files
                """
                if has_refs
                else "0 AS ref_files"
            )
            calls_select = (
                """
                (
                  SELECT COUNT(DISTINCT cf.path)
                  FROM calls c
                  JOIN symbols caller ON caller.id = c.caller_symbol_id
                  JOIN files cf ON cf.id = caller.file_id
                  WHERE c.callee_symbol_id = s.id
                    AND cf.path != f.path
                ) AS call_files
                """
                if has_calls
                else "0 AS call_files"
            )
            if has_refs and has_calls:
                usage_files_select = """
                (
                  SELECT COUNT(DISTINCT usage.path)
                  FROM (
                    SELECT rf.path AS path
                    FROM refs r
                    JOIN files rf ON rf.id = r.file_id
                    WHERE r.source_file_id = f.id
                      AND r.name = s.name
                      AND rf.path != f.path
                    UNION
                    SELECT cf.path AS path
                    FROM calls c
                    JOIN symbols caller ON caller.id = c.caller_symbol_id
                    JOIN files cf ON cf.id = caller.file_id
                    WHERE c.callee_symbol_id = s.id
                      AND cf.path != f.path
                  ) usage
                ) AS usage_files
                """
            elif has_refs:
                usage_files_select = """
                (
                  SELECT COUNT(DISTINCT rf.path)
                  FROM refs r
                  JOIN files rf ON rf.id = r.file_id
                  WHERE r.source_file_id = f.id
                    AND r.name = s.name
                    AND rf.path != f.path
                ) AS usage_files
                """
            elif has_calls:
                usage_files_select = """
                (
                  SELECT COUNT(DISTINCT cf.path)
                  FROM calls c
                  JOIN symbols caller ON caller.id = c.caller_symbol_id
                  JOIN files cf ON cf.id = caller.file_id
                  WHERE c.callee_symbol_id = s.id
                    AND cf.path != f.path
                ) AS usage_files
                """
            else:
                usage_files_select = "0 AS usage_files"
            rows = conn.execute(
                f"""
                SELECT s.name, s.kind, s.line, s.end_line, s.signature,
                       {refs_select},
                       {calls_select},
                       {usage_files_select}
                FROM symbols s
                JOIN files f ON f.id = s.file_id
                WHERE f.path = ? AND s.is_exported = 1
                ORDER BY usage_files DESC, s.line ASC
                LIMIT ?
                """,
                (path, limit),
            ).fetchall()
        return [
            {
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "line": int(row["line"] or 0),
                "end_line": int(row["end_line"] or row["line"] or 0),
                "signature": str(row["signature"]) if row["signature"] is not None else None,
                "ref_files": int(row["ref_files"] or 0),
                "call_files": int(row["call_files"] or 0),
                "usage_files": int(row["usage_files"] or 0),
            }
            for row in rows
        ]

    def impact_summary_for_file(self, path: str) -> dict[str, object]:
        dependents = self.file_links(path, direction="dependents")
        dependencies = self.file_links(path, direction="dependencies")
        cochanges = self.cochanges_for_file(path)
        symbols = self.exported_symbols_at_risk(path)
        direct = self.dependent_count_for_file(path)
        transitive = self.transitive_dependent_count_for_file(path)
        max_symbol_usage = max((int(symbol["usage_files"]) for symbol in symbols), default=0)
        if direct >= 10 or transitive >= 25 or max_symbol_usage >= 10:
            risk = "high"
        elif direct >= 3 or transitive >= 8 or max_symbol_usage >= 3:
            risk = "medium"
        else:
            risk = "low"
        return {
            "direct_dependents": direct,
            "dependencies": len(dependencies),
            "cochange_partners": len(cochanges),
            "total_affected_scope": transitive,
            "risk": risk,
            "dependents": dependents,
            "dependency_files": dependencies,
            "cochanges": cochanges,
            "exported_symbols_at_risk": symbols,
        }

    def graph_neighbors_for_file(self, path: str, limit: int = 5) -> list[dict[str, float | str]]:
        if not self.available:
            return []
        with self._connect() as conn:
            if not self._has_table(conn, "edges"):
                return []
            rows = conn.execute(
                """
                SELECT other.path AS path, SUM(e.weight) AS weight
                FROM files f
                JOIN edges e ON e.source_file_id = f.id OR e.target_file_id = f.id
                JOIN files other ON other.id = CASE
                  WHEN e.source_file_id = f.id THEN e.target_file_id
                  ELSE e.source_file_id
                END
                WHERE f.path = ?
                GROUP BY other.path
                ORDER BY weight DESC, other.path ASC
                LIMIT ?
                """,
                (path, limit),
            ).fetchall()
        return [
            {"path": str(row["path"]), "weight": float(row["weight"] or 0)}
            for row in rows
            if not is_generated_or_cache_path(str(row["path"]))
        ]

    def related_files_for_paths(self, paths: Iterable[str], limit: int) -> list[str]:
        """Intentionally prefer SoulForge graph/cochange; workflow heuristics are fallback."""
        if limit <= 0:
            return []
        if not self.available:
            return self.native_index.related_paths(paths, limit)
        base_paths = set(paths)
        scores: dict[str, float] = {}
        for path in base_paths:
            for neighbor in self.graph_neighbors_for_file(path, limit=limit):
                candidate = str(neighbor["path"])
                if candidate not in base_paths:
                    scores[candidate] = scores.get(candidate, 0) + float(neighbor["weight"])
            for partner in self.cochanges_for_file(path, limit=limit):
                candidate = str(partner["path"])
                if candidate not in base_paths and not is_generated_or_cache_path(candidate):
                    scores[candidate] = scores.get(candidate, 0) + int(partner["count"]) * 0.5
        return [
            path
            for path, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        ]

    def intent_files(self, tokens: list[str], limit: int) -> list[str]:
        if self.native_index.is_available:
            return self.native_index.rank_intent(tokens, limit)
        if not self.available or not tokens:
            return []
        with self._connect() as conn:
            files = conn.execute(
                "SELECT id, path, pagerank, symbol_count FROM files"
            ).fetchall()
            symbols = conn.execute(
                "SELECT f.path AS path, s.name AS name, s.signature AS signature "
                "FROM symbols s JOIN files f ON f.id = s.file_id"
            ).fetchall()

        scores: dict[str, float] = {}
        for row in files:
            path = str(row["path"])
            haystack = path.lower()
            score = float(row["pagerank"] or 0) * 1000
            for token in tokens:
                if token in haystack:
                    score += 5
            if score > 0:
                scores[path] = score

        for row in symbols:
            path = str(row["path"])
            haystack = f"{row['name'] or ''} {row['signature'] or ''}".lower()
            for token in tokens:
                if token in haystack:
                    scores[path] = scores.get(path, 0) + 4

        return [
            path
            for path, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        ]

    def cochanges_for_file(self, path: str, limit: int = 5) -> list[dict[str, int | str]]:
        if not self.available:
            return []
        with self._connect() as conn:
            if not self._has_table(conn, "cochanges"):
                return []
            rows = conn.execute(
                """
                SELECT other.path AS path, c.count AS count
                FROM files f
                JOIN cochanges c ON c.file_id_a = f.id OR c.file_id_b = f.id
                JOIN files other ON other.id = CASE
                  WHEN c.file_id_a = f.id THEN c.file_id_b
                  ELSE c.file_id_a
                END
                WHERE f.path = ?
                ORDER BY c.count DESC, other.path ASC
                LIMIT ?
                """,
                (path, limit),
            ).fetchall()
        return [{"path": str(row["path"]), "count": int(row["count"])} for row in rows]

    @staticmethod
    def _native_symbol(entry: workflow_index.IndexedSymbol) -> Symbol:
        return Symbol(
            name=entry.name, kind=entry.kind, line=entry.line, end_line=entry.end_line, signature=entry.signature, is_exported=entry.is_exported,
            summary=entry.summary, summary_source=entry.summary_source,
        )

    @staticmethod
    def _has_table(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None


def target_files_for_mode(
    mode: Mode,
    source_git_state: GitState,
    soul_map: SoulForgeMap,
    intent: str | None,
    top: int,
    reference_only: Iterable[str] = (),
    intent_resolution: workflow_index.IntentResolution | None = None,
) -> list[str]:
    reference_prefixes = tuple(reference_only)
    if mode == "pr":
        changed = [
            path
            for path in source_git_state.pr_files
            if not is_generated_or_cache_path(path)
        ]
        related_limit = max(0, max(top * 3, top + 20) - len(changed))
        related = [
            path
            for path in soul_map.related_files_for_paths(changed, related_limit)
            if not is_reference_only_path(path, reference_prefixes)
        ]
        return unique_ordered([*changed, *related])[:top]
    if mode == "local":
        return [
            path
            for path in selected_files(source_git_state, "all")
            if not is_generated_or_cache_path(path)
        ][:top]
    if mode == "repo":
        return [
            entry.path
            for entry in soul_map.top_files(max(top * 3, top + 20))
            if not is_generated_or_cache_path(entry.path)
            and not is_reference_only_path(entry.path, reference_prefixes)
        ][:top]
    resolution = intent_resolution or soul_map.native_index.resolve_intent(intent or "")
    candidates = [
        item.path
        for item in resolution.file_evidence
        if not is_generated_or_cache_path(item.path)
        and not is_reference_only_path(item.path, reference_prefixes)
    ]
    path_references = intent_path_references(intent or "")
    exact_files = [item.path for item in resolution.file_evidence if item.exact_file]
    directory_matches = [
        path
        for path in candidates
        if any(path.startswith(f"{reference}/") for reference in path_references)
    ]
    return [
        path
        for path in unique_ordered([*exact_files, *directory_matches, *candidates])
        if Path(path).name not in INTENT_GRAPH_EXCLUDED_NAMES or path not in directory_matches or path in path_references
    ][:top]


def rank_target_entry(
    entry: dict[str, object],
    source_git_state: GitState,
    *,
    mode: Mode,
) -> dict[str, object]:
    path = str(entry["path"])
    role = file_role(path)
    changed_file = path in source_git_state.pr_files
    dirty_file = mode != "pr" and path in selected_files(source_git_state, "dirty")
    changed_symbols = entry.get("changed_symbols")
    symbol_list = changed_symbols if isinstance(changed_symbols, list) else []
    cochanges = entry.get("cochanges")
    graph_neighbors = entry.get("graph_neighbors")
    pagerank = entry.get("pagerank")

    signals: list[str] = []
    reasons: list[str] = []
    score = 0.0
    if changed_file:
        signals.append("changed_file")
        reasons.append("changed in base...HEAD diff")
        score += 1000
    if dirty_file:
        signals.append("dirty_file")
        reasons.append("present in local staged/unstaged/untracked state")
        score += 750
    if role == "production":
        signals.append("production_file")
        score += 350 if changed_file or dirty_file else 80
    elif role == "test":
        signals.append("test_file")
        reasons.append("test or verification surface")
        score += 150 if changed_file or dirty_file else 40
    else:
        signals.append("generated_file")
        score -= 1000
    if symbol_list:
        signals.append("changed_symbol")
        reasons.append("contains symbols overlapping changed hunks")
        score += 300
    if isinstance(graph_neighbors, list) and graph_neighbors:
        signals.append("graph_neighbor")
        reasons.append("has import/reference graph neighbors in SoulForge map")
        score += 90
    if isinstance(cochanges, list) and cochanges:
        signals.append("cochange_partner")
        reasons.append("has historical co-change partners in SoulForge map")
        score += 70
    if isinstance(pagerank, float | int) and pagerank:
        signals.append("pagerank")
        score += min(float(pagerank) * 1000, 100)
    broad_test_container = False
    for symbol in symbol_list:
        if not isinstance(symbol, dict):
            continue
        if role == "test" and symbol.get("kind") == "class":
            start = int(symbol.get("line") or 0)
            end = int(symbol.get("end_line") or 0)
            if end - start > 400:
                broad_test_container = True
                break
    if broad_test_container:
        signals.append("broad_test_container")
        reasons.append("broad test container kept as verification context")
        score -= 250

    entry["surface_role"] = role
    entry["rank_signals"] = signals
    entry["why_selected"] = reasons or ["selected by target mode"]
    entry["priority_score"] = round(score, 4)
    return entry


def target_entry_sort_key(entry: dict[str, object]) -> tuple[object, ...]:
    relevance = entry["intent_evidence"].get("relevance_score", 0) if isinstance(entry.get("intent_evidence"), dict) else 0
    return (
        -float(relevance),
        -float(entry.get("priority_score") or 0),
        0 if entry.get("surface_role") == "production" else 1,
        int(entry["rank"]) if isinstance(entry.get("rank"), int) else 999999,
        str(entry["path"]),
    )


def apply_task_state_to_entries(
    target_entries: list[dict[str, object]],
    task_state: dict[str, object] | None,
) -> list[dict[str, object]]:
    if not task_state:
        return target_entries
    boosts = [
        ("edited_files", "edited_file", "edited during this task", 500),
        ("read_files", "read_file", "read during this task", 180),
        ("search_files", "search_hit", "matched a task search", 140),
        ("mentioned_files", "mentioned_file", "mentioned in task context", 160),
        ("previous_targets", "previous_target", "selected in a prior packet", 50),
        ("gitnexus_confirmed_files", "gitnexus_confirmed", "confirmed by GitNexus", 220),
        ("gitnexus_missing_files", "gitnexus_missing", "not confirmed by GitNexus", -160),
    ]
    for entry in target_entries:
        path = str(entry["path"])
        signals = entry.get("rank_signals")
        if not isinstance(signals, list):
            signals = []
        reasons = entry.get("why_selected")
        if not isinstance(reasons, list):
            reasons = []
        score = float(entry.get("priority_score") or 0)
        for field, signal, reason, boost in boosts:
            if path in task_state_paths(task_state, field):
                if signal not in signals:
                    signals.append(signal)
                if reason not in reasons:
                    reasons.append(reason)
                score += boost
        entry["rank_signals"] = signals
        entry["why_selected"] = reasons
        entry["priority_score"] = round(score, 4)
    return sorted(target_entries, key=target_entry_sort_key)


def is_changed_target(entry: dict[str, object]) -> bool:
    signals = entry.get("rank_signals")
    return isinstance(signals, list) and any(
        signal in signals
        for signal in ("changed_file", "dirty_file", "edited_file")
    )


def is_doc_path(path: str) -> bool:
    name = Path(path).name.lower()
    return (
        name.endswith((".md", ".rst", ".txt"))
        or path.startswith(("docs/", "doc/"))
        or name in {"readme", "readme.md"}
    )


def target_paths_for_area(
    target_entries: list[dict[str, object]],
    predicate,
) -> list[str]:
    paths: list[str] = []
    for entry in target_entries:
        path = str(entry.get("path") or "")
        if path and predicate(entry, path):
            paths.append(path)
    return unique_ordered(paths)


def make_coverage_area(
    area_id: str,
    kind: str,
    files: list[str],
    why: str,
    must_answer: str,
) -> dict[str, object] | None:
    if not files:
        return None
    return {
        "id": area_id,
        "kind": kind,
        "required": True,
        "files": files,
        "why": why,
        "must_answer": must_answer,
    }


def build_coverage_plan(target_entries: list[dict[str, object]]) -> dict[str, object]:
    areas: list[dict[str, object]] = []

    def add(area: dict[str, object] | None) -> None:
        if area:
            areas.append(area)

    add(make_coverage_area(
        "production_contract",
        "production",
        target_paths_for_area(
            target_entries,
            lambda entry, path: is_changed_target(entry)
            and entry.get("surface_role") == "production"
            and not is_doc_path(path),
        ),
        "changed production surface from the packet",
        "What behavior or contract changed, and which consumers or no-change paths could regress?",
    ))
    add(make_coverage_area(
        "verification_contract",
        "verification",
        target_paths_for_area(
            target_entries,
            lambda entry, _path: entry.get("surface_role") == "test"
            and (
                is_changed_target(entry)
                or "broad_test_container" in [str(item) for item in entry.get("rank_signals", [])]
            ),
        ),
        "changed or broad verification surface from the packet",
        "Do the tests prove the changed contract, and can they pass for the wrong reason?",
    ))
    add(make_coverage_area(
        "operator_contract",
        "operator",
        target_paths_for_area(
            target_entries,
            lambda entry, path: is_changed_target(entry) and is_doc_path(path),
        ),
        "changed docs or operator-facing contract surface",
        "Does the operator contract match runtime behavior, configuration, and failure modes?",
    ))
    add(make_coverage_area(
        "blast_radius",
        "impact",
        target_paths_for_area(
            target_entries,
            lambda entry, _path: isinstance(entry.get("soulforge_impact"), dict)
            and (
                str(entry["soulforge_impact"].get("risk") or "low") != "low"
                or int(entry["soulforge_impact"].get("direct_dependents") or 0) > 0
                or int(entry["soulforge_impact"].get("total_affected_scope") or 0) > 0
            ),
        ),
        "SoulForge impact marks dependents, affected scope, or elevated risk",
        "Which dependents, co-change partners, and unchanged flows must remain safe?",
    ))
    add(make_coverage_area(
        "related_map_surface",
        "related",
        target_paths_for_area(
            target_entries,
            lambda entry, _path: not is_changed_target(entry)
            and (
                "graph_neighbor" in [str(item) for item in entry.get("rank_signals", [])]
                or "cochange_partner" in [str(item) for item in entry.get("rank_signals", [])]
            ),
        ),
        "repo-map related surface selected by graph or co-change signals",
        "Why is this related surface safe or relevant, despite not being directly changed?",
    ))

    return {
        "required": bool(areas),
        "delegation_required": False,
        "areas": areas,
    }


def make_target_entries(
    *,
    mode: Mode,
    source_repo: Path,
    analysis_repo: Path,
    base_ref: str,
    head_ref: str,
    source_git_state: GitState,
    soul_map: SoulForgeMap,
    targets: list[str],
    intent: str | None = None,
    intent_resolution: workflow_index.IntentResolution | None = None,
) -> list[dict[str, object]]:
    if mode == "intent":
        intent_resolution = intent_resolution or soul_map.native_index.resolve_intent(intent or "")
    else:
        intent = None
        intent_resolution = workflow_index.IntentResolution((), (), (), ())
    relevance_by_symbol = {
        (item.path, item.line, item.name): item.score
        for item in intent_resolution.symbol_relevance
    }
    file_evidence_by_path = {
        item.path: item for item in intent_resolution.file_evidence
    }
    map_files = soul_map.files_by_path(targets)
    symbols_by_path = {
        path: soul_map.symbols_for_file(
            path,
            limit=max(80, int(map_files[path].symbol_count or 0))
            if path in map_files
            else 80,
        )
        for path in targets
    }
    required_symbol_keys: set[tuple[str, int, str]] = set(
        (match.path, match.symbol.line, match.symbol.name)
        for match in intent_resolution.required_symbols
    )
    directory_owner_paths: set[str] = set()
    for reference in intent_path_references(intent or ""):
        owner = next(
            (path for path in targets if path.startswith(f"{reference}/")),
            None,
        )
        if owner:
            directory_owner_paths.add(owner)
    target_entries: list[dict[str, object]] = []
    for path in targets:
        map_file = map_files.get(path)
        scope: Scope = "pr" if mode == "pr" else "dirty"
        ranges = (
            diff_ranges_for_file(source_repo, base_ref, scope, path, head_ref=head_ref)
            if mode not in {"intent", "repo"}
            else []
        )
        symbols = symbols_by_path[path]
        changed_symbols = remove_nested_symbols(
            [symbol for symbol in symbols if symbol_overlaps(symbol, ranges)]
        )
        intent_required_symbols = [
            symbol
            for symbol in symbols
            if (path, symbol.line, symbol.name) in required_symbol_keys
        ]
        intent_symbols = [
            *intent_required_symbols,
            *(symbol for symbol in symbols if symbol not in intent_required_symbols),
        ]
        planner_symbols = sorted(
            symbols,
            key=lambda symbol: (
                -relevance_by_symbol.get((path, symbol.line, symbol.name), 0),
                not symbol.is_exported,
                symbol.line,
                symbol.name,
            ),
        )
        intent_names_file = bool(intent) and any(
            re.search(
                rf"(?<![A-Za-z0-9_./-]){re.escape(candidate)}(?![A-Za-z0-9_./-])",
                intent,
            )
            for candidate in (path, Path(path).name)
        )
        intent_required_file = not intent_required_symbols and (
            intent_names_file or path in directory_owner_paths
        )
        file_evidence = file_evidence_by_path.get(path)
        display_symbols = changed_symbols[:12] if changed_symbols else intent_symbols[:12]
        dirty_kinds = []
        if path in source_git_state.staged_files:
            dirty_kinds.append("staged")
        if path in source_git_state.unstaged_files:
            dirty_kinds.append("unstaged")
        if path in source_git_state.untracked_files:
            dirty_kinds.append("untracked")
        entry = {
            "path": path,
            "dirty_kinds": dirty_kinds,
            "source_dirty_overlap": bool(dirty_kinds),
            "scope_contaminated": mode != "pr" and bool(dirty_kinds),
            "changed_ranges": ranges,
            "in_soulforge_map": map_file is not None,
            "rank": map_file.rank if map_file else None,
            "pagerank": map_file.pagerank if map_file else None,
            "symbol_count": map_file.symbol_count if map_file else None,
            "line_count": map_file.line_count if map_file else None,
            "changed_symbols": [symbol.__dict__ for symbol in changed_symbols],
            "symbols": [symbol.__dict__ for symbol in display_symbols],
            "_planner_symbols": [symbol.__dict__ for symbol in planner_symbols],
            "intent_required_symbols": [symbol.name for symbol in intent_required_symbols],
            "intent_required_file": intent_required_file,
            "dependent_count": soul_map.dependent_count_for_file(path),
            "graph_neighbors": soul_map.graph_neighbors_for_file(path),
            "cochanges": soul_map.cochanges_for_file(path),
            "soulforge_impact": soul_map.impact_summary_for_file(path),
            "analysis_repo": str(analysis_repo),
        }
        if file_evidence is not None:
            entry["intent_evidence"] = file_evidence.__dict__
        target_entries.append(
            rank_target_entry(entry, source_git_state, mode=mode)
        )
    return sorted(target_entries, key=target_entry_sort_key)


def build_gitnexus_plan(
    target_entries: list[dict[str, object]],
    repo_name: str | None = None,
    *,
    intent_mode: bool = False,
) -> tuple[list[dict[str, object]], int, list[dict[str, object]]]:
    required_groups: list[list[dict[str, object]]] = []
    optional_queues: list[list[list[dict[str, object]]]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in target_entries:
        entry_optional_groups: list[list[dict[str, object]]] = []
        path = str(entry["path"])
        symbols = (
            entry.get("changed_symbols")
            or entry.get("_planner_symbols")
            or entry.get("symbols")
        )
        required_file = entry.get("intent_required_file") is True
        file_key = ("file_context", path, path, "")
        if required_file and file_key not in seen:
            seen.add(file_key)
            file_item: dict[str, object] = {
                "kind": "file_context",
                "file": path,
                "target": path,
                "required": True,
            }
            if repo_name:
                file_item["repo"] = repo_name
            required_groups.append([file_item])
        if not isinstance(symbols, list) or not symbols:
            if not required_file and not intent_mode and file_key not in seen:
                seen.add(file_key)
                file_item = {
                    "kind": "file_context",
                    "file": path,
                    "target": path,
                }
                if repo_name:
                    file_item["repo"] = repo_name
                entry_optional_groups.append([file_item])
            if entry_optional_groups:
                optional_queues.append(entry_optional_groups)
            continue
        required_names = unique_ordered(
            name
            for name in entry.get("intent_required_symbols") or []
            if isinstance(name, str) and name
        )
        required_name_set = set(required_names)
        planned_symbols = [(name, True) for name in required_names]
        for symbol in symbols:
            if not isinstance(symbol, dict):
                continue
            name = symbol.get("name")
            if (
                not isinstance(name, str)
                or not name
                or name in required_name_set
                or symbol.get("kind") not in {"function", "class", "method"}
            ):
                continue
            planned_symbols.append((name, False))
        for name, required in planned_symbols:
            context_item: dict[str, object] = {
                "kind": "symbol_context",
                "target": name,
                "file": path,
            }
            impact_item: dict[str, object] = {
                "kind": "symbol_impact",
                "target": name,
                "file": path,
                "direction": "upstream",
            }
            if repo_name:
                context_item["repo"] = repo_name
                impact_item["repo"] = repo_name
            context_key = ("symbol_context", path, name, "")
            impact_key = ("symbol_impact", path, name, "upstream")
            if context_key in seen and impact_key in seen:
                continue
            seen.update((context_key, impact_key))
            group = [context_item, impact_item]
            if required:
                context_item["required"] = True
                impact_item["required"] = True
                required_groups.append(group)
            else:
                entry_optional_groups.append(group)
        if entry_optional_groups:
            optional_queues.append(entry_optional_groups)

    optional_groups = [
        queue[index]
        for index in range(max((len(queue) for queue in optional_queues), default=0))
        for queue in optional_queues
        if index < len(queue)
    ]
    plan: list[dict[str, object]] = []
    omitted_required_checks: list[dict[str, object]] = []
    for group in required_groups:
        if len(plan) + len(group) > MAX_GITNEXUS_CHECKS:
            omitted_required_checks.extend(dict(item) for item in group)
            continue
        plan.extend(group)

    omitted_optional_checks = 0
    if omitted_required_checks:
        omitted_optional_checks = sum(len(group) for group in optional_groups)
    else:
        for group in optional_groups:
            if len(plan) + len(group) > MAX_GITNEXUS_CHECKS:
                omitted_optional_checks += len(group)
                continue
            plan.extend(group)
    return plan, omitted_optional_checks, omitted_required_checks


semantic_summary_section = workflow_index.semantic_summary


def render_coverage_plan_lines(plan: object, indent: str) -> list[str]:
    plan = plan if isinstance(plan, dict) else {}
    areas = plan.get("areas")
    areas = areas if isinstance(areas, list) else []
    lines = [
        f"{indent}<coverage_plan required=\"{str(bool(plan.get('required'))).lower()}\" "
        f"delegation_required=\"{str(bool(plan.get('delegation_required'))).lower()}\">"
    ]
    for step in CRITICAL_AREA_STEPS:
        lines.append(f"{indent}  <step>{html.escape(step)}</step>")
    for area in areas:
        if not isinstance(area, dict):
            continue
        lines.append(
            f"{indent}  <area id=\"{html.escape(str(area.get('id') or ''))}\" "
            f"kind=\"{html.escape(str(area.get('kind') or ''))}\" "
            f"required=\"{str(bool(area.get('required'))).lower()}\">"
        )
        lines.append(f"{indent}    <why>{html.escape(str(area.get('why') or ''))}</why>")
        lines.append(
            f"{indent}    <must_answer>{html.escape(str(area.get('must_answer') or ''))}</must_answer>"
        )
        lines.append(f"{indent}    <files>")
        files = area.get("files")
        if isinstance(files, list):
            for path in files:
                lines.append(f"{indent}      <file path=\"{html.escape(str(path))}\"/>")
        lines.append(f"{indent}    </files>")
        lines.append(f"{indent}  </area>")
    lines.append(f"{indent}</coverage_plan>")
    return lines


def _omissions_are_blocking(analysis: dict[str, object]) -> bool:
    unresolved = analysis.get("unresolved_checks")
    return isinstance(unresolved, list) and any(
        isinstance(item, dict) and item.get("status") == "omitted"
        for item in unresolved
    )


def render_gitnexus_analysis_lines(gitnexus: dict[str, object], indent: str) -> list[str]:
    analysis = gitnexus.get("analysis")
    if not isinstance(analysis, dict):
        return []
    omissions_blocking = _omissions_are_blocking(analysis)
    lines = [
        f'{indent}<gitnexus_analysis status="{html.escape(str(analysis.get("status") or "unknown"))}" '
        f'graph_calls="{html.escape(str(analysis.get("graph_call_count") or 0))}" '
        f'omitted_checks="{html.escape(str(analysis.get("omitted_check_count") or 0))}" '
        f'omissions_blocking="{str(omissions_blocking).lower()}" '
        f'elapsed_ms="{html.escape(str(analysis.get("elapsed_ms") or 0))}" '
        f'output_bytes="{html.escape(str(analysis.get("output_bytes") or 0))}">'
    ]
    entries = analysis.get("entries")
    if isinstance(entries, list):
        for entry in entries[:MAX_GITNEXUS_CHECKS]:
            if not isinstance(entry, dict):
                continue
            attrs = " ".join(
                f'{name}="{html.escape(str(entry.get(name) or ""))}"'
                for name in ("kind", "file", "target", "direction", "status", "resolved_identity")
            )
            lines.append(f"{indent}  <check {attrs}>")
            for key in ("callers", "references"):
                facts = entry.get(key)
                if isinstance(facts, list):
                    for fact in facts[:5]:
                        if isinstance(fact, dict):
                            lines.append(
                                f'{indent}    <{key[:-1]} identity="{html.escape(str(fact.get("identity") or ""))}" '
                                f'file="{html.escape(str(fact.get("file") or ""))}"/>'
                            )
            impacted = entry.get("impacted_files")
            if isinstance(impacted, list):
                for path in impacted[:5]:
                    lines.append(f'{indent}    <impacted_file path="{html.escape(str(path))}"/>')
            if entry.get("diagnostic"):
                lines.append(
                    f"{indent}    <diagnostic>{html.escape(str(entry['diagnostic']))}</diagnostic>"
                )
            lines.append(f"{indent}  </check>")
    lines.append(f"{indent}</gitnexus_analysis>")
    return lines


def build_gitnexus_section(
    plan: list[dict[str, object]],
    target_state: TargetState,
    repo_name: str | None,
    status: dict[str, object] | None = None,
) -> dict[str, object]:
    section = {
        "status": "planned",
        "repo": repo_name or target_state.source_repo.name,
        "expected_repo_path": str(target_state.analysis_repo),
        "expected_head_sha": target_state.head_sha,
        "stale_index_policy": "block_or_reindex_before_trusting_impact",
        "plan": plan,
    }
    if status:
        section.update(status)
    return section


def analysis_repo_is_cache_owned(path: Path, cache_dir: Path) -> bool:
    resolved = path.resolve()
    cache_root = cache_dir.resolve()
    return resolved == cache_root or cache_root in resolved.parents


def soulforge_target_metadata(
    target_state: TargetState,
    build_result: MapBuildResult,
    soul_map: SoulForgeMap,
    cache_dir: Path,
) -> dict[str, object]:
    analysis_head = run_git(target_state.analysis_repo, ["rev-parse", "HEAD"], allow_fail=True)
    db_exists = soul_map.db_path.exists()
    db_mtime = soul_map.db_path.stat().st_mtime if db_exists else None
    head_matches = analysis_head == target_state.head_sha
    if build_result.warning:
        status = "failed" if build_result.attempted else "missing"
    elif db_exists and head_matches:
        status = "fresh"
    elif not db_exists:
        status = "missing"
    else:
        status = "unknown"
    return {
        "status": status,
        "target_head_verified": status == "fresh",
        "source_head_sha": target_state.head_sha,
        "analysis_head_sha": analysis_head,
        "analysis_repo_is_cache_owned": analysis_repo_is_cache_owned(
            target_state.analysis_repo,
            cache_dir,
        ),
        "analysis_head_matches_source_head": head_matches,
        "db_path": str(soul_map.db_path),
        "db_exists": db_exists,
        "db_mtime": db_mtime,
        "build_attempted": build_result.attempted,
        "build_returncode": build_result.returncode,
    }


def producer_revision() -> dict[str, object]:
    source = Path(__file__).resolve().parent
    if not is_git_repo(source):
        return {"commit": "unknown", "dirty": False}
    root = repo_root(source)
    return {
        "commit": run_git(root, ["rev-parse", "HEAD"]),
        "dirty": is_dirty(root, ignore_tool_cache=True),
    }


def apply_gitnexus_findings(
    packet: dict[str, object],
    findings: dict[str, object],
) -> dict[str, object]:
    gitnexus = packet.get("gitnexus")
    if not isinstance(gitnexus, dict):
        raise RuntimeError("packet missing gitnexus section")
    warnings = packet.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
        packet["warnings"] = warnings

    stale = bool(findings.get("stale_index"))
    unavailable = bool(findings.get("unavailable"))
    if stale:
        warnings.append("GitNexus index is stale; blast-radius claims are blocked")
    if unavailable:
        warnings.append("GitNexus is unavailable; blast-radius claims are blocked")

    confirmed = set(str(item) for item in findings.get("confirmed_files", []) if str(item))
    missing = set(str(item) for item in findings.get("missing_files", []) if str(item))
    impacted = set(str(item) for item in findings.get("impacted_files", []) if str(item))
    targets = packet.get("targets")
    if isinstance(targets, list):
        known = {
            str(target.get("path"))
            for target in targets
            if isinstance(target, dict) and target.get("path")
        }
        for target in targets:
            if not isinstance(target, dict):
                continue
            path = str(target.get("path") or "")
            signals = target.get("rank_signals")
            if not isinstance(signals, list):
                signals = []
            reasons = target.get("why_selected")
            if not isinstance(reasons, list):
                reasons = []
            if path in confirmed:
                signals.append("gitnexus_confirmed")
                reasons.append("confirmed by GitNexus impact/context")
            if path in missing:
                signals.append("gitnexus_missing")
                reasons.append("not confirmed by GitNexus impact/context")
            target["rank_signals"] = unique_ordered(str(item) for item in signals)
            target["why_selected"] = unique_ordered(str(item) for item in reasons)
        absent_impacts = sorted(impacted - known)
        if absent_impacts:
            warnings.append(
                "GitNexus found impacted files absent from map packet: "
                + ", ".join(absent_impacts[:10])
            )
            gitnexus["absent_impacted_files"] = absent_impacts

    gitnexus["status"] = "blocked" if stale or unavailable else "merged"
    gitnexus["findings"] = findings
    return packet


def make_blocker_packet(
    repo: Path,
    *,
    reason: str,
    base_ref: str | None,
    head_ref: str,
    suggestions: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    source_repo = repo_root(repo)
    head_sha = run_git(source_repo, ["rev-parse", head_ref], allow_fail=True)
    git_state = read_git_state(source_repo, base_ref or head_ref, head_ref)
    packet = {
        "schema_version": 1,
        "blocked": True,
        "blocker": {
            "reason": reason,
            "worktree_suggestions": suggestions or [],
        },
        "repo": str(source_repo),
        "mode": "blocked",
        "scope": "blocked",
        "intent": None,
        "token_budget": DEFAULT_TOKEN_BUDGET,
        "warnings": [reason],
        "target_state": {
            "source_repo": str(source_repo),
            "analysis_repo": str(source_repo),
            "base_ref": base_ref or "",
            "head_ref": head_ref,
            "head_sha": head_sha,
            "source_dirty": is_dirty(source_repo, ignore_tool_cache=True),
            "target_dirty": is_dirty(source_repo, ignore_tool_cache=True),
            "cache_key": None,
            "detached": is_detached(source_repo),
        },
        "git": {
            "branch": git_state.branch,
            "head": git_state.head,
            "base_ref": git_state.base_ref,
            "merge_base": git_state.merge_base,
            "pr_files": git_state.pr_files,
            "staged_files": git_state.staged_files,
            "unstaged_files": git_state.unstaged_files,
            "untracked_files": git_state.untracked_files,
        },
        "soulforge": {
            "build": {
                "attempted": False,
                "command": [],
                "returncode": None,
                "warning": reason,
            },
            "stats": {"available": False, "path": str(source_repo / ".soulforge" / "repomap.db")},
            "top_files": [],
        },
        "targets": [],
        "gitnexus": build_gitnexus_section([], TargetState(
            mode="local",
            source_repo=source_repo,
            analysis_repo=source_repo,
            base_ref=base_ref or "",
            head_ref=head_ref,
            head_sha=head_sha,
            source_dirty=is_dirty(source_repo, ignore_tool_cache=True),
            target_dirty=is_dirty(source_repo, ignore_tool_cache=True),
            cache_key=None,
        ), source_repo.name),
        "gitnexus_plan": [],
    }
    packet["advisorProjection"] = build_advisor_projection(packet)
    return packet


def canonical_repo_identity(repo: Path) -> str | None:
    remotes = sorted(split_lines(run_git(repo, ["remote"], allow_fail=True)))
    if not remotes:
        return None
    remote = "origin" if "origin" in remotes else remotes[0]
    url = run_git(repo, ["remote", "get-url", remote], allow_fail=True)
    scp_match = re.fullmatch(r"[^@]+@([^:]+):(.+)", url)
    if scp_match:
        host, path = scp_match.groups()
    else:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return None
        host, path = parsed.hostname, parsed.path
    normalized_path = path.strip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    return f"{host}/{normalized_path}" if normalized_path else None


def build_advisor_projection(packet: dict[str, object]) -> dict[str, object]:
    gitnexus = packet.get("gitnexus")
    gitnexus = gitnexus if isinstance(gitnexus, dict) else {}
    analysis = gitnexus.get("analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    entries = analysis.get("entries")
    entries = entries if isinstance(entries, list) else []
    references = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        reference = f"gitnexus.analysis.entries[{index}]"
        entry["reference"] = reference
        references.append(reference)
    unresolved = analysis.get("unresolved_checks")
    unresolved = unresolved if isinstance(unresolved, list) else []
    required_omissions = [
        dict(item)
        for item in unresolved
        if isinstance(item, dict) and item.get("status") == "omitted"
    ]
    target_state = packet.get("target_state")
    target_state = target_state if isinstance(target_state, dict) else {}
    git = packet.get("git")
    git = git if isinstance(git, dict) else {}
    targets = packet.get("targets")
    targets = targets if isinstance(targets, list) else []
    source_identity = canonical_repo_identity(Path(str(packet.get("repo") or ".")))
    source_base = git.get("merge_base")
    coverage_gaps = [
        dict(gap)
        for gap in packet.get("coverage_gaps", [])
        if isinstance(gap, dict)
    ]
    if not source_identity:
        coverage_gaps.append({"kind": "source_repo_unavailable"})
    if not source_base:
        coverage_gaps.append({"kind": "source_base_unavailable"})
    expected_candidate = target_state.get("candidate_tree")
    if not expected_candidate:
        expected_candidate = {"gap": "expected_candidate_tree_unavailable"}
        coverage_gaps.append({"kind": "expected_candidate_tree_unavailable"})
    indexed_candidate = gitnexus.get("indexed_candidate_tree")
    if not indexed_candidate:
        indexed_candidate = {"gap": "indexed_candidate_tree_unavailable"}
        coverage_gaps.append({"kind": "indexed_candidate_tree_unavailable"})
    graph_status = (
        "disabled"
        if gitnexus.get("status") == "disabled"
        else analysis.get("status") or gitnexus.get("status") or "unknown"
    )
    return {
        "schemaVersion": 1,
        "producerRevision": analysis.get("producer_revision") or producer_revision(),
        "sourceRepo": source_identity or {"gap": "source_repo_unavailable"},
        "sourceBaseOid": source_base or {"gap": "source_base_unavailable"},
        "committedHeadOid": target_state.get("head_sha") or "",
        "expectedCandidateTree": expected_candidate,
        "indexedCandidateTree": indexed_candidate,
        "targets": [dict(target) for target in targets if isinstance(target, dict)],
        "graph": {
            "status": graph_status,
            "references": references,
            "requiredOmissions": required_omissions,
            "optionalOmissionCount": int(analysis.get("omitted_check_count") or 0),
        },
        "coverageGaps": coverage_gaps,
    }


def candidate_analysis_transaction(function):
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        source_repo = repo_root(Path(arguments["repo"]))
        head_sha = run_git(source_repo, ["rev-parse", str(arguments["head_ref"])])
        analysis_repo, _key = analysis_checkout_path(
            source_repo, head_sha, Path(arguments["cache_dir"]), arguments["mode"])
        with gitnexus_analysis.analysis_transaction(
            gitnexus_analysis.analysis_transaction_path(analysis_repo)
        ):
            return function(*args, **kwargs)

    return wrapped


@candidate_analysis_transaction
def make_packet(
    repo: Path,
    *,
    mode: Mode,
    base_ref: str,
    head_ref: str,
    intent: str | None,
    top: int,
    token_budget: int,
    cache_dir: Path,
    soulforge_bin: str | None,
    map_build: MapBuildMode,
    map_timeout_ms: int,
    allow_missing_map: bool,
    gitnexus_repo: str | None,
    task_state: dict[str, object] | None = None,
    gitnexus_mode: GitNexusMode = "off",
) -> dict[str, object]:
    source_repo = repo_root(repo)
    source_status_before = porcelain_status(source_repo)
    target_state = resolve_target_state(repo, mode, base_ref, head_ref, cache_dir)
    expected_candidate_tree = candidate_tree(target_state.source_repo)
    analysis_candidate_tree = candidate_tree(target_state.analysis_repo)
    if analysis_candidate_tree != expected_candidate_tree:
        raise RuntimeError("analysis checkout does not match the source candidate tree")
    if target_state.cache_key is None:
        raise RuntimeError("packet analysis checkout must be cache-owned")
    require_cache_path(target_state.analysis_repo, cache_dir)
    gitignore_dirty_before_build = ".gitignore" in dirty_paths(target_state.analysis_repo)
    build_result = build_soulforge_map(
        target_state.analysis_repo,
        soulforge_bin,
        map_build,
        map_timeout_ms,
    )
    can_cleanup_analysis = target_state.analysis_repo.resolve() != target_state.source_repo.resolve()
    if can_cleanup_analysis and (mode == "pr" or not gitignore_dirty_before_build):
        cleanup_soulforge_gitignore_change(target_state.analysis_repo)
    native_index = workflow_index.WorkflowIndex(target_state.analysis_repo, file_role)
    native_index.ensure_current(
        source_worktree_files(target_state.analysis_repo),
        head_sha=target_state.head_sha,
        dirty_overlay=target_state.target_dirty,
        summary_for_symbol=synthetic_symbol_summary,
        reuse=not target_state.source_dirty and not target_state.target_dirty,
    )
    soul_map = SoulForgeMap(target_state.analysis_repo, native_index)
    # Standalone analyze stays SoulForge-strict; Codex bootstrap explicitly opts into native fallback.
    if not soul_map.available and not allow_missing_map:
        raise RuntimeError(
            "SoulForge map is required but unavailable for "
            f"{target_state.analysis_repo}. Use --allow-missing-map to continue without it."
        )
    soulforge_target = soulforge_target_metadata(
        target_state,
        build_result,
        soul_map,
        cache_dir,
    )

    source_git_state = read_git_state(target_state.source_repo, base_ref, head_ref)
    reference_only = reference_only_prefixes(target_state.source_repo)
    intent_resolution = (
        native_index.resolve_intent(intent or "")
        if mode == "intent" else workflow_index.IntentResolution((), (), (), ())
    )
    targets = target_files_for_mode(
        mode,
        source_git_state,
        soul_map,
        intent,
        top,
        reference_only,
        intent_resolution,
    )
    if mode == "intent":
        targets = unique_ordered([
            *(match.path for match in intent_resolution.required_symbols),
            *targets,
        ])
    target_entries = make_target_entries(
        mode=mode,
        source_repo=target_state.source_repo,
        analysis_repo=target_state.analysis_repo,
        base_ref=base_ref,
        head_ref=head_ref,
        source_git_state=source_git_state,
        soul_map=soul_map,
        targets=targets,
        intent=intent,
        intent_resolution=intent_resolution,
    )
    target_entries = apply_task_state_to_entries(target_entries, task_state)
    coverage_plan = build_coverage_plan(target_entries)
    gitnexus_status = gitnexus_analysis.ensure_index(
        target_state.analysis_repo,
        target_state.head_sha,
        gitnexus_repo,
        gitnexus_mode,
        run_command=run_cmd,
        expected_candidate_tree=expected_candidate_tree,
    )
    index_freshness_status = str(gitnexus_status.get("status") or "unknown")
    gitnexus_repo_name = str(gitnexus_status.get("repo") or gitnexus_repo or target_state.analysis_repo.name)
    graph_targets = [
        entry for entry in target_entries
        if (target_state.analysis_repo / str(entry["path"])).exists()
        or (target_state.analysis_repo / str(entry["path"])).is_symlink()
    ]
    unavailable_targets = [entry for entry in target_entries if entry not in graph_targets]
    plan, omitted_check_count, omitted_required_checks = build_gitnexus_plan(
        graph_targets, gitnexus_repo_name, intent_mode=mode == "intent")
    unavailable_plan, unavailable_omitted, unavailable_required = build_gitnexus_plan(
        unavailable_targets, gitnexus_repo_name, intent_mode=mode == "intent")
    omitted_check_count += unavailable_omitted
    for item in unavailable_plan:
        if item.get("required") is True:
            omitted_required_checks.append(item)
        else:
            omitted_check_count += 1
    omitted_required_checks.extend(unavailable_required)
    gitnexus_status = gitnexus_analysis.execute(
        plan,
        gitnexus_status,
        run_command=run_cmd,
        omitted_check_count=omitted_check_count,
        omitted_required_checks=omitted_required_checks,
    )
    coverage_gaps = [
        {
            "kind": gap.kind,
            "reference": gap.reference,
            "candidates": list(gap.candidates),
        }
        for gap in intent_resolution.coverage_gaps
    ]
    analysis = gitnexus_status.get("analysis")
    if isinstance(analysis, dict):
        if coverage_gaps:
            unresolved = analysis.get("unresolved_checks")
            if isinstance(unresolved, list):
                unresolved.extend(
                    {**gap, "status": "coverage_gap"}
                    for gap in coverage_gaps
                )
            analysis["status"] = "blocked"
            gitnexus_status.update(
                status="blocked",
                required_checks_resolved=False,
                warning="intent coverage gaps block semantic analysis",
            )
        analysis["authority"] = {
            "source_repository": str(target_state.source_repo),
            "analysis_repository": str(target_state.analysis_repo),
            "expected_head": target_state.head_sha,
            "indexed_head": str(gitnexus_status.get("indexed_head_sha") or ""),
            "gitnexus_repository": gitnexus_repo_name,
            "freshness_status": index_freshness_status,
        }
        analysis["producer_revision"] = producer_revision()
        if gitnexus_mode != "off" and gitnexus_analysis.result_is_resolved(analysis):
            cleanup_soulforge_gitignore_change(target_state.analysis_repo)
            if candidate_tree(target_state.analysis_repo) == expected_candidate_tree:
                gitnexus_status = gitnexus_analysis.publish_receipt(
                    gitnexus_status,
                    target_state.analysis_repo,
                    target_state.head_sha,
                    expected_candidate_tree,
                    run_command=run_cmd,
                )
            if not gitnexus_status.get("index_fresh"):
                analysis["status"] = "blocked"
                unresolved = analysis.get("unresolved_checks")
                if isinstance(unresolved, list):
                    unresolved.append({
                        "kind": "candidate_receipt",
                        "file": str(target_state.analysis_repo),
                        "target": expected_candidate_tree,
                        "status": "unresolved",
                        "diagnostic": str(
                            gitnexus_status.get("warning")
                            or "candidate changed before receipt publication"
                        ),
                    })
                gitnexus_status["required_checks_resolved"] = False
    source_status = source_status_proof(
        source_status_before,
        porcelain_status(target_state.source_repo),
    )

    warnings = []
    if build_result.warning:
        warnings.append(build_result.warning)
    if not soulforge_target.get("target_head_verified"):
        warnings.append("SoulForge target head could not be verified")
    gitnexus_warning = gitnexus_status.get("warning")
    if gitnexus_warning:
        warnings.append(str(gitnexus_warning))
    if mode == "pr" and target_state.source_dirty:
        warnings.append("source worktree is dirty; PR target map was built from clean cached checkout")
    if mode != "pr" and target_state.target_dirty:
        warnings.append("analysis uses dirty local files copied into cached checkout by design")
    if mode == "intent" and not targets:
        warnings.append("intent mode found no targets; refine --intent or build a map")
    if mode == "repo" and not targets:
        warnings.append("repo mode found no targets; build a SoulForge map or allow map fallback")
    if not source_status["unchanged"]:
        warnings.append("source checkout status changed during context generation")

    packet_target_entries = [
        {key: value for key, value in entry.items() if key != "_planner_symbols"}
        for entry in target_entries
    ]
    packet = {
        "schema_version": 1,
        "repo": str(target_state.source_repo),
        "mode": mode,
        "scope": "pr" if mode == "pr" else ("dirty" if mode == "local" else mode),
        "intent": intent,
        "task_state": task_state,
        "token_budget": token_budget,
        "warnings": warnings,
        "target_state": {
            "source_repo": str(target_state.source_repo),
            "analysis_repo": str(target_state.analysis_repo),
            "base_ref": base_ref,
            "head_ref": head_ref,
            "head_sha": target_state.head_sha,
            "candidate_tree": expected_candidate_tree,
            "source_dirty": target_state.source_dirty,
            "target_dirty": target_state.target_dirty,
            "cache_key": target_state.cache_key,
            "analysis_head_sha": soulforge_target["analysis_head_sha"],
            "analysis_repo_is_cache_owned": soulforge_target["analysis_repo_is_cache_owned"],
            "analysis_head_matches_source_head": soulforge_target["analysis_head_matches_source_head"],
            "source_status_unchanged": source_status["unchanged"],
        },
        "policy": {
            "reference_only_prefixes": reference_only,
        },
        "source_status": source_status,
        "workflow_index": native_index.status(),
        "git": {
            "branch": source_git_state.branch,
            "head": source_git_state.head,
            "base_ref": source_git_state.base_ref,
            "merge_base": source_git_state.merge_base,
            "pr_files": source_git_state.pr_files,
            "staged_files": source_git_state.staged_files,
            "unstaged_files": source_git_state.unstaged_files,
            "untracked_files": source_git_state.untracked_files,
        },
        "soulforge": {
            "build": {
                "attempted": build_result.attempted,
                "command": build_result.command,
                "returncode": build_result.returncode,
                "warning": build_result.warning,
            },
            "stats": soul_map.stats(),
            "top_files": [entry.__dict__ for entry in soul_map.top_files(top)],
            "target": soulforge_target,
        },
        "semantic_summaries": semantic_summary_section(target_entries),
        "architecture_summary": workflow_index.summarize_architecture(target_entries),
        "coverage_plan": coverage_plan,
        "coverage_gaps": coverage_gaps,
        "targets": packet_target_entries,
        "gitnexus": build_gitnexus_section(plan, target_state, gitnexus_repo_name, gitnexus_status),
        "gitnexus_plan": plan,
    }
    packet["advisorProjection"] = build_advisor_projection(packet)
    if gitnexus_mode != "off" and not gitnexus_analysis.result_is_resolved(analysis):
        packet["blocked"] = True
        packet["blocker"] = {
            "reason": str(
                gitnexus_status.get("warning")
                or "GitNexus semantic analysis is unresolved"
            ),
            "worktree_suggestions": [],
        }
    return packet


def render_target_lines(target: dict[str, object], *, max_symbols: int = 8) -> list[str]:
    lines = []
    rank = target["rank"] if target["rank"] is not None else "not indexed"
    lines.append(f"### {target['path']}")
    lines.append(f"- map rank: {rank}")
    if target.get("surface_role"):
        lines.append(f"- role: {target['surface_role']}")
    if target.get("priority_score") is not None:
        lines.append(f"- priority score: {target['priority_score']}")
    rank_signals = target.get("rank_signals")
    if isinstance(rank_signals, list) and rank_signals:
        lines.append(f"- rank signals: {', '.join(str(item) for item in rank_signals)}")
    why_selected = target.get("why_selected")
    if isinstance(why_selected, list) and why_selected:
        lines.append(f"- why selected: {'; '.join(str(item) for item in why_selected)}")
    if target.get("dependent_count") is not None:
        lines.append(f"- dependent count: {target['dependent_count']}")
    impact = target.get("soulforge_impact")
    if isinstance(impact, dict):
        lines.append(
            "- SoulForge impact: "
            f"{impact.get('risk', 'unknown')} risk; "
            f"{impact.get('direct_dependents', 0)} direct dependents; "
            f"{impact.get('total_affected_scope', 0)} total affected; "
            f"{impact.get('cochange_partners', 0)} co-change partners"
        )
        symbols_at_risk = impact.get("exported_symbols_at_risk")
        if isinstance(symbols_at_risk, list) and symbols_at_risk:
            lines.append("- exported symbols at risk:")
            for symbol in symbols_at_risk[:5]:
                if isinstance(symbol, dict):
                    lines.append(
                        f"  - `{symbol.get('name')}` "
                        f"used by {symbol.get('usage_files', 0)} files"
                    )
    dirty_kinds = target.get("dirty_kinds")
    if isinstance(dirty_kinds, list) and dirty_kinds:
        lines.append(f"- source dirty overlap: {', '.join(str(item) for item in dirty_kinds)}")
    if target.get("scope_contaminated"):
        lines.append("- warning: analysis intentionally uses dirty local state")
    if target.get("symbol_count") is not None:
        lines.append(f"- symbols: {target['symbol_count']}")
    ranges = target.get("changed_ranges")
    if isinstance(ranges, list) and ranges:
        rendered_ranges = ", ".join(
            f"{start}-{end}" if start != end else str(start)
            for start, end in ranges[:8]
            if isinstance(start, int) and isinstance(end, int)
        )
        lines.append(f"- changed lines: {rendered_ranges}")
    changed_symbols = target.get("changed_symbols")
    if isinstance(changed_symbols, list) and changed_symbols:
        lines.append("- changed symbols:")
        for symbol in changed_symbols[:max_symbols]:
            if not isinstance(symbol, dict):
                continue
            signature = symbol.get("signature") or f"{symbol.get('kind')} {symbol.get('name')}"
            lines.append(f"  - `{signature}` lines {symbol.get('line')}-{symbol.get('end_line')}")
    symbols = target.get("symbols")
    if isinstance(symbols, list) and symbols:
        lines.append("- prompt symbols:")
        for symbol in symbols[:max_symbols]:
            if not isinstance(symbol, dict):
                continue
            prefix = "+" if symbol.get("is_exported") else " "
            signature = symbol.get("signature") or f"{symbol.get('kind')} {symbol.get('name')}"
            lines.append(f"  - `{prefix}{signature}` line {symbol.get('line')}")
            if symbol.get("summary"):
                source = symbol.get("summary_source") or "unknown"
                lines.append(f"    - summary ({source}): {symbol['summary']}")
    graph_neighbors = target.get("graph_neighbors")
    if isinstance(graph_neighbors, list) and graph_neighbors:
        lines.append("- graph neighbors:")
        for neighbor in graph_neighbors[:5]:
            if isinstance(neighbor, dict):
                lines.append(f"  - `{neighbor['path']}` ({neighbor['weight']})")
    cochanges = target.get("cochanges")
    if isinstance(cochanges, list) and cochanges:
        lines.append("- co-change partners:")
        for partner in cochanges[:5]:
            if isinstance(partner, dict):
                lines.append(f"  - `{partner['path']}` ({partner['count']})")
    return lines


def render_markdown(packet: dict[str, object]) -> str:
    git = packet["git"]
    soulforge = packet["soulforge"]
    target_state = packet["target_state"]
    gitnexus = packet["gitnexus"]
    workflow = packet.get("workflow_index") or {}
    semantic = packet.get("semantic_summaries") or {}
    coverage_plan = packet.get("coverage_plan") or {}
    source_status = packet.get("source_status") or {}
    policy = packet.get("policy") or {}
    assert isinstance(git, dict)
    assert isinstance(soulforge, dict)
    assert isinstance(target_state, dict)
    assert isinstance(gitnexus, dict)
    assert isinstance(workflow, dict)
    assert isinstance(semantic, dict)
    assert isinstance(source_status, dict)
    assert isinstance(policy, dict)
    lines = [
        "# Repo Context Packet",
        "",
        f"- repo: `{packet['repo']}`",
        f"- mode: `{packet['mode']}`",
        f"- branch: `{git['branch']}`",
        f"- source head: `{git['head']}`",
        f"- target head sha: `{target_state['head_sha']}`",
        f"- base: `{git['base_ref']}`",
        f"- analysis repo: `{target_state['analysis_repo']}`",
        f"- analysis head: `{target_state.get('analysis_head_sha', '')}`",
        f"- analysis cache-owned: `{target_state.get('analysis_repo_is_cache_owned', False)}`",
        f"- source status unchanged: `{source_status.get('unchanged', 'unknown')}`",
        f"- workflow index: `{workflow.get('db_path', '')}`",
        f"- workflow index files: `{workflow.get('files', 0)}`",
        f"- workflow index symbols: `{workflow.get('symbols', 0)}`",
        f"- reference-only prefixes: `{', '.join(str(item) for item in policy.get('reference_only_prefixes', []))}`",
        "",
        "## Warnings",
        "",
    ]
    warnings = packet.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Git Scope",
            "",
            f"- PR files: {len(git['pr_files'])}",
            f"- staged files: {len(git['staged_files'])}",
            f"- unstaged files: {len(git['unstaged_files'])}",
            f"- untracked files: {len(git['untracked_files'])}",
            f"- source status before hash: `{source_status.get('before_hash', '')}`",
            f"- source status after hash: `{source_status.get('after_hash', '')}`",
            f"- source status added paths: {len(source_status.get('added_paths', []))}",
            f"- source tool-cache paths existed before: {len(source_status.get('tool_cache_paths_before', []))}",
            f"- source tool-cache paths created: {len(source_status.get('tool_cache_paths_created', []))}",
            "",
            "## SoulForge Map",
            "",
        ]
    )
    stats = soulforge["stats"]
    soulforge_target = soulforge.get("target")
    assert isinstance(stats, dict)
    if isinstance(soulforge_target, dict):
        lines.append(f"- status: `{soulforge_target.get('status')}`")
        lines.append(f"- target head verified: `{soulforge_target.get('target_head_verified')}`")
        lines.append(f"- db: `{soulforge_target.get('db_path')}`")
    if stats.get("available"):
        for key in ("files", "symbols", "edges", "refs", "cochanges", "calls", "summaries"):
            if key in stats:
                lines.append(f"- {key}: {stats[key]}")
    else:
        lines.append(f"- unavailable: `{stats['path']}`")
    lines.extend(
        [
            f"- semantic mode: `{semantic.get('mode', 'unknown')}`",
            f"- semantic synthetic fill: `{semantic.get('synthetic_fill', False)}`",
            f"- semantic live LLM generation: `{semantic.get('live_llm_generation', False)}`",
        ]
    )

    lines.extend(["", "## Coverage Plan", ""])
    if isinstance(coverage_plan, dict):
        lines.append(f"- required: `{coverage_plan.get('required', False)}`")
        lines.append("- delegated agent required: `False`")
        areas = coverage_plan.get("areas")
        if isinstance(areas, list) and areas:
            for area in areas:
                if isinstance(area, dict):
                    files = area.get("files")
                    file_text = ", ".join(str(path) for path in files) if isinstance(files, list) else ""
                    lines.append(f"- {area.get('id')}: {file_text}")
        else:
            lines.append("- areas: none")

    lines.extend(["", "## Targets", ""])
    targets = packet["targets"]
    assert isinstance(targets, list)
    for target in targets:
        assert isinstance(target, dict)
        lines.extend(render_target_lines(target))
        lines.append("")

    lines.extend(["## GitNexus Required Checks", ""])
    lines.append(f"- status: `{gitnexus.get('status')}`")
    lines.append(f"- repo: `{gitnexus.get('repo')}`")
    lines.append(f"- expected head: `{gitnexus.get('expected_head_sha')}`")
    if gitnexus.get("indexed_head_sha") is not None:
        lines.append(f"- indexed head: `{gitnexus.get('indexed_head_sha')}`")
    if gitnexus.get("reindex_attempted") is not None:
        lines.append(f"- reindex attempted: `{gitnexus.get('reindex_attempted')}`")
    if gitnexus.get("required_checks_resolved") is not None:
        lines.append(f"- required checks resolved: `{gitnexus.get('required_checks_resolved')}`")
    missing_symbols = gitnexus.get("missing_required_symbols")
    if isinstance(missing_symbols, list) and missing_symbols:
        lines.append(f"- missing required symbols: {', '.join(str(item) for item in missing_symbols)}")
    lines.append("")
    plan = gitnexus.get("plan")
    assert isinstance(plan, list)
    if not plan:
        lines.append("- No symbol-level GitNexus targets were found.")
    for item in plan:
        assert isinstance(item, dict)
        if item["kind"] == "symbol_impact":
            lines.append(f"- impact upstream: `{item['target']}`")
        elif item["kind"] == "symbol_context":
            lines.append(f"- context: `{item['target']}` in `{item['file']}`")
        else:
            lines.append(f"- file context: `{item['target']}`")
    return "\n".join(lines) + "\n"


def render_context_digest_lines(packet: dict[str, object], *, indent: str = "  ") -> list[str]:
    return workflow_index.context_digest_lines(packet, indent, DEFAULT_TOKEN_BUDGET)


def semantic_source_counts_text(semantic: dict[str, object]) -> str:
    source_counts = semantic.get("source_counts")
    if not isinstance(source_counts, dict) or not source_counts:
        return "none"
    return ", ".join(f"{source}={count}" for source, count in source_counts.items())


def render_required_intake(packet: dict[str, object]) -> str:
    target_state = packet.get("target_state") or {}
    gitnexus = packet.get("gitnexus") or {}
    workflow = packet.get("workflow_index") or {}
    semantic = packet.get("semantic_summaries") or {}
    targets = packet.get("targets") or []
    assert isinstance(target_state, dict)
    assert isinstance(gitnexus, dict)
    assert isinstance(workflow, dict)
    assert isinstance(semantic, dict)
    assert isinstance(targets, list)
    architecture = packet.get("architecture_summary") or workflow_index.summarize_architecture(
        [target for target in targets if isinstance(target, dict)]
    )

    lines = [
        "REPO_CONTEXT_FORGE_REQUIRED_INTAKE",
        f"mode: {packet.get('mode') or 'unknown'}",
        f"head_sha: {target_state.get('head_sha') or ''}",
        f"token_budget: {packet.get('token_budget') or DEFAULT_TOKEN_BUDGET}",
        (
            "workflow_index: "
            f"available={str(workflow.get('available', False)).lower()}; "
            f"head_sha={workflow.get('head_sha') or ''}; "
            f"dirty_overlay={str(workflow.get('dirty_overlay', False)).lower()}; "
            f"files={workflow.get('files') or 0}; "
            f"symbols={workflow.get('symbols') or 0}"
        ),
        (
            "semantic: "
            f"{semantic.get('mode') or 'unknown'}; "
            f"live_llm={str(semantic.get('live_llm_generation', False)).lower()}; "
            f"sources={semantic_source_counts_text(semantic)}"
        ),
        (
            "gitnexus: authority=packet; "
            f"repo={gitnexus.get('repo') or ''}; "
            f"status={gitnexus.get('status') or 'unknown'}; "
            f"expected_head_sha={gitnexus.get('expected_head_sha') or ''}; "
            f"indexed_head_sha={gitnexus.get('indexed_head_sha') or ''}; "
            f"required_checks_resolved={str(gitnexus.get('required_checks_resolved', False)).lower()}"
        ),
    ]
    analysis = gitnexus.get("analysis")
    if isinstance(analysis, dict):
        unresolved = analysis.get("unresolved_checks")
        unresolved_items = unresolved if isinstance(unresolved, list) else []
        omissions_blocking = _omissions_are_blocking(analysis)
        lines.append(
            "gitnexus_analysis: "
            f"status={analysis.get('status') or 'unknown'}; "
            f"graph_calls={analysis.get('graph_call_count') or 0}; "
            f"omitted_checks={analysis.get('omitted_check_count') or 0}; "
            f"omissions_blocking={str(omissions_blocking).lower()}; "
            f"unresolved={len(unresolved_items)}; "
            f"elapsed_ms={analysis.get('elapsed_ms') or 0}"
        )
    lines.append(
        "gitnexus_tool_discovery: run tool_search for missing follow-up GitNexus capabilities before declaring context, impact, or detect_changes unavailable."
    )
    lines.extend(workflow_index.architecture_lines(architecture))
    lines.append("top_targets:")
    for target in targets[:5]:
        if not isinstance(target, dict):
            continue
        impact = target.get("soulforge_impact")
        impact = impact if isinstance(impact, dict) else {}
        signals = target.get("rank_signals")
        signal_text = ",".join(str(item) for item in signals) if isinstance(signals, list) else ""
        lines.append(
            "- "
            f"{target.get('path') or ''} | "
            f"score={target.get('priority_score') or 0} | "
            f"role={target.get('surface_role') or 'unknown'} | "
            f"risk={impact.get('risk') or 'unknown'} | "
            f"direct_dependents={impact.get('direct_dependents') or 0} | "
            f"signals={signal_text}"
        )
    if not any(isinstance(target, dict) for target in targets[:5]):
        lines.append("- none")
    coverage_plan = packet.get("coverage_plan")
    coverage_plan = coverage_plan if isinstance(coverage_plan, dict) else {}
    areas = coverage_plan.get("areas")
    areas = areas if isinstance(areas, list) else []
    lines.append(
        "coverage_plan: "
        f"required={str(bool(coverage_plan.get('required'))).lower()} "
        f"delegation_required={str(bool(coverage_plan.get('delegation_required'))).lower()}"
    )
    for area in areas[:6]:
        if not isinstance(area, dict):
            continue
        files = area.get("files")
        file_text = ",".join(str(path) for path in files[:4]) if isinstance(files, list) else ""
        lines.append(
            "- "
            f"{area.get('id') or 'area'} | "
            f"kind={area.get('kind') or 'unknown'} | "
            f"files={file_text} | "
            f"must_answer={area.get('must_answer') or ''}"
        )
    if not areas:
        lines.append("- none")
    lines.append("parent_coverage_tasks:")
    if areas:
        covers = ",".join(str(area.get("id") or "area") for area in areas[:8] if isinstance(area, dict))
        lines.append(f"- cover_serially | covers={covers} | question=Review all required coverage areas in the parent session.")
    else:
        lines.append("- none")
    lines.append("critical_area_steps:")
    for step in CRITICAL_AREA_STEPS:
        lines.append(f"- {step}")
    lines.extend(
        [
            "required_behavior:",
            "- Report this intake before code reasoning, review findings, edits, or GitNexus claims.",
            "- Do not spawn sub-agents from Repo Context Forge output alone.",
            "- Cover every required coverage area in the parent session unless the current user turn explicitly asks for delegated agents.",
            "- Satisfy coverage_plan before GitNexus calls, GitHub review comments, review findings, or edits.",
            "- Consume gitnexus_analysis first; gitnexus_required_checks records the already-executed packet plan.",
            "- If a required GitNexus MCP tool is not loaded, run tool_search for that exact GitNexus capability before falling back or reporting it unavailable.",
            "- Use packet targets plus live base...HEAD, dirty worktree, or intent surface according to packet mode.",
            "- Do not let unscoped gitnexus_detect_changes(compare) choose the target surface.",
            "- Use gitnexus_detect_changes after local edits, before commit, or as supplemental graph evidence after the packet surface is fixed.",
            "END_REPO_CONTEXT_FORGE_REQUIRED_INTAKE",
            "",
        ]
    )
    return "\n".join(lines)


def trim_prompt_lines(lines: list[str], token_budget: int) -> list[str] | None:
    if estimate_tokens("\n".join(lines)) <= token_budget:
        return lines
    predicates = [
        lambda line: line.lstrip().startswith("<summary "),
        lambda line: line.lstrip().startswith("<symbol ")
        and " kind=" in line
        and "</symbol>" in line,
        lambda line: line.lstrip().startswith("<file ")
        and line.rstrip().endswith("/>")
        and (" weight=" in line or " count=" in line),
        lambda line: line.lstrip().startswith("<reason>"),
        lambda line: line.lstrip().startswith("<signal>"),
    ]
    trimmed = list(lines)
    for predicate in predicates:
        trimmed = [line for line in trimmed if not predicate(line)]
        if estimate_tokens("\n".join(trimmed)) <= token_budget:
            return trimmed
    return None


def render_compact_prompt(packet: dict[str, object]) -> str:
    target_state = packet["target_state"]
    gitnexus = packet["gitnexus"]
    source_status = packet.get("source_status") or {}
    soulforge = packet.get("soulforge") or {}
    semantic = packet.get("semantic_summaries") or {}
    coverage_plan = packet.get("coverage_plan") or {}
    assert isinstance(target_state, dict)
    assert isinstance(gitnexus, dict)
    assert isinstance(source_status, dict)
    assert isinstance(soulforge, dict)
    assert isinstance(semantic, dict)
    targets = packet["targets"]
    assert isinstance(targets, list)
    lines = ['<repo_context_packet schema_version="1" compacted="true">']
    blocker = packet.get("blocker")
    if packet.get("blocked") and isinstance(blocker, dict):
        lines.append(
            f'  <blocker reason="{html.escape(str(blocker.get("reason") or ""))}"/>'
        )
    lines.extend([
        "  <target_state>",
        f"    <mode>{html.escape(str(packet['mode']))}</mode>",
        f"    <source_repo>{html.escape(str(target_state['source_repo']))}</source_repo>",
        f"    <analysis_repo>{html.escape(str(target_state['analysis_repo']))}</analysis_repo>",
        f"    <base_ref>{html.escape(str(target_state['base_ref']))}</base_ref>",
        f"    <head_ref>{html.escape(str(target_state['head_ref']))}</head_ref>",
        f"    <head_sha>{html.escape(str(target_state['head_sha']))}</head_sha>",
        f"    <analysis_head_sha>{html.escape(str(target_state.get('analysis_head_sha') or ''))}</analysis_head_sha>",
        f"    <analysis_repo_is_cache_owned>{str(target_state.get('analysis_repo_is_cache_owned', False)).lower()}</analysis_repo_is_cache_owned>",
        f"    <analysis_head_matches_source_head>{str(target_state.get('analysis_head_matches_source_head', False)).lower()}</analysis_head_matches_source_head>",
        f"    <source_status_unchanged>{str(source_status.get('unchanged', False)).lower()}</source_status_unchanged>",
        f"    <token_budget>{html.escape(str(packet.get('token_budget') or DEFAULT_TOKEN_BUDGET))}</token_budget>",
        "  </target_state>",
    ])
    lines.extend(render_context_digest_lines(packet))
    lines.append("  <soulforge_status>")
    soulforge_target = soulforge.get("target")
    if isinstance(soulforge_target, dict):
        lines.extend([
            f"    <status>{html.escape(str(soulforge_target.get('status') or 'unknown'))}</status>",
            f"    <target_head_verified>{str(soulforge_target.get('target_head_verified', False)).lower()}</target_head_verified>",
            f"    <db_exists>{str(soulforge_target.get('db_exists', False)).lower()}</db_exists>",
        ])
    lines.extend([
        "  </soulforge_status>",
        "  <semantic_summaries>",
        f"    <mode>{html.escape(str(semantic.get('mode') or 'unknown'))}</mode>",
        f"    <synthetic_fill>{str(semantic.get('synthetic_fill', False)).lower()}</synthetic_fill>",
        f"    <live_llm_generation>{str(semantic.get('live_llm_generation', False)).lower()}</live_llm_generation>",
        "    <source_counts>",
    ])
    lines.extend(workflow_index.source_count_lines(semantic, "      "))
    lines.extend([
        "    </source_counts>",
        "  </semantic_summaries>",
    ])
    lines.extend(render_coverage_plan_lines(coverage_plan, "  "))
    lines.extend([
        "  <gitnexus_status>",
        f"    <status>{html.escape(str(gitnexus.get('status') or 'unknown'))}</status>",
        f"    <repo>{html.escape(str(gitnexus.get('repo') or ''))}</repo>",
        f"    <expected_head_sha>{html.escape(str(gitnexus.get('expected_head_sha') or ''))}</expected_head_sha>",
        f"    <indexed_head_sha>{html.escape(str(gitnexus.get('indexed_head_sha') or ''))}</indexed_head_sha>",
        f"    <reindex_attempted>{str(gitnexus.get('reindex_attempted', False)).lower()}</reindex_attempted>",
        f"    <required_checks_resolved>{str(gitnexus.get('required_checks_resolved', False)).lower()}</required_checks_resolved>",
        "  </gitnexus_status>",
    ])
    lines.extend(render_gitnexus_analysis_lines(gitnexus, "  "))
    lines.extend([
        "  <warnings>",
        "    <warning>prompt compacted to fit token budget; rerun with a larger budget for symbol details</warning>",
    ])
    warnings = packet.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            lines.append(f"    <warning>{html.escape(str(warning))}</warning>")
    lines.extend(["  </warnings>", "  <targets>"])
    for target in targets:
        if not isinstance(target, dict):
            continue
        lines.append(f"    <file path=\"{html.escape(str(target['path']))}\">")
        lines.append(f"      <rank>{html.escape(str(target.get('rank')))}</rank>")
        lines.append(f"      <role>{html.escape(str(target.get('surface_role') or 'unknown'))}</role>")
        lines.append(f"      <priority_score>{html.escape(str(target.get('priority_score') or 0))}</priority_score>")
        lines.append("    </file>")
    lines.extend(["  </targets>", "  <gitnexus_required_checks>"])
    plan = gitnexus.get("plan")
    if isinstance(plan, list):
        for item in plan[:20]:
            if isinstance(item, dict):
                attrs = " ".join(
                    f'{html.escape(str(key))}="{html.escape(str(value))}"'
                    for key, value in item.items()
                )
                lines.append(f"    <check {attrs}/>")
    lines.extend(["  </gitnexus_required_checks>", "</repo_context_packet>"])
    return "\n".join(lines) + "\n"


def render_prompt(packet: dict[str, object]) -> str:
    target_state = packet["target_state"]
    gitnexus = packet["gitnexus"]
    soulforge = packet.get("soulforge") or {}
    semantic = packet.get("semantic_summaries") or {}
    source_status = packet.get("source_status") or {}
    policy = packet.get("policy") or {}
    coverage_plan = packet.get("coverage_plan") or {}
    assert isinstance(target_state, dict)
    assert isinstance(gitnexus, dict)
    assert isinstance(soulforge, dict)
    assert isinstance(semantic, dict)
    assert isinstance(source_status, dict)
    assert isinstance(policy, dict)
    targets = packet["targets"]
    assert isinstance(targets, list)

    lines = [
        '<repo_context_packet schema_version="1">',
    ]
    if packet.get("blocked"):
        blocker = packet.get("blocker")
        reason = ""
        suggestions: list[object] = []
        if isinstance(blocker, dict):
            reason = str(blocker.get("reason") or "")
            raw_suggestions = blocker.get("worktree_suggestions")
            if isinstance(raw_suggestions, list):
                suggestions = raw_suggestions
        lines.extend(
            [
                f"  <blocker reason=\"{html.escape(reason)}\">",
                "    <worktree_suggestions>",
            ]
        )
        for suggestion in suggestions:
            if isinstance(suggestion, dict):
                path = html.escape(str(suggestion.get("path") or ""))
                branch = html.escape(str(suggestion.get("branch") or ""))
                head = html.escape(str(suggestion.get("head") or ""))
                detached = html.escape(str(suggestion.get("detached") or "false"))
                lines.append(
                    f"      <worktree path=\"{path}\" branch=\"{branch}\" head=\"{head}\" detached=\"{detached}\"/>"
                )
        lines.extend(["    </worktree_suggestions>", "  </blocker>"])
    lines.extend([
        "  <target_state>",
        f"    <mode>{html.escape(str(packet['mode']))}</mode>",
        f"    <source_repo>{html.escape(str(target_state['source_repo']))}</source_repo>",
        f"    <analysis_repo>{html.escape(str(target_state['analysis_repo']))}</analysis_repo>",
        f"    <base_ref>{html.escape(str(target_state['base_ref']))}</base_ref>",
        f"    <head_ref>{html.escape(str(target_state['head_ref']))}</head_ref>",
        f"    <head_sha>{html.escape(str(target_state['head_sha']))}</head_sha>",
        f"    <analysis_head_sha>{html.escape(str(target_state.get('analysis_head_sha') or ''))}</analysis_head_sha>",
        f"    <analysis_repo_is_cache_owned>{str(target_state.get('analysis_repo_is_cache_owned', False)).lower()}</analysis_repo_is_cache_owned>",
        f"    <analysis_head_matches_source_head>{str(target_state.get('analysis_head_matches_source_head', False)).lower()}</analysis_head_matches_source_head>",
        f"    <source_dirty>{str(target_state['source_dirty']).lower()}</source_dirty>",
        f"    <target_dirty>{str(target_state['target_dirty']).lower()}</target_dirty>",
        f"    <source_status_unchanged>{str(source_status.get('unchanged', False)).lower()}</source_status_unchanged>",
        f"    <token_budget>{html.escape(str(packet.get('token_budget') or DEFAULT_TOKEN_BUDGET))}</token_budget>",
        "  </target_state>",
    ])
    lines.extend(render_context_digest_lines(packet))
    lines.extend([
        "  <source_status>",
        f"    <before_hash>{html.escape(str(source_status.get('before_hash') or ''))}</before_hash>",
        f"    <after_hash>{html.escape(str(source_status.get('after_hash') or ''))}</after_hash>",
        f"    <before_count>{html.escape(str(source_status.get('before_count') or 0))}</before_count>",
        f"    <after_count>{html.escape(str(source_status.get('after_count') or 0))}</after_count>",
        "    <added_paths>",
    ])
    for path in source_status.get("added_paths", []):
        lines.append(f"      <path>{html.escape(str(path))}</path>")
    lines.extend([
        "    </added_paths>",
        "    <tool_cache_paths_before>",
    ])
    for path in source_status.get("tool_cache_paths_before", []):
        lines.append(f"      <path>{html.escape(str(path))}</path>")
    lines.extend([
        "    </tool_cache_paths_before>",
        "    <tool_cache_paths_created>",
    ])
    for path in source_status.get("tool_cache_paths_created", []):
        lines.append(f"      <path>{html.escape(str(path))}</path>")
    lines.extend([
        "    </tool_cache_paths_created>",
        "  </source_status>",
        "  <policy>",
        "    <reference_only_prefixes>",
    ])
    for prefix in policy.get("reference_only_prefixes", []):
        lines.append(f"      <prefix>{html.escape(str(prefix))}</prefix>")
    lines.extend([
        "    </reference_only_prefixes>",
        "  </policy>",
        "  <soulforge_status>",
    ])
    soulforge_target = soulforge.get("target")
    if isinstance(soulforge_target, dict):
        lines.extend([
            f"    <status>{html.escape(str(soulforge_target.get('status') or 'unknown'))}</status>",
            f"    <target_head_verified>{str(soulforge_target.get('target_head_verified', False)).lower()}</target_head_verified>",
            f"    <db_path>{html.escape(str(soulforge_target.get('db_path') or ''))}</db_path>",
            f"    <db_exists>{str(soulforge_target.get('db_exists', False)).lower()}</db_exists>",
        ])
    lines.extend([
        "  </soulforge_status>",
        "  <semantic_summaries>",
        f"    <mode>{html.escape(str(semantic.get('mode') or 'unknown'))}</mode>",
        f"    <synthetic_fill>{str(semantic.get('synthetic_fill', False)).lower()}</synthetic_fill>",
        f"    <live_llm_generation>{str(semantic.get('live_llm_generation', False)).lower()}</live_llm_generation>",
        "    <source_counts>",
    ])
    lines.extend(workflow_index.source_count_lines(semantic, "      "))
    lines.extend([
        "    </source_counts>",
        "  </semantic_summaries>",
    ])
    lines.extend(render_coverage_plan_lines(coverage_plan, "  "))
    lines.extend([
        "  <gitnexus_status>",
        f"    <status>{html.escape(str(gitnexus.get('status') or 'unknown'))}</status>",
        f"    <repo>{html.escape(str(gitnexus.get('repo') or ''))}</repo>",
        f"    <expected_head_sha>{html.escape(str(gitnexus.get('expected_head_sha') or ''))}</expected_head_sha>",
        f"    <indexed_head_sha>{html.escape(str(gitnexus.get('indexed_head_sha') or ''))}</indexed_head_sha>",
        f"    <reindex_attempted>{str(gitnexus.get('reindex_attempted', False)).lower()}</reindex_attempted>",
        f"    <required_checks_resolved>{str(gitnexus.get('required_checks_resolved', False)).lower()}</required_checks_resolved>",
        "    <missing_required_symbols>",
    ])
    missing_symbols = gitnexus.get("missing_required_symbols")
    if isinstance(missing_symbols, list):
        for symbol in missing_symbols:
            lines.append(f"      <symbol>{html.escape(str(symbol))}</symbol>")
    lines.extend([
        "    </missing_required_symbols>",
        "  </gitnexus_status>",
    ])
    lines.extend(render_gitnexus_analysis_lines(gitnexus, "  "))
    lines.extend([
        "  <scope_rules>",
        "    Use files under <targets> as the first-pass edit/review surface.",
        "    Use workflow-index targets and symbols as the packet's source-orientation surface.",
        "    Use <soulforge_impact> as optional native graph blast radius when available.",
        "    Use <gitnexus_status><repo> for every GitNexus MCP call for this packet.",
        "    Do not spawn sub-agents from Repo Context Forge output alone.",
        "    Cover every required coverage area in the parent session unless the current user turn explicitly asks for delegated agents.",
        "    Satisfy coverage_plan before GitNexus calls, GitHub review comments, review findings, or edits.",
        "    Consume <gitnexus_analysis> first; <gitnexus_required_checks> records the already-executed packet plan.",
        "    Do not let unscoped gitnexus_detect_changes(compare) choose the target surface.",
        "    Treat source dirty overlaps as warnings, not PR target files, when mode is pr.",
        "    Trust GitNexus blast-radius claims only when <gitnexus_status> is fresh or reindexed and required checks resolve.",
        "  </scope_rules>",
        "  <warnings>",
    ])
    warnings = packet.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            lines.append(f"    <warning>{html.escape(str(warning))}</warning>")
    lines.extend(["  </warnings>", "  <soulforge_impact>"])
    for target in targets:
        if not isinstance(target, dict):
            continue
        impact = target.get("soulforge_impact")
        if not isinstance(impact, dict):
            continue
        lines.append(f"    <file path=\"{html.escape(str(target['path']))}\">")
        lines.append(f"      <risk>{html.escape(str(impact.get('risk') or 'unknown'))}</risk>")
        lines.append(f"      <direct_dependents>{html.escape(str(impact.get('direct_dependents') or 0))}</direct_dependents>")
        lines.append(f"      <dependencies>{html.escape(str(impact.get('dependencies') or 0))}</dependencies>")
        lines.append(f"      <cochange_partners>{html.escape(str(impact.get('cochange_partners') or 0))}</cochange_partners>")
        lines.append(f"      <total_affected_scope>{html.escape(str(impact.get('total_affected_scope') or 0))}</total_affected_scope>")
        lines.append("      <dependents>")
        dependents = impact.get("dependents")
        if isinstance(dependents, list):
            for dependent in dependents[:5]:
                if isinstance(dependent, dict):
                    lines.append(
                        f"        <file path=\"{html.escape(str(dependent.get('path') or ''))}\" "
                        f"weight=\"{html.escape(str(dependent.get('weight') or 0))}\"/>"
                    )
        lines.append("      </dependents>")
        lines.append("      <cochanges>")
        cochanges = impact.get("cochanges")
        if isinstance(cochanges, list):
            for partner in cochanges[:5]:
                if isinstance(partner, dict):
                    lines.append(
                        f"        <file path=\"{html.escape(str(partner.get('path') or ''))}\" "
                        f"count=\"{html.escape(str(partner.get('count') or 0))}\"/>"
                    )
        lines.append("      </cochanges>")
        lines.append("      <exported_symbols_at_risk>")
        symbols_at_risk = impact.get("exported_symbols_at_risk")
        if isinstance(symbols_at_risk, list):
            for symbol in symbols_at_risk[:5]:
                if isinstance(symbol, dict):
                    lines.append(
                        f"        <symbol name=\"{html.escape(str(symbol.get('name') or ''))}\" "
                        f"kind=\"{html.escape(str(symbol.get('kind') or ''))}\" "
                        f"usage_files=\"{html.escape(str(symbol.get('usage_files') or 0))}\"/>"
                    )
        lines.append("      </exported_symbols_at_risk>")
        lines.append("    </file>")
    lines.extend(["  </soulforge_impact>", "  <targets>"])
    for target in targets:
        if not isinstance(target, dict):
            continue
        lines.append(f"    <file path=\"{html.escape(str(target['path']))}\">")
        lines.append(f"      <rank>{html.escape(str(target.get('rank')))}</rank>")
        lines.append(f"      <role>{html.escape(str(target.get('surface_role') or 'unknown'))}</role>")
        lines.append(f"      <priority_score>{html.escape(str(target.get('priority_score') or 0))}</priority_score>")
        rank_signals = target.get("rank_signals")
        if isinstance(rank_signals, list):
            lines.append("      <rank_signals>")
            for signal in rank_signals:
                lines.append(f"        <signal>{html.escape(str(signal))}</signal>")
            lines.append("      </rank_signals>")
        why_selected = target.get("why_selected")
        if isinstance(why_selected, list):
            lines.append("      <why_selected>")
            for reason in why_selected:
                lines.append(f"        <reason>{html.escape(str(reason))}</reason>")
            lines.append("      </why_selected>")
        if target.get("dependent_count") is not None:
            lines.append(f"      <dependent_count>{html.escape(str(target['dependent_count']))}</dependent_count>")
        changed_symbols = target.get("changed_symbols")
        symbols = changed_symbols if isinstance(changed_symbols, list) and changed_symbols else target.get("symbols")
        if isinstance(symbols, list):
            lines.append("      <symbols>")
            for symbol in symbols[:8]:
                if not isinstance(symbol, dict):
                    continue
                name = html.escape(str(symbol.get("name")))
                kind = html.escape(str(symbol.get("kind")))
                start = html.escape(str(symbol.get("line")))
                end = html.escape(str(symbol.get("end_line")))
                signature = html.escape(str(symbol.get("signature") or ""))
                lines.append(
                    f"        <symbol name=\"{name}\" kind=\"{kind}\" line=\"{start}\" end_line=\"{end}\">{signature}</symbol>"
                )
                if symbol.get("summary"):
                    source = html.escape(str(symbol.get("summary_source") or "unknown"))
                    lines.append(
                        f"        <summary source=\"{source}\">{html.escape(str(symbol['summary']))}</summary>"
                    )
            lines.append("      </symbols>")
        lines.append("    </file>")
    lines.extend(["  </targets>", "  <gitnexus_required_checks>"])
    plan = gitnexus.get("plan")
    if isinstance(plan, list):
        for item in plan[:20]:
            if isinstance(item, dict):
                attrs = " ".join(
                    f'{html.escape(str(key))}="{html.escape(str(value))}"'
                    for key, value in item.items()
                )
                lines.append(f"    <check {attrs}/>")
    lines.extend(["  </gitnexus_required_checks>", "</repo_context_packet>"])
    token_budget = int(packet.get("token_budget") or DEFAULT_TOKEN_BUDGET)
    trimmed = trim_prompt_lines(lines, token_budget)
    if trimmed is not None:
        return "\n".join(trimmed) + "\n"
    return render_compact_prompt(packet)


def render_packet(packet: dict[str, object], output_format: OutputFormat) -> str:
    if output_format == "json":
        return json.dumps(packet, indent=2, sort_keys=True) + "\n"
    if output_format == "prompt":
        return render_prompt(packet)
    return render_markdown(packet)


def packet_file_for(packet: dict[str, object], cache_dir: Path) -> Path:
    target_state = packet["target_state"]
    assert isinstance(target_state, dict)
    head_sha = str(target_state["head_sha"])[:12]
    mode = str(packet["mode"])
    repo_name = Path(str(packet["repo"])).name
    return cache_dir / "packets" / f"{repo_name}-{mode}-{head_sha}.prompt.xml"


def wrapper_env(packet: dict[str, object], packet_file: Path) -> dict[str, str]:
    target_state = packet["target_state"]
    gitnexus = packet["gitnexus"]
    assert isinstance(target_state, dict)
    assert isinstance(gitnexus, dict)
    return {
        "REPO_CONTEXT_FORGE_PACKET_FILE": str(packet_file),
        "REPO_CONTEXT_FORGE_MODE": str(packet["mode"]),
        "REPO_CONTEXT_FORGE_SOURCE_REPO": str(target_state["source_repo"]),
        "REPO_CONTEXT_FORGE_ANALYSIS_REPO": str(target_state["analysis_repo"]),
        "REPO_CONTEXT_FORGE_TARGET_SHA": str(target_state["head_sha"]),
        "REPO_CONTEXT_FORGE_GITNEXUS_REPO": str(gitnexus["repo"]),
    }


def make_benchmark(
    repo: Path,
    *,
    base_ref: str,
    head_ref: str,
    intent: str,
    top: int,
    cache_dir: Path,
    soulforge_bin: str | None,
    map_build: MapBuildMode,
    map_timeout_ms: int,
    gitnexus_repo: str | None,
) -> dict[str, object]:
    modes = [
        ("no_map", None),
        ("ambient_map", "local"),
        ("clean_target_map", "pr"),
    ]
    results = []
    for label, mode in modes:
        if mode is None:
            source_repo = repo_root(repo)
            git_state = read_git_state(source_repo, base_ref, head_ref)
            results.append(
                {
                    "label": label,
                    "target_files": [],
                    "dirty_contamination": False,
                    "prompt": intent,
                    "git": {
                        "pr_files": git_state.pr_files,
                        "staged_files": git_state.staged_files,
                        "unstaged_files": git_state.unstaged_files,
                        "untracked_files": git_state.untracked_files,
                    },
                }
            )
            continue
        packet = make_packet(
            repo,
            mode=mode,  # type: ignore[arg-type]
            base_ref=base_ref,
            head_ref=head_ref,
            intent=intent,
            top=top,
            token_budget=DEFAULT_TOKEN_BUDGET,
            cache_dir=cache_dir,
            soulforge_bin=soulforge_bin,
            map_build=map_build,
            map_timeout_ms=map_timeout_ms,
            allow_missing_map=True,
            gitnexus_repo=gitnexus_repo,
        )
        targets = packet["targets"]
        assert isinstance(targets, list)
        results.append(
            {
                "label": label,
                "target_files": [target["path"] for target in targets if isinstance(target, dict)],
                "dirty_contamination": any(
                    bool(target.get("scope_contaminated"))
                    for target in targets
                    if isinstance(target, dict)
                ),
                "warnings": packet.get("warnings", []),
                "prompt": render_prompt(packet),
            }
        )
    return {"schema_version": 1, "repo": str(repo.resolve()), "intent": intent, "results": results}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="repo-context-forge")
    subcommands = parser.add_subparsers(dest="command", required=True)

    analyze = subcommands.add_parser("analyze", help="Generate a production context packet")
    analyze.add_argument("--repo", required=True, type=Path)
    analyze.add_argument("--mode", choices=["pr", "local", "intent", "repo"], default="pr")
    analyze.add_argument("--base", default="main")
    analyze.add_argument("--head", default="HEAD")
    analyze.add_argument("--intent")
    analyze.add_argument("--format", choices=["markdown", "json", "prompt"], default="markdown")
    analyze.add_argument("--top", type=int, default=20)
    analyze.add_argument("--token-budget", type=int)
    analyze.add_argument("--conversation-tokens", type=int)
    analyze.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    analyze.add_argument("--soulforge-bin")
    analyze.add_argument("--map-build", choices=["auto", "always", "never"], default="auto")
    analyze.add_argument("--map-timeout-ms", type=int, default=120_000)
    analyze.add_argument("--allow-missing-map", action="store_true")
    analyze.add_argument("--gitnexus-repo")
    analyze.add_argument("--gitnexus-mode", choices=["off", "check", "auto"], default="auto")
    analyze.add_argument("--out", type=Path)

    packet = subcommands.add_parser("packet", help="Compatibility alias for analyze")
    packet.add_argument("--repo", required=True, type=Path)
    packet.add_argument("--base", default="main")
    packet.add_argument("--head", default="HEAD")
    packet.add_argument("--scope", choices=["pr", "dirty", "all"], default="pr")
    packet.add_argument("--format", choices=["markdown", "json", "prompt"], default="markdown")
    packet.add_argument("--top", type=int, default=20)
    packet.add_argument("--out", type=Path)

    benchmark = subcommands.add_parser("benchmark", help="Generate no-map/ambient/clean-target prompts")
    benchmark.add_argument("--repo", required=True, type=Path)
    benchmark.add_argument("--base", default="main")
    benchmark.add_argument("--head", default="HEAD")
    benchmark.add_argument("--intent", required=True)
    benchmark.add_argument("--format", choices=["markdown", "json"], default="markdown")
    benchmark.add_argument("--top", type=int, default=20)
    benchmark.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    benchmark.add_argument("--soulforge-bin")
    benchmark.add_argument("--map-build", choices=["auto", "always", "never"], default="auto")
    benchmark.add_argument("--map-timeout-ms", type=int, default=120_000)
    benchmark.add_argument("--gitnexus-repo")
    benchmark.add_argument("--out", type=Path)

    context_start = subcommands.add_parser("context-start", help="Start a task context and render its packet")
    context_start.add_argument("--repo", required=True, type=Path)
    context_start.add_argument("--mode", choices=["pr", "local", "intent", "repo"], default="pr")
    context_start.add_argument("--base", default="main")
    context_start.add_argument("--head", default="HEAD")
    context_start.add_argument("--intent")
    context_start.add_argument("--task-id")
    context_start.add_argument("--top", type=int, default=20)
    context_start.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    context_start.add_argument("--soulforge-bin")
    context_start.add_argument("--map-build", choices=["auto", "always", "never"], default="auto")
    context_start.add_argument("--map-timeout-ms", type=int, default=120_000)
    context_start.add_argument("--allow-missing-map", action="store_true")
    context_start.add_argument("--gitnexus-repo")
    context_start.add_argument("--gitnexus-mode", choices=["off", "check", "auto"], default="auto")
    context_start.add_argument("--out", type=Path)

    context_refresh = subcommands.add_parser("context-refresh", help="Refresh a task context packet")
    context_refresh.add_argument("--repo", required=True, type=Path)
    context_refresh.add_argument("--mode", choices=["pr", "local", "intent", "repo"], default="pr")
    context_refresh.add_argument("--base", default="main")
    context_refresh.add_argument("--head", default="HEAD")
    context_refresh.add_argument("--task-id", required=True)
    context_refresh.add_argument("--top", type=int, default=20)
    context_refresh.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    context_refresh.add_argument("--soulforge-bin")
    context_refresh.add_argument("--map-build", choices=["auto", "always", "never"], default="auto")
    context_refresh.add_argument("--map-timeout-ms", type=int, default=120_000)
    context_refresh.add_argument("--allow-missing-map", action="store_true")
    context_refresh.add_argument("--gitnexus-repo")
    context_refresh.add_argument("--gitnexus-mode", choices=["off", "check", "auto"], default="auto")
    context_refresh.add_argument("--out", type=Path)

    for command_name, help_text in (
        ("context-record-read", "Record files read during the task"),
        ("context-record-search", "Record files matched by search during the task"),
        ("context-record-edit", "Record files edited during the task"),
        ("context-record-mention", "Record files mentioned during the task"),
    ):
        parser_for_event = subcommands.add_parser(command_name, help=help_text)
        parser_for_event.add_argument("--repo", required=True, type=Path)
        parser_for_event.add_argument("--head", default="HEAD")
        parser_for_event.add_argument("--task-id", required=True)
        parser_for_event.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
        parser_for_event.add_argument("paths", nargs="+")

    gitnexus_merge = subcommands.add_parser("gitnexus-merge", help="Merge GitNexus findings into a packet")
    gitnexus_merge.add_argument("--repo", required=True, type=Path)
    gitnexus_merge.add_argument("--packet", required=True, type=Path)
    gitnexus_merge.add_argument("--findings", required=True, type=Path)
    gitnexus_merge.add_argument("--format", choices=["markdown", "json", "prompt"], default="prompt")
    gitnexus_merge.add_argument("--out", type=Path)

    wrap = subcommands.add_parser("wrap", help="Generate a prompt packet and optionally run a command")
    wrap.add_argument("--repo", required=True, type=Path)
    wrap.add_argument("--mode", choices=["pr", "local", "intent", "repo"], default="pr")
    wrap.add_argument("--base", default="main")
    wrap.add_argument("--head", default="HEAD")
    wrap.add_argument("--intent")
    wrap.add_argument("--top", type=int, default=20)
    wrap.add_argument("--token-budget", type=int)
    wrap.add_argument("--conversation-tokens", type=int)
    wrap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    wrap.add_argument("--soulforge-bin")
    wrap.add_argument("--map-build", choices=["auto", "always", "never"], default="auto")
    wrap.add_argument("--map-timeout-ms", type=int, default=120_000)
    wrap.add_argument("--allow-missing-map", action="store_true")
    wrap.add_argument("--gitnexus-repo")
    wrap.add_argument("--gitnexus-mode", choices=["off", "check", "auto"], default="auto")
    wrap.add_argument("--out", type=Path)
    wrap.add_argument("--no-stdin", action="store_true")
    wrap.add_argument("wrapped_command", nargs=argparse.REMAINDER)

    return parser.parse_args(argv)


def output_text(text: str, out: Path | None) -> None:
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def render_benchmark_markdown(report: dict[str, object]) -> str:
    results = report["results"]
    assert isinstance(results, list)
    lines = ["# Repo Context Benchmark", "", f"- repo: `{report['repo']}`", ""]
    for result in results:
        assert isinstance(result, dict)
        lines.append(f"## {result['label']}")
        target_files = result.get("target_files")
        if isinstance(target_files, list):
            lines.append(f"- target files: {', '.join(str(item) for item in target_files) or 'none'}")
        lines.append(f"- dirty contamination: {result.get('dirty_contamination', False)}")
        warnings = result.get("warnings")
        if isinstance(warnings, list) and warnings:
            lines.extend(f"- warning: {warning}" for warning in warnings)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo = args.repo.resolve()
    if not is_git_repo(repo):
        print(f"not a git repository: {repo}", file=sys.stderr)
        return 2

    try:
        if args.command == "context-start":
            head_sha = run_git(repo_root(repo), ["rev-parse", args.head])
            task_id = args.task_id or default_task_id(repo_root(repo), head_sha, args.intent)
            state_path = task_state_path(args.cache_dir.resolve(), repo_root(repo), head_sha, task_id)
            state = empty_task_state(repo_root(repo), head_sha, task_id, args.intent)
            packet = make_packet(
                repo,
                mode=args.mode,
                base_ref=args.base,
                head_ref=args.head,
                intent=args.intent,
                top=args.top,
                token_budget=DEFAULT_TOKEN_BUDGET,
                cache_dir=args.cache_dir.resolve(),
                soulforge_bin=find_soulforge_binary(args.soulforge_bin),
                map_build=args.map_build,
                map_timeout_ms=args.map_timeout_ms,
                allow_missing_map=args.allow_missing_map,
                gitnexus_repo=args.gitnexus_repo,
                task_state=state,
                gitnexus_mode=args.gitnexus_mode,
            )
            state["previous_targets"] = [
                str(target["path"])
                for target in packet["targets"]
                if isinstance(target, dict)
            ]
            save_task_state(state_path, state)
            output_text(render_prompt(packet), args.out)
            return 0

        if args.command == "context-refresh":
            head_sha = run_git(repo_root(repo), ["rev-parse", args.head])
            state_path = task_state_path(args.cache_dir.resolve(), repo_root(repo), head_sha, args.task_id)
            state = load_task_state(state_path)
            packet = make_packet(
                repo,
                mode=args.mode,
                base_ref=args.base,
                head_ref=args.head,
                intent=str(state.get("intent") or ""),
                top=args.top,
                token_budget=DEFAULT_TOKEN_BUDGET,
                cache_dir=args.cache_dir.resolve(),
                soulforge_bin=find_soulforge_binary(args.soulforge_bin),
                map_build=args.map_build,
                map_timeout_ms=args.map_timeout_ms,
                allow_missing_map=args.allow_missing_map,
                gitnexus_repo=args.gitnexus_repo,
                task_state=state,
                gitnexus_mode=args.gitnexus_mode,
            )
            state["previous_targets"] = [
                str(target["path"])
                for target in packet["targets"]
                if isinstance(target, dict)
            ]
            save_task_state(state_path, state)
            output_text(render_prompt(packet), args.out)
            return 0

        if args.command.startswith("context-record-"):
            event_by_command: dict[str, TaskEvent] = {
                "context-record-read": "read",
                "context-record-search": "search",
                "context-record-edit": "edit",
                "context-record-mention": "mention",
            }
            head_sha = run_git(repo_root(repo), ["rev-parse", args.head])
            state_path = task_state_path(args.cache_dir.resolve(), repo_root(repo), head_sha, args.task_id)
            state = load_task_state(state_path)
            record_task_event(state, event_by_command[args.command], args.paths)
            save_task_state(state_path, state)
            output_text(json.dumps(state, indent=2, sort_keys=True) + "\n", None)
            return 0

        if args.command == "gitnexus-merge":
            packet = json.loads(args.packet.read_text(encoding="utf-8"))
            findings = json.loads(args.findings.read_text(encoding="utf-8"))
            if not isinstance(packet, dict) or not isinstance(findings, dict):
                raise RuntimeError("packet and findings must be JSON objects")
            merged = apply_gitnexus_findings(packet, findings)
            output_text(render_packet(merged, args.format), args.out)
            return 0

        if args.command == "wrap":
            token_budget = compute_token_budget(args.conversation_tokens, args.token_budget)
            packet = make_packet(
                repo,
                mode=args.mode,
                base_ref=args.base,
                head_ref=args.head,
                intent=args.intent,
                top=args.top,
                token_budget=token_budget,
                cache_dir=args.cache_dir.resolve(),
                soulforge_bin=find_soulforge_binary(args.soulforge_bin),
                map_build=args.map_build,
                map_timeout_ms=args.map_timeout_ms,
                allow_missing_map=args.allow_missing_map,
                gitnexus_repo=args.gitnexus_repo,
                gitnexus_mode=args.gitnexus_mode,
            )
            text = render_prompt(packet)
            packet_file = args.out.resolve() if args.out else packet_file_for(packet, args.cache_dir.resolve())
            output_text(text, packet_file)

            command = list(args.wrapped_command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                print(packet_file)
                return 0

            env = os.environ.copy()
            env.update(wrapper_env(packet, packet_file))
            proc = subprocess.run(
                command,
                input=None if args.no_stdin else text,
                text=True,
                env=env,
                check=False,
            )
            return proc.returncode

        if args.command == "benchmark":
            report = make_benchmark(
                repo,
                base_ref=args.base,
                head_ref=args.head,
                intent=args.intent,
                top=args.top,
                cache_dir=args.cache_dir.resolve(),
                soulforge_bin=find_soulforge_binary(args.soulforge_bin),
                map_build=args.map_build,
                map_timeout_ms=args.map_timeout_ms,
                gitnexus_repo=args.gitnexus_repo,
            )
            text = (
                json.dumps(report, indent=2, sort_keys=True) + "\n"
                if args.format == "json"
                else render_benchmark_markdown(report) + "\n"
            )
            output_text(text, args.out)
            return 0

        if args.command == "packet":
            mode: Mode = "pr" if args.scope == "pr" else "local"
            packet = make_packet(
                repo,
                mode=mode,
                base_ref=args.base,
                head_ref=args.head,
                intent=None,
                top=args.top,
                token_budget=DEFAULT_TOKEN_BUDGET,
                cache_dir=DEFAULT_CACHE_DIR,
                soulforge_bin=find_soulforge_binary(None),
                map_build="never",
                map_timeout_ms=120_000,
                allow_missing_map=True,
                gitnexus_repo=None,
                gitnexus_mode="off",
            )
            output_text(render_packet(packet, args.format), args.out)
            return 0

        token_budget = compute_token_budget(args.conversation_tokens, args.token_budget)
        packet = make_packet(
            repo,
            mode=args.mode,
            base_ref=args.base,
            head_ref=args.head,
            intent=args.intent,
            top=args.top,
            token_budget=token_budget,
            cache_dir=args.cache_dir.resolve(),
            soulforge_bin=find_soulforge_binary(args.soulforge_bin),
            map_build=args.map_build,
            map_timeout_ms=args.map_timeout_ms,
            allow_missing_map=args.allow_missing_map,
            gitnexus_repo=args.gitnexus_repo,
            gitnexus_mode=args.gitnexus_mode,
        )
        output_text(render_packet(packet, args.format), args.out)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
