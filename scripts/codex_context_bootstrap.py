#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import repo_context_forge as forge  # noqa: E402


BASE_CANDIDATES = ("upstream/main", "origin/main", "main", "master")


def git_output(repo: Path, args: list[str]) -> str:
    proc = forge.run_cmd(["git", *args], cwd=repo, allow_fail=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def first_existing_base(repo: Path, requested: str | None) -> str | None:
    candidates = [requested] if requested else []
    candidates.extend(BASE_CANDIDATES)
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if git_output(repo, ["rev-parse", "--verify", candidate]):
            return candidate
    return None


def has_pr_changes(repo: Path, base_ref: str, head_ref: str) -> bool:
    return bool(git_output(repo, ["diff", "--name-only", f"{base_ref}...{head_ref}"]))


def context_surface_paths(git_state: forge.GitState) -> list[str]:
    return [
        path
        for path in forge.unique_ordered(
            [
                *git_state.pr_files,
                *git_state.staged_files,
                *git_state.unstaged_files,
                *git_state.untracked_files,
            ]
        )
        if not forge.is_generated_or_cache_path(path)
    ]


def is_user_worktree(path: str, cache_dir: Path) -> bool:
    resolved = Path(path).resolve()
    cache_root = cache_dir.resolve()
    return resolved != cache_root and cache_root not in resolved.parents


def choose_mode(
    repo: Path,
    requested_mode: str,
    base_ref: str | None,
    head_ref: str,
    intent: str | None,
) -> forge.Mode:
    if requested_mode != "auto":
        return requested_mode  # type: ignore[return-value]
    if base_ref and has_pr_changes(repo, base_ref, head_ref):
        return "pr"
    if forge.is_dirty(repo, ignore_tool_cache=True):
        return "local"
    if intent:
        return "intent"
    return "repo"


def should_block_empty_checkout(
    repo: Path,
    mode: forge.Mode,
    base_ref: str | None,
    head_ref: str,
    intent: str | None,
) -> str | None:
    if mode == "repo":
        return None
    if mode == "pr":
        return None
    git_state = forge.read_git_state(repo, base_ref or head_ref, head_ref)
    has_any_surface = bool(context_surface_paths(git_state) or intent)
    if has_any_surface:
        return None
    if forge.is_detached(repo):
        return "detached checkout has no target surface; select the active PR worktree"
    return "no changed files, dirty files, or intent were available for context mapping"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="codex-context-bootstrap")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=["auto", "pr", "local", "intent", "repo"], default="auto")
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--intent")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--token-budget", type=int)
    parser.add_argument("--conversation-tokens", type=int)
    parser.add_argument("--cache-dir", type=Path, default=forge.DEFAULT_CACHE_DIR)
    parser.add_argument("--soulforge-bin")
    parser.add_argument("--map-build", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--map-timeout-ms", type=int, default=120_000)
    parser.add_argument("--require-map", action="store_true")
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--gitnexus-repo")
    parser.add_argument("--gitnexus-mode", choices=["off", "check", "auto"], default="auto")
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo = args.repo.resolve()
    if not forge.is_git_repo(repo):
        print(f"not a git repository: {repo}", file=sys.stderr)
        return 2

    root = forge.repo_root(repo)
    base_ref = first_existing_base(root, args.base)
    mode = choose_mode(root, args.mode, base_ref, args.head, args.intent)
    if mode == "pr" and not base_ref:
        mode = "local"
    if mode == "intent" and not args.intent:
        mode = "repo"
    if not args.allow_empty:
        blocker_reason = should_block_empty_checkout(root, mode, base_ref, args.head, args.intent)
        if blocker_reason:
            packet = forge.make_blocker_packet(
                root,
                reason=blocker_reason,
                base_ref=base_ref,
                head_ref=args.head,
                suggestions=[
                    item
                    for item in forge.worktree_suggestions(root)
                    if item.get("path") != str(root)
                    and is_user_worktree(item.get("path", ""), args.cache_dir)
                ],
            )
            forge.output_text(forge.render_prompt(packet), args.out)
            return 1

    token_budget = forge.compute_token_budget(args.conversation_tokens, args.token_budget)
    packet = forge.make_packet(
        root,
        mode=mode,
        base_ref=base_ref or args.head,
        head_ref=args.head,
        intent=args.intent,
        top=args.top,
        token_budget=token_budget,
        cache_dir=args.cache_dir.resolve(),
        soulforge_bin=forge.find_soulforge_binary(args.soulforge_bin),
        map_build=args.map_build,
        map_timeout_ms=args.map_timeout_ms,
        allow_missing_map=not args.require_map,
        gitnexus_repo=args.gitnexus_repo,
        gitnexus_mode=args.gitnexus_mode,
    )
    rendered = forge.render_prompt(packet)
    forge.output_text(rendered, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
