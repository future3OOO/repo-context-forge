# Repo Context Forge

Standalone context orchestrator for giving coding agents the right repository
map before they reason, edit, or run GitNexus.

The tool is dependency-free Python and can be run against any git repository.
Its workflow index owns deterministic target ranking and source symbols. It
indexes an exact head for PR/repo mode and the explicit dirty overlay for
local/intent mode. Optional SoulForge data enriches graph impact without
becoming a startup dependency.

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
3. The bootstrap script auto-selects `pr`, `local`, `intent`, or `repo` mode.
4. Forge builds an atomic workflow index in the cache-owned analysis checkout.
5. The generated XML packet becomes the initial repo context for the task.
6. GitNexus freshness is checked for the exact target head before blast-radius
   claims are trusted.

In production plugin mode, the current git folder is the target. Clean folders
with no diff use `repo` mode for whole-repo context. Repo Context Forge does not
silently switch to sibling worktrees.

## What It Does

- `pr` mode creates or reuses a clean cached git checkout at the target head.
- `local` mode copies dirty local files into a cached analysis checkout and
  marks dirty state.
- `intent` mode searches a cached analysis map from a user-described change
  request.
- `repo` mode maps a clean current project folder for ambient whole-repo
  context.
- native ranking combines changed hunks, production/test role, workflow-index
  relevance, optional SoulForge graph/co-change data, semantic summaries, and
  task refresh signals.
- semantic summaries run in `full_cached` mode during bootstrap: cached
  LLM/LSP/AST/native summaries are used first, deterministic synthetic summaries
  fill missing symbols, and live LLM generation is not performed on routine
  prompt injection.
- output can be Markdown, JSON, or an XML prompt packet for upstream injection.
- prompt packets default to a 16k token budget and compact optional symbol
  detail before dropping required status, target identity, or GitNexus checks.
- packets execute their bounded GitNexus context/impact plan serially and include
  normalized, identity-bound semantic answers plus timing and output metrics.
- packets include workflow-index and architecture summaries, optional SoulForge
  target-head proof, and packet-authoritative GitNexus exact-head status.
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
--gitnexus-mode off|check|auto
--format markdown|json|prompt
```

By default, `analyze` requires a SoulForge map for backward compatibility. The
Codex bootstrap permits a missing map because the workflow index
still provides targets and symbols; only optional native graph impact is absent.

## Production Contract

For PR review, use `mode=pr`. It builds/reads the map from a clean cached target
checkout, not the dirty root checkout.

For active implementation before a PR exists, use `mode=local` or `mode=intent`.
These modes intentionally use ambient current-worktree context, but SoulForge
runs against a cached analysis checkout. The target repository is an input only:
Repo Context Forge must not add `.soulforge`, edit `.gitignore`, or run cleanup
checkouts in the source checkout. Intent-mode graph checks apply only to existing
selected files. A qualified existing symbol becomes required when its immediate
qualifier identifies one exact-name direct enclosing class candidate or one selected
file path, or when its terminal name is unique across selected targets. Matching uses
each selected file's complete extracted inventory. A planned symbol that does not
exist never synthesizes a symbol check; when the intent independently names an existing
file path, that file is required instead as context.
Required checks omitted by the bounded plan block through `unresolved_checks`, while
optional omissions remain non-blocking. Directory-derived `go.sum` and `Cargo.lock`
are excluded; explicitly named lockfiles remain required anchors.

For clean exploration, use `mode=repo` or let the plugin bootstrap select it.

GitNexus is not replaced by this tool. In `auto` mode, Repo Context Forge checks
whether the GitNexus index matches the packet target head and reindexes the
cache-owned analysis checkout when it is missing or stale. It then executes the
packet plan once. Stale, unavailable, malformed, ambiguous, or identity-mismatched
results produce one blocker instead of a successful packet. The bootstrap's
optional `--packet-json-out PATH` atomically writes the same machine packet while
normal prompt stdout remains unchanged. A blocker is still rendered and written,
then the production bootstrap exits nonzero.
