#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


Mode = Literal["pr", "local", "intent"]
Scope = Literal["pr", "dirty", "all"]
OutputFormat = Literal["markdown", "json", "prompt"]
MapBuildMode = Literal["auto", "always", "never"]


DEFAULT_CACHE_DIR = Path.home() / ".cache" / "repo-context-forge"
DEFAULT_TOKEN_BUDGET = 2500
MIN_TOKEN_BUDGET = 1500
MAX_TOKEN_BUDGET = DEFAULT_TOKEN_BUDGET


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


@dataclass(frozen=True)
class MapFile:
    path: str
    pagerank: float
    symbol_count: int
    line_count: int
    rank: int


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line: int
    end_line: int
    signature: str | None
    is_exported: bool


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
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
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


def is_git_repo(path: Path) -> bool:
    proc = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, allow_fail=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_root(path: Path) -> Path:
    root = run_git(path, ["rev-parse", "--show-toplevel"])
    return Path(root).resolve()


def dirty_paths(repo: Path) -> list[str]:
    status = run_cmd(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        allow_fail=True,
    ).stdout
    return parse_porcelain_paths(status)


def parse_porcelain_paths(status: str) -> list[str]:
    paths = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        paths.append(line[3:])
    return paths


def filter_tool_cache_dirty_paths(paths: Iterable[str]) -> list[str]:
    return [
        path
        for path in paths
        if path != ".soulforge" and not path.startswith(".soulforge/")
    ]


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
    if content_changes and all(line in {"+.soulforge", "+.soulforge/"} for line in content_changes):
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
    return [
        token
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", intent.lower())
        if len(token) > 2 and token not in stop
    ]


def compute_token_budget(conversation_tokens: int | None, explicit_budget: int | None) -> int:
    if explicit_budget is not None:
        return explicit_budget
    if not conversation_tokens or conversation_tokens < 1000:
        return DEFAULT_TOKEN_BUDGET
    scale = max(0.6, 1 - (conversation_tokens / 100_000) * 0.4)
    return round(MIN_TOKEN_BUDGET + (MAX_TOKEN_BUDGET - MIN_TOKEN_BUDGET) * scale)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def cache_key_for(repo: Path, head_sha: str, extra: str = "") -> str:
    material = f"{repo.resolve()}\0{head_sha}\0{extra}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def safe_rmtree(path: Path, cache_root: Path) -> None:
    resolved = path.resolve()
    root = cache_root.resolve()
    if root not in resolved.parents and resolved != root:
        raise RuntimeError(f"refusing to remove path outside cache root: {resolved}")
    shutil.rmtree(resolved)


def ensure_pr_worktree(source_repo: Path, head_ref: str, cache_dir: Path) -> TargetState:
    head_sha = run_git(source_repo, ["rev-parse", head_ref])
    key = cache_key_for(source_repo, head_sha)
    worktree = (cache_dir / "worktrees" / f"{source_repo.name}-{head_sha[:12]}-{key}").resolve()

    if worktree.exists():
        existing_sha = run_git(worktree, ["rev-parse", "HEAD"], allow_fail=True)
        if existing_sha != head_sha:
            run_git(source_repo, ["worktree", "remove", "--force", str(worktree)], allow_fail=True)
            if worktree.exists():
                safe_rmtree(worktree, cache_dir)

    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run_git(source_repo, ["worktree", "add", "--detach", str(worktree), head_sha])

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
        source_dirty=is_dirty(source_repo),
        target_dirty=target_dirty,
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

    head_sha = run_git(source_repo, ["rev-parse", head_ref])
    return TargetState(
        mode=mode,
        source_repo=source_repo,
        analysis_repo=source_repo,
        base_ref=base_ref,
        head_ref=head_ref,
        head_sha=head_sha,
        source_dirty=is_dirty(source_repo),
        target_dirty=is_dirty(source_repo),
        cache_key=None,
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
    def __init__(self, repo: Path) -> None:
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
        if not self.available or not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
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
                WHERE path IN ({placeholders})
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
        if not self.available:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.name, s.kind, s.line, s.end_line, s.signature, s.is_exported
                FROM symbols s
                JOIN files f ON f.id = s.file_id
                WHERE f.path = ?
                ORDER BY s.is_exported DESC, s.line ASC
                LIMIT ?
                """,
                (path, limit),
            ).fetchall()
        return [
            Symbol(
                name=str(row["name"]),
                kind=str(row["kind"]),
                line=int(row["line"] or 0),
                end_line=int(row["end_line"] or row["line"] or 0),
                signature=str(row["signature"]) if row["signature"] is not None else None,
                is_exported=bool(row["is_exported"]),
            )
            for row in rows
        ]

    def intent_files(self, tokens: list[str], limit: int) -> list[str]:
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
) -> list[str]:
    if mode == "pr":
        return source_git_state.pr_files
    if mode == "local":
        return selected_files(source_git_state, "all")
    tokens = tokenize_intent(intent or "")
    return soul_map.intent_files(tokens, top)


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
) -> list[dict[str, object]]:
    map_files = soul_map.files_by_path(targets)
    target_entries: list[dict[str, object]] = []
    for path in targets:
        map_file = map_files.get(path)
        scope: Scope = "pr" if mode == "pr" else "dirty"
        ranges = (
            diff_ranges_for_file(source_repo, base_ref, scope, path, head_ref=head_ref)
            if mode != "intent"
            else []
        )
        symbols = soul_map.symbols_for_file(path, limit=80)
        changed_symbols = remove_nested_symbols(
            [symbol for symbol in symbols if symbol_overlaps(symbol, ranges)]
        )
        display_symbols = changed_symbols[:12] if changed_symbols else symbols[:12]
        dirty_kinds = []
        if path in source_git_state.staged_files:
            dirty_kinds.append("staged")
        if path in source_git_state.unstaged_files:
            dirty_kinds.append("unstaged")
        if path in source_git_state.untracked_files:
            dirty_kinds.append("untracked")
        target_entries.append(
            {
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
                "cochanges": soul_map.cochanges_for_file(path),
                "analysis_repo": str(analysis_repo),
            }
        )
    return target_entries


def build_gitnexus_plan(
    target_entries: list[dict[str, object]],
    repo_name: str | None = None,
) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in target_entries:
        path = str(entry["path"])
        symbols = entry.get("changed_symbols") or entry.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            if path not in seen:
                item = {"kind": "file_context", "target": path}
                if repo_name:
                    item["repo"] = repo_name
                plan.append(item)
                seen.add(path)
            continue
        for symbol in symbols:
            if not isinstance(symbol, dict):
                continue
            name = symbol.get("name")
            if not isinstance(name, str) or not name or name in seen:
                continue
            if symbol.get("kind") not in {"function", "class", "method"}:
                continue
            context_item = {"kind": "symbol_context", "target": name, "file": path}
            impact_item = {"kind": "symbol_impact", "target": name, "direction": "upstream"}
            if repo_name:
                context_item["repo"] = repo_name
                impact_item["repo"] = repo_name
            plan.append(context_item)
            plan.append(impact_item)
            seen.add(name)
            if len(plan) >= 20:
                return plan
    return plan


def build_gitnexus_section(
    plan: list[dict[str, str]],
    target_state: TargetState,
    repo_name: str | None,
) -> dict[str, object]:
    return {
        "status": "planned",
        "repo": repo_name or target_state.source_repo.name,
        "expected_repo_path": str(target_state.analysis_repo),
        "expected_head_sha": target_state.head_sha,
        "stale_index_policy": "block_or_reindex_before_trusting_impact",
        "plan": plan,
    }


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
) -> dict[str, object]:
    target_state = resolve_target_state(repo, mode, base_ref, head_ref, cache_dir)
    gitignore_dirty_before_build = ".gitignore" in dirty_paths(target_state.analysis_repo)
    build_result = build_soulforge_map(
        target_state.analysis_repo,
        soulforge_bin,
        map_build,
        map_timeout_ms,
    )
    if mode == "pr" or not gitignore_dirty_before_build:
        cleanup_soulforge_gitignore_change(target_state.analysis_repo)
    soul_map = SoulForgeMap(target_state.analysis_repo)
    if not soul_map.available and not allow_missing_map:
        raise RuntimeError(
            "SoulForge map is required but unavailable for "
            f"{target_state.analysis_repo}. Use --allow-missing-map to continue without it."
        )

    source_git_state = read_git_state(target_state.source_repo, base_ref, head_ref)
    targets = target_files_for_mode(mode, source_git_state, soul_map, intent, top)
    target_entries = make_target_entries(
        mode=mode,
        source_repo=target_state.source_repo,
        analysis_repo=target_state.analysis_repo,
        base_ref=base_ref,
        head_ref=head_ref,
        source_git_state=source_git_state,
        soul_map=soul_map,
        targets=targets,
    )
    plan = build_gitnexus_plan(target_entries, gitnexus_repo)

    warnings = []
    if build_result.warning:
        warnings.append(build_result.warning)
    if mode == "pr" and target_state.source_dirty:
        warnings.append("source worktree is dirty; PR target map was built from clean cached worktree")
    if mode != "pr" and target_state.target_dirty:
        warnings.append("analysis uses dirty local worktree by design")
    if mode == "intent" and not targets:
        warnings.append("intent mode found no targets; refine --intent or build a map")

    return {
        "schema_version": 1,
        "repo": str(target_state.source_repo),
        "mode": mode,
        "scope": "pr" if mode == "pr" else ("dirty" if mode == "local" else "intent"),
        "intent": intent,
        "token_budget": token_budget,
        "warnings": warnings,
        "target_state": {
            "source_repo": str(target_state.source_repo),
            "analysis_repo": str(target_state.analysis_repo),
            "base_ref": base_ref,
            "head_ref": head_ref,
            "head_sha": target_state.head_sha,
            "source_dirty": target_state.source_dirty,
            "target_dirty": target_state.target_dirty,
            "cache_key": target_state.cache_key,
        },
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
        },
        "targets": target_entries,
        "gitnexus": build_gitnexus_section(plan, target_state, gitnexus_repo),
        "gitnexus_plan": plan,
    }


def render_target_lines(target: dict[str, object], *, max_symbols: int = 8) -> list[str]:
    lines = []
    rank = target["rank"] if target["rank"] is not None else "not indexed"
    lines.append(f"### {target['path']}")
    lines.append(f"- map rank: {rank}")
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
    assert isinstance(git, dict)
    assert isinstance(soulforge, dict)
    assert isinstance(target_state, dict)
    assert isinstance(gitnexus, dict)
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
            "",
            "## SoulForge Map",
            "",
        ]
    )
    stats = soulforge["stats"]
    assert isinstance(stats, dict)
    if stats.get("available"):
        lines.append(f"- db: `{stats['path']}`")
        for key in ("files", "symbols", "edges", "refs", "cochanges", "calls", "summaries"):
            if key in stats:
                lines.append(f"- {key}: {stats[key]}")
    else:
        lines.append(f"- unavailable: `{stats['path']}`")

    lines.extend(["", "## Targets", ""])
    targets = packet["targets"]
    assert isinstance(targets, list)
    for target in targets:
        assert isinstance(target, dict)
        lines.extend(render_target_lines(target))
        lines.append("")

    lines.extend(["## GitNexus Required Checks", ""])
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


def render_prompt(packet: dict[str, object]) -> str:
    target_state = packet["target_state"]
    gitnexus = packet["gitnexus"]
    assert isinstance(target_state, dict)
    assert isinstance(gitnexus, dict)
    targets = packet["targets"]
    assert isinstance(targets, list)

    lines = [
        '<repo_context_packet schema_version="1">',
        "  <target_state>",
        f"    <mode>{html.escape(str(packet['mode']))}</mode>",
        f"    <source_repo>{html.escape(str(target_state['source_repo']))}</source_repo>",
        f"    <analysis_repo>{html.escape(str(target_state['analysis_repo']))}</analysis_repo>",
        f"    <base_ref>{html.escape(str(target_state['base_ref']))}</base_ref>",
        f"    <head_ref>{html.escape(str(target_state['head_ref']))}</head_ref>",
        f"    <head_sha>{html.escape(str(target_state['head_sha']))}</head_sha>",
        f"    <source_dirty>{str(target_state['source_dirty']).lower()}</source_dirty>",
        f"    <target_dirty>{str(target_state['target_dirty']).lower()}</target_dirty>",
        "  </target_state>",
        "  <scope_rules>",
        "    Use files under <targets> as the first-pass edit/review surface.",
        "    Treat source dirty overlaps as warnings, not PR target files, when mode is pr.",
        "    Run the GitNexus required checks before editing production code.",
        "  </scope_rules>",
        "  <warnings>",
    ]
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
    return "\n".join(lines) + "\n"


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
    analyze.add_argument("--mode", choices=["pr", "local", "intent"], default="pr")
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

    wrap = subcommands.add_parser("wrap", help="Generate a prompt packet and optionally run a command")
    wrap.add_argument("--repo", required=True, type=Path)
    wrap.add_argument("--mode", choices=["pr", "local", "intent"], default="pr")
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
        )
        output_text(render_packet(packet, args.format), args.out)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
