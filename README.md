# Repo Context Forge

Standalone context orchestrator for giving coding agents the right repository
map before they reason, edit, or run GitNexus.

The tool is dependency-free Python and can be run against any git repository. It
uses SoulForge as the first map engine through an adapter, but keeps SoulForge's
map separate from the target-selection and prompt-packet contracts.

## Codex Plugin

Install the plugin once:

```bash
python3 scripts/install_local_plugin.py
```

That registers this repo as the `repo-context-forge` Codex plugin in the local
plugin marketplace and marks it installed by default. After that, Codex can use
the plugin skill at the start of code work in any git repository. Users should
not need to run context commands per task.

The plugin startup path is:

1. Codex loads `repo-context-forge`.
2. The plugin skill runs `scripts/codex_context_bootstrap.py` for the current
   git repo.
3. The bootstrap script auto-selects `pr`, `local`, or `intent` mode.
4. The generated XML packet becomes the initial repo context for the task.
5. Codex runs the packet's GitNexus checks before editing when GitNexus MCP is
   available.

In production plugin mode, an ambiguous checkout fails closed. For example, a
detached clean checkout with no PR diff emits a blocker packet with sibling
worktree suggestions instead of pretending there is useful local context.

## What It Does

- `pr` mode creates or reuses a clean cached git worktree at the target head.
- `local` mode analyzes the current dirty worktree and marks dirty state.
- `intent` mode searches the ambient map from a user-described change request.
- native ranking combines changed hunks, production/test role, PageRank,
  SoulForge graph neighbors, co-change partners, semantic summaries, and task
  refresh signals.
- output can be Markdown, JSON, or an XML prompt packet for upstream injection.
- packets include GitNexus required-check entries for context and impact calls.
- `gitnexus-merge` merges real GitNexus findings and blocks stale blast-radius
  claims.
- `wrap` writes the prompt packet and can pass it to another command before
  that command starts reasoning.
- `scripts/codex_context_bootstrap.py` is the plugin entrypoint Codex uses to
  avoid manual per-project commands.
- `context-start`, `context-record-*`, and `context-refresh` provide the
  per-turn personalization layer.
- `benchmark` generates no-map, ambient-map, and clean-target-map comparison
  inputs for testing agent behavior.

## Usage

Analyze a PR/head target with a clean target map:

```bash
python3 repo_context_forge.py analyze \
  --repo /path/to/repo \
  --mode pr \
  --base upstream/main \
  --head HEAD
```

Generate an upstream prompt packet:

```bash
python3 repo_context_forge.py analyze \
  --repo /path/to/repo \
  --mode pr \
  --base upstream/main \
  --format prompt
```

Analyze local dirty development before a PR exists:

```bash
python3 repo_context_forge.py analyze \
  --repo /path/to/repo \
  --mode local \
  --format markdown
```

Map an intent before implementation:

```bash
python3 repo_context_forge.py analyze \
  --repo /path/to/repo \
  --mode intent \
  --intent "Add support for updating Gmail drafts while preserving omitted fields"
```

Generate benchmark inputs:

```bash
python3 repo_context_forge.py benchmark \
  --repo /path/to/repo \
  --base upstream/main \
  --intent "Review the Gmail draft lifecycle changes"
```

Run another command with the packet injected through stdin and metadata exposed
through environment variables:

```bash
python3 repo_context_forge.py wrap \
  --repo /path/to/repo \
  --mode pr \
  --base upstream/main \
  --gitnexus-repo my_index_name \
  -- your-agent-command
```

`wrap` sets `REPO_CONTEXT_FORGE_PACKET_FILE`,
`REPO_CONTEXT_FORGE_ANALYSIS_REPO`, `REPO_CONTEXT_FORGE_TARGET_SHA`, and
`REPO_CONTEXT_FORGE_GITNEXUS_REPO` for the wrapped command.

## Important Options

```bash
--map-build auto|always|never
--allow-missing-map
--cache-dir ~/.cache/repo-context-forge
--soulforge-bin /path/to/soulforge
--gitnexus-repo fork_google_workspace_mcp
--format markdown|json|prompt
```

By default, `analyze` requires a SoulForge map. Use `--allow-missing-map` only
when testing failure paths or no-map baselines.

## Production Contract

For PR review, use `mode=pr`. It builds/reads the map from a clean cached target
worktree, not the dirty root checkout.

For active implementation before a PR exists, use `mode=local` or `mode=intent`.
These modes intentionally use ambient current-worktree context and mark dirty
state in the packet.

GitNexus is not replaced by this tool. The packet tells the agent which GitNexus
context and impact checks must run after the target map has been injected.
