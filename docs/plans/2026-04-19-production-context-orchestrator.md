# Production Context Orchestrator Plan

Date: 2026-04-19
Project: Repo Context Forge
Status: implementation in progress

## Objective

Ship a standalone production tool that gives Codex or any agent the right codebase
context before it reasons, edits, or runs GitNexus.

The tool must support:

- whole-repo ambient mapping for local work before a PR exists
- clean target mapping for PR/head review
- intent-based mapping when the user describes a change before code exists
- prompt packet rendering before GitNexus is called
- GitNexus blast-radius and process verification after target context exists
- reusable operation across arbitrary git repositories

WorkspaceMCP is the first acceptance fixture, not a special case.

## Scope

In scope:

- standalone CLI
- isolated git worktree target resolution
- SoulForge-map adapter as the first map engine
- token-budgeted prompt packet renderer
- GitNexus bridge interface
- Codex/plugin-ready output contract
- benchmark harness for no-map, ambient-map, and clean-target-map comparisons

Out of scope for the first production pass:

- reimplementing SoulForge tree-sitter indexing from scratch
- replacing GitNexus graph storage
- supporting non-git directories
- automatic PR creation or publishing
- editing target repositories

## Trusted Base And Target Branch

This project is not yet initialized as a git repository. Before implementation
commits begin:

- initialize this project as its own git repository or move it into the intended
  hosting repository
- trusted base: the first clean commit containing the current prototype and this
  plan
- target branch: `codex/production-context-orchestrator`

Do not treat WorkspaceMCP as this tool's source repository. WorkspaceMCP is only
the test bench.

## Delivery Map

| Slice | Type | Ownership | Purpose |
| --- | --- | --- | --- |
| PR 1 | foundation/runtime | target resolver and map builder | Create correct target maps from clean worktrees and ambient dirty worktrees |
| PR 2 | runtime/operator UX | prompt packet and GitNexus bridge | Render upstream context packets and attach GitNexus verification |
| PR 3 | proof/tests/docs | plugin wrapper and benchmark harness | Prove agent behavior improves and document production use |

Maximum active stack depth: 3.

Consolidation trigger: if any slice needs a second broad rewrite, stop stacking
and consolidate the remaining work into one PR.

Deploy freeze rule: no production/plugin rollout until PR 3 benchmark evidence
shows clean-target context beats ambient-map and no-map baselines on WorkspaceMCP.

## PR Structure And Ownership

### PR 1: Target Maps

Branch: `codex/context-target-maps`

Owns:

- target resolver
- cache directory layout
- clean git worktree creation
- ambient dirty worktree mode
- SoulForge adapter that can build/read a map for the selected target
- map snapshot metadata
- WorkspaceMCP PR-head fixture run

Does not own:

- GitNexus calls
- model prompt benchmarking
- plugin packaging

Required behavior:

- `mode=pr` creates or reuses a detached clean worktree at `head`
- `mode=local` uses the current worktree and marks dirty state explicitly
- `mode=intent` starts from ambient whole-repo map plus search terms
- every map snapshot records repo path, base ref, head ref, head SHA, dirty status,
  map engine version, and ignore rules
- if the target worktree is dirty in PR mode, fail closed

Commit structure:

1. add target resolver and cache layout
2. add SoulForge map builder adapter
3. add WorkspaceMCP fixture command and tests

Verification:

- unit tests for target resolution and cache keys
- integration test that WorkspaceMCP PR mode maps a clean `HEAD`, not the dirty
  root checkout
- integration test that local mode reports staged, unstaged, and untracked files
- repeated run reuses the same snapshot when inputs are unchanged

### PR 2: Prompt Packet And GitNexus Bridge

Branch: `codex/context-prompt-gitnexus`

Owns:

- prompt packet schema
- token-budgeted renderer
- upstream injection payload
- GitNexus bridge interface
- GitNexus stale-index checks
- merged context packet containing map context plus blast radius

Does not own:

- final plugin distribution
- benchmark report

Required behavior:

- render these sections in order:
  - ambient map summary
  - target map summary
  - change or intent overlay
  - allowed files
  - out-of-scope dirty files
  - GitNexus required checks
  - GitNexus results when available
- packet must be consumable as:
  - Markdown for humans
  - JSON for tools
  - prompt XML block for agents
- GitNexus is never run against an implicit checkout; packet must state the repo
  path and expected SHA
- stale GitNexus index is reported as a blocking warning unless the caller opts
  into reindex

Commit structure:

1. add packet schema and renderer
2. add GitNexus bridge contract and command output
3. add WorkspaceMCP merged packet fixture

Verification:

- renderer budget tests
- JSON schema or structural validation tests
- WorkspaceMCP packet includes correct PR files before GitNexus results
- stale GitNexus index is visible in the packet
- no dirty root files appear as target files in PR mode

### PR 3: Plugin Wrapper And Benchmark

Branch: `codex/context-plugin-benchmark`

Owns:

- Codex/plugin wrapper contract
- MCP server or command wrapper, whichever is fastest to wire first
- benchmark harness
- documentation and runbooks
- WorkspaceMCP A/B/C benchmark evidence

Required behavior:

- run these benchmark modes with same prompt, model, step limit, and repo target:
  - no map
  - ambient whole-repo map
  - clean target map plus overlay
- record:
  - first files named by the model
  - first symbols named by the model
  - first search/GitNexus calls
  - irrelevant file rate
  - dirty-contamination rate
  - test suggestions
  - final target-surface accuracy
- provide a wrapper path where the packet is injected before the agent reasons
  or calls GitNexus

Commit structure:

1. add wrapper entrypoint
2. add benchmark harness and WorkspaceMCP fixture
3. add production docs and acceptance report

Verification:

- no-map baseline produces broad or low-confidence context on WorkspaceMCP
- ambient-map baseline improves orientation but can show dirty contamination
- clean-target-map mode produces correct PR target files and reduced irrelevant
  symbols
- benchmark output is deterministic enough for regression checks

## Active Branch Order

1. `codex/context-target-maps`
2. `codex/context-prompt-gitnexus`
3. `codex/context-plugin-benchmark`

Do not begin PR 2 until PR 1 can produce a clean WorkspaceMCP PR-head map.
Do not begin PR 3 until PR 2 can emit a packet with GitNexus verification fields.

## Simplest Complete Runtime Flow

### PR Review Mode

```text
repo-context-forge analyze --mode pr --repo <repo> --base <base> --head <head>

1. Resolve base/head.
2. Create clean detached worktree at head.
3. Build SoulForge map inside that worktree.
4. Generate diff overlay from base...head.
5. Render target prompt packet.
6. Check GitNexus index for the target checkout.
7. Run GitNexus context/impact for changed symbols.
8. Emit merged packet before the agent reviews or edits.
```

### Local Development Mode

```text
repo-context-forge analyze --mode local --repo <repo>

1. Use current worktree.
2. Build or read ambient whole-repo map.
3. Detect staged, unstaged, and untracked files.
4. Mark all dirty state explicitly.
5. Render prompt packet for before-edit planning.
6. Run GitNexus impact only after target symbols are chosen.
```

### Intent Mode

```text
repo-context-forge analyze --mode intent --repo <repo> --intent "<user request>"

1. Build or read ambient whole-repo map.
2. Search map symbols, file paths, summaries, and co-change partners.
3. Ask GitNexus query for matching execution flows.
4. Produce target overlay with candidate files and required verification.
5. Inject packet before the agent starts implementation.
```

## GitNexus Contract

GitNexus is authoritative for:

- symbol context
- upstream and downstream impact
- execution processes
- API/tool/route maps when available
- stale-index detection

SoulForge-style maps are authoritative only for:

- prompt orientation
- ranked file/symbol summaries
- co-change hints
- likely target discovery

The bridge must never present SoulForge blast-radius hints as GitNexus proof.

## Prompt Injection Contract

The production packet must be available before the model selects files or calls
GitNexus.

Minimum injection shape:

```xml
<repo_context_packet>
  <target_state mode="pr|local|intent" repo="..." sha="..."/>
  <scope_rules>...</scope_rules>
  <ambient_map>...</ambient_map>
  <target_map>...</target_map>
  <change_overlay>...</change_overlay>
  <gitnexus_required_checks>...</gitnexus_required_checks>
</repo_context_packet>
```

MCP-only integration is not sufficient for final production because MCP is called
after the model has already started reasoning. MCP may still be provided as a
secondary interface.

## Verification Gates Per PR

Every PR:

- `python3 -m unittest discover -s tests -v`
- command help smoke test
- WorkspaceMCP fixture smoke test
- no generated cache files committed

PR 1 additional gates:

- clean target worktree test
- dirty local worktree test
- missing SoulForge binary fails with actionable error

PR 2 additional gates:

- prompt packet snapshot test
- GitNexus stale-index warning test
- JSON packet structural test

PR 3 additional gates:

- A/B/C benchmark run on WorkspaceMCP
- plugin/wrapper smoke test
- production runbook review

## Affected Surface

Implementation will affect:

- local git worktree management
- external SoulForge invocation
- external GitNexus invocation or MCP adapter
- prompt content that influences model behavior
- benchmark reproducibility

Adjacent no-change surfaces:

- target repositories must not be modified by analysis
- dirty user work must not be cleaned, staged, or reverted
- GitNexus indexes must not be silently used for the wrong SHA
- generated map caches must not be committed into target repos

## Risk Checks

- Wrong checkout: packet records repo path, head SHA, base, and dirty state.
- Dirty contamination: PR mode fails closed if target worktree is dirty.
- Stale GitNexus: packet flags stale index before impact claims.
- Prompt drift: benchmark records first files and first symbols, not just final answer.
- Slow map build: cache snapshots by repo, SHA, ignore rules, and map engine version.
- Engine coupling: SoulForge adapter is behind a map-engine interface.

## Execution Checklist

- [ ] Initialize or move project into the final source repository.
- [x] PR 1: implement target resolver and clean worktree map snapshots.
- [x] PR 1: prove WorkspaceMCP clean PR-head map differs from dirty ambient map.
- [x] PR 2: implement prompt packet schema and renderer.
- [x] PR 2: implement GitNexus bridge planning and expected-target reporting.
- [x] PR 2: prove merged packet contains target context before GitNexus claims.
- [x] PR 3: implement wrapper/plugin entrypoint.
- [x] PR 3: implement A/B/C benchmark harness.
- [x] PR 3: run WorkspaceMCP benchmark and record acceptance evidence.
- [x] PR 3: document production usage and failure modes.

## WorkspaceMCP Acceptance Evidence

Commands run on April 19, 2026:

- `python3 -m unittest discover -s tests -v`: 18 tests passed after adding the Codex plugin manifest, skill, bootstrap script, and local installer.
- `python3 -m json.tool .codex-plugin/plugin.json`: validated the plugin manifest.
- `python3 scripts/codex_context_bootstrap.py --help`: validated the plugin bootstrap entrypoint.
- `python3 scripts/codex_context_bootstrap.py --repo /home/prop_/projects/repo-context-forge --map-build never`: verified local dirty-worktree packet generation.
- `python3 scripts/codex_context_bootstrap.py --repo /home/prop_/projects/fork_google_workspace_mcp --base upstream/main --head HEAD --map-build auto --gitnexus-repo fork_google_workspace_mcp`: verified clean PR-head packet generation through the plugin bootstrap.
- `python3 scripts/install_local_plugin.py`: registered the plugin in `/home/prop_/.agents/plugins/marketplace.json` through `/home/prop_/plugins/repo-context-forge`.
- `python3 repo_context_forge.py analyze --repo /home/prop_/projects/fork_google_workspace_mcp --mode pr --base upstream/main --head HEAD --map-build auto --format prompt --gitnexus-repo fork_google_workspace_mcp --out workspace-mcp-pr-clean-map.prompt.xml`: generated a clean PR-head prompt packet.
- `python3 repo_context_forge.py benchmark --repo /home/prop_/projects/fork_google_workspace_mcp --base upstream/main --head HEAD --intent "Review the Gmail draft lifecycle changes" --map-build auto --format json --gitnexus-repo fork_google_workspace_mcp --out workspace-mcp-benchmark-map.json`: generated A/B/C benchmark output.
- `python3 repo_context_forge.py wrap --repo /home/prop_/projects/fork_google_workspace_mcp --mode pr --base upstream/main --head HEAD --map-build auto --gitnexus-repo fork_google_workspace_mcp --out workspace-mcp-wrapper.prompt.xml -- python3 -c ...`: verified wrapper stdin injection and environment metadata.

Observed benchmark result:

- `no_map`: 0 target files, no map guidance.
- `ambient_map`: 12 target files, dirty contamination true; includes dirty/untracked files such as `.gitignore`, `AGENTS.md`, `README.md`, and unrelated staged surfaces.
- `clean_target_map`: 2 target files, dirty contamination false; targets only `gmail/gmail_tools.py` and `tests/gmail/test_draft_gmail_message.py`.

Prompt packet evidence:

- target SHA: `6a05817e3cbfc5345dba5367fcaa70365ad4d8ad`
- analysis repo: `/home/prop_/.cache/repo-context-forge/worktrees/fork_google_workspace_mcp-6a05817e3cbf-cb3f28913a473fa4`
- source dirty: true
- target dirty: false
- GitNexus required checks include symbol context and upstream impact checks for Gmail draft lifecycle symbols before any GitNexus claims are trusted.

## Open Questions

- Final hosting location for this standalone project is not set.
- Final plugin integration target should be decided after PR 2: Codex plugin,
  MCP server, or both.
- Whether to keep using SoulForge as the long-term map engine depends on adapter
  stability after PR 1.
