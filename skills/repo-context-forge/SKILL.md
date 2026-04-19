---
name: repo-context-forge
description: Use at the start of coding, debugging, review, refactor, or repo exploration tasks inside a git repository to inject a targeted Repo Context Forge packet before deciding files, edits, or GitNexus queries.
---

# Repo Context Forge

Use this skill before codebase reasoning when the user asks to edit, review,
debug, refactor, explain, or plan work in a git repository.

## Required Startup Flow

1. Resolve this skill's directory, then run the bootstrap script at:

```bash
python3 ../../scripts/codex_context_bootstrap.py --repo "$PWD" --enforce-intake
```

The path is relative to this `SKILL.md`.

2. Treat the script output as the initial repository context packet for the
current task. The output begins with `REPO_CONTEXT_FORGE_REQUIRED_INTAKE`; that
banner is the enforced startup contract and must be reported before any code
reasoning, review findings, edits, or GitNexus claims.

If the script exits non-zero and emits a `<blocker>`, stop normal repo analysis
and surface the blocker. Do not continue with an empty or detached checkout
packet.

3. Follow the packet's `<scope_rules>`:

- before code reasoning, review, or GitNexus calls, surface a short intake from
  `<context_digest>`: mode, head SHA, token budget, semantic source counts, top
  targets, SoulForge impact headline, and GitNexus repo/status
- use `<targets>` as the first-pass edit/review surface
- use `<soulforge_impact>` as native SoulForge blast-radius context before
  editing or reviewing selected files
- use `<semantic_summaries>` and each symbol's summary source as injected
  context; `full_cached` means cached LLM/LSP/AST/native summaries are used
  first and deterministic synthetic fill is used without live LLM calls
- use `<gitnexus_status><repo>` as the repo value for every GitNexus MCP call
- in `pr` mode, do not treat dirty source-worktree files as PR targets
- run the listed `<gitnexus_required_checks>` before editing production code
  when GitNexus MCP tools are available

4. If the packet says SoulForge is unavailable, continue only with the explicit
fallback surface in the packet and say that symbol-level map data was missing.

## Mode Selection

The bootstrap script auto-selects the mode:

- `pr`: a base ref is available and `base...HEAD` has changed files
- `local`: dirty local work exists before a PR is pushed
- `intent`: pass `--intent "<user request>"` when there are no changes yet and
  the user's request describes planned work
- `repo`: clean current folder with no diff or intent; use whole-repo context

Packets are generated from a cache-owned analysis checkout. Treat the user's
checkout as read-only input; Repo Context Forge must not leave `.soulforge` or
`.gitignore` changes in it.

Do not switch to a sibling worktree unless the user explicitly asks. The current
git folder is the target.

For a user-described implementation before edits, prefer:

```bash
python3 ../../scripts/codex_context_bootstrap.py --repo "$PWD" --intent "<task>" --enforce-intake
```

## GitNexus Follow-Up

For each `<check>` in `<gitnexus_required_checks>`:

- `kind="symbol_context"` means call `gitnexus_context` for that symbol/file
- `kind="symbol_impact"` means call `gitnexus_impact` upstream for that symbol
- if a GitNexus MCP call says the repo is missing or stale, rerun the bootstrap
  with `--gitnexus-mode auto`, then retry using the new `<gitnexus_status><repo>`
- do not call `gitnexus_list_repos` during normal recovery; the packet repo is
  authoritative
- cite SoulForge impact separately from GitNexus impact when reporting review
  evidence; SoulForge explains repo-map blast radius, while GitNexus validates
  execution-flow impact
- cite summary source when semantic summaries materially affect target choice
  or code reasoning
- do not use `gitnexus_detect_changes(compare)` as PR-diff authority when it
  conflicts with live `base...HEAD` files or the packet's `pr` targets; use it
  only as additional graph evidence after the packet target surface is fixed
- trust blast-radius claims only when `<gitnexus_status>` is `fresh` or
  `reindexed` and `required_checks_resolved` is true

## Turn Refresh

When Codex materially changes context during a longer task, record it before
refreshing the packet. Start a task context with an explicit task id:

```bash
python3 ../../repo_context_forge.py context-start --repo "$PWD" --task-id <id>
python3 ../../repo_context_forge.py context-record-read --repo "$PWD" --task-id <id> <path>
python3 ../../repo_context_forge.py context-record-search --repo "$PWD" --task-id <id> <path>
python3 ../../repo_context_forge.py context-record-edit --repo "$PWD" --task-id <id> <path>
python3 ../../repo_context_forge.py context-refresh --repo "$PWD" --task-id <id>
```

The refreshed packet boosts edited, read, searched, and mentioned files while
preserving changed-hunk priority.

## Do Not

- Do not ask the user to manually run Repo Context Forge commands for routine
  repo work.
- Do not edit files before reading the packet and running required GitNexus
  checks when they are available.
- Do not use dirty local map results as PR-head truth.
