# Native SoulForge Codex Production Plan

Date: 2026-04-19
Project: Repo Context Forge
Status: implementation in progress

## Objective

Ship Repo Context Forge as a production Codex plugin that reproduces native
SoulForge repo-map behavior for Codex, then fuses that context with GitNexus
verification before edits or review conclusions.

The production build must provide:

- automatic context bootstrap for any git repository Codex is working in
- fail-closed checkout validation before mapping
- clean PR-head mapping isolated from dirty source worktrees
- whole-repo ambient mapping for pre-PR implementation work
- intent-based mapping when no files have changed yet
- SoulForge-equivalent graph ranking, co-change ranking, full-text relevance,
  exported-symbol rendering, blast-radius tags, semantic summaries, and token
  budgeting
- per-turn personalization from files read, files edited, searches, mentioned
  paths, changed hunks, active task intent, and GitNexus findings
- prompt injection before substantive code reasoning
- GitNexus context/impact checks after the map is injected and before editing
- repeatable benchmarks proving ranking quality against WorkspaceMCP and
  Property Partner Ops

The current CLI/plugin is a prototype baseline. It is not production complete
until the acceptance gates in this plan pass.

## Non-Negotiable Product Contract

- Do not emit an empty "useful" packet for a detached or wrong checkout.
- Do not treat `.soulforge` cache files as target files.
- Do not use dirty source files as PR-head truth.
- Do not rank broad test classes above changed production control flow unless
  the PR is test-only.
- Do not claim blast radius from SoulForge alone; GitNexus must verify it.
- Do not require users to remember long commands per repository.
- Do not add a second map engine while SoulForge can supply the needed data.
- Do not ship a hidden best-effort mode that silently skips missing map data in
  production paths.

## Native SoulForge Parity Matrix

| SoulForge behavior | Production requirement in Repo Context Forge |
| --- | --- |
| Startup indexing | Plugin bootstrap indexes the selected repo/clean worktree before reasoning. |
| `.gitignore` aware walking | Preserve SoulForge ignore behavior and prevent cache leakage into target lists. |
| Tree-sitter symbols | Read SoulForge symbol data and expose exported symbols with signatures. |
| Import/reference graph | Use SoulForge edges for graph proximity and dependent-count tags. |
| PageRank | Use PageRank as one ranking input, not the sole target selector. |
| Co-change history | Boost co-change partners of changed/read/mentioned files. |
| Conversation personalization | Persist per-task reads, edits, searches, mentions, and intent; refresh ranking. |
| Edited-file boost | Edited files and their graph neighbors get highest refresh priority. |
| Mentioned/read-file boost | Tool reads, grep hits, and prompt mentions update ranking state. |
| Full-text relevance | Search map text/symbols for task terms and merge with graph ranking. |
| Prompt token budget | Render more context at task start and less as conversation tokens grow. |
| Exported symbol rendering | Show exported symbols first, then changed/internal symbols when relevant. |
| Blast-radius tags | Render dependent counts from graph/GitNexus, with source labelled. |
| Semantic summaries | Use SoulForge cached summaries when present; otherwise omit, do not fake. |
| Real-time updates | Refresh map after file edits/search/read events; reindex changed files. |

## Current Gap Summary

The existing implementation differs from native SoulForge in four material ways:

1. It is a one-shot packet, not a per-turn personalized repo map.
2. It uses SoulForge data but does not yet reproduce SoulForge ranking signals.
3. It can produce a low-value local packet on a detached clean checkout instead
   of failing closed.
4. It depends on Codex invoking the plugin skill; production requires a proven
   pre-reasoning startup path or a blocked release if the host cannot provide
   one.

## Trusted Base And Target Branches

Trusted base: `main` at `f8e1777` or the current clean successor commit.

Target branches:

1. `codex/native-startup-checkout`
2. `codex/native-map-ranking`
3. `codex/native-turn-refresh`
4. `codex/native-gitnexus-fusion`
5. `codex/native-benchmark-hardening`

Maximum active stack depth: 3.

Consolidation trigger: if PR 2 or PR 3 needs a second broad rewrite after
review starts, stop stacking and consolidate remaining native-runtime work into
one branch before continuing.

Deploy freeze rule: do not publish or recommend this plugin as production-ready
until PR 5 proves clean-target native ranking beats the current static packet on
WorkspaceMCP and Property Partner Ops.

## Delivery Map

| PR | Type | Ownership | Mergeable Outcome |
| --- | --- | --- | --- |
| PR 1 | foundation/runtime | startup, checkout safety, production config | plugin never maps the wrong checkout silently |
| PR 2 | core runtime | SoulForge data adapter and ranking engine | accurate native-style file/symbol ranking |
| PR 3 | core runtime/operator UX | per-turn state and prompt refresh | context adapts after reads/searches/edits |
| PR 4 | runtime/verification | GitNexus fusion | blast radius is verified and mismatch-aware |
| PR 5 | proof/tests/docs | benchmark, acceptance fixtures, release hardening | production readiness is measurable |

This is the smallest coherent split. PR 1 protects correctness. PR 2 delivers
ranking quality. PR 3 delivers native dynamic behavior. PR 4 keeps GitNexus
verification separate from map generation. PR 5 proves the system.

## PR 1: Native Startup And Checkout Safety

Branch: `codex/native-startup-checkout`

Owns:

- plugin bootstrap contract
- checkout classifier
- strict production mode
- wrong-checkout blocker packets
- sibling worktree discovery
- minimal plugin install/update path
- production config defaults

Required behavior:

- detect these checkout states:
  - attached branch with PR diff
  - attached dirty local worktree
  - detached clean checkout
  - detached dirty checkout
  - no base ref
  - no target surface
- in strict plugin mode, detached clean checkout plus no diff must emit a blocker
  packet and exit non-zero
- if sibling git worktrees exist for the same repo, list likely active branches
  and paths in the blocker packet
- PR mode must always map a clean cached target checkout
- local/intent mode must clearly mark dirty state
- `.soulforge` files must never appear in targets
- the bootstrap must write a packet file and print a short context summary for
  Codex to attach before reasoning

Commit structure:

1. add checkout classifier and blocker packet schema
2. add sibling worktree discovery and strict plugin defaults
3. add plugin bootstrap summary output and tests
4. update install/docs with strict production startup

Verification gates:

- unit tests for every checkout state
- smoke test on `/home/prop_/projects/property-partner-ops` must fail closed as
  detached/no-surface, not emit an empty useful packet
- smoke test on `/home/prop_/worktrees/pp-ops-property-tree-postmerge-proof-fix`
  must select PR mode and clean target worktree
- smoke test on WorkspaceMCP PR branch must still select clean PR mode
- `python3 -m unittest discover -s tests -v`
- `python3 -m py_compile repo_context_forge.py scripts/*.py`

No-change surfaces:

- existing `analyze`, `benchmark`, and `wrap` command compatibility
- generated WorkspaceMCP artifacts remain ignored
- plugin marketplace registration remains stable

## PR 2: SoulForge-Native Ranking Engine

Branch: `codex/native-map-ranking`

Owns:

- SoulForge SQLite adapter hardening
- graph signal extraction
- co-change signal extraction
- full-text/task-term matching
- changed-hunk and changed-symbol prioritization
- production/test/file-type ranking policy
- rendered file reasons
- exported symbol and dependent-count display

Required behavior:

- use SoulForge PageRank as a baseline signal only
- rank changed production files before changed tests unless the PR is test-only
- rank symbols that overlap changed hunks before broad container classes
- rank directly referenced/imported neighbors after changed files
- rank co-change partners after direct graph neighbors
- rank test files as verification context unless tests are the only changed files
- include a `why_selected` list per target file
- include `rank_signals` per target file:
  - `changed_file`
  - `changed_symbol`
  - `production_file`
  - `test_file`
  - `pagerank`
  - `graph_neighbor`
  - `cochange_partner`
  - `full_text_match`
  - `intent_match`
  - `gitnexus_confirmed` when present
- include dependent count tags when SoulForge or GitNexus can supply them
- include semantic summaries only when SoulForge has cached summaries

Ranking policy:

```text
changed symbol overlapping diff hunk      highest
changed production file                   very high
direct graph neighbor of changed file     high
co-change partner                         medium-high
full-text/intent match                    medium
PageRank/global importance                medium-low
changed test file                         verification priority
broad test class/helper                   fallback unless hunk-overlapping
generated/cache/vendor file               excluded unless explicitly changed
```

Commit structure:

1. define ranking DTOs and signal extraction
2. extract SoulForge graph/co-change/full-text/summary data
3. implement deterministic ranking policy
4. update prompt renderer with reasons, dependent counts, and summaries
5. add WorkspaceMCP and Property Partner fixture tests

Verification gates:

- Property Partner PR ranks
  `workers/browser-automation/browser_automation_worker/cli.py` above broad
  `BrowserAutomationWorkerTests` when production hunks changed
- Property Partner tests remain present as verification context
- WorkspaceMCP Gmail branch ranks `gmail/gmail_tools.py` before Gmail tests
- empty/no-map fallback remains explicit
- no generated/cache files appear as targets
- ranking tests are deterministic

No-change surfaces:

- target checkout isolation
- existing packet schema compatibility for downstream consumers
- GitNexus plan generation still works without executed GitNexus results

## PR 3: Per-Turn Prompt Refresh And Personalization

Branch: `codex/native-turn-refresh`

Owns:

- task-scoped context state
- read/search/edit/mention event recording
- refresh command
- token-budget adaptation
- incremental reindex triggers
- prompt packet versioning

Required behavior:

- create a task context file under
  `~/.cache/repo-context-forge/tasks/<repo>/<head>/<task-id>.json`
- record:
  - initial user intent
  - files read by Codex
  - files searched or grep-hit by Codex
  - files edited by Codex
  - paths/symbols mentioned by user or model
  - prior selected targets
  - GitNexus confirmed/missing surfaces
- expose commands:
  - `context-start`
  - `context-refresh`
  - `context-record-read`
  - `context-record-search`
  - `context-record-edit`
  - `context-record-mention`
- refresh ranking after material events
- reindex changed files after edits through SoulForge when supported
- token budget starts larger and decays as conversation tokens grow
- prompt renderer marks:
  - `[CHANGED]`
  - `[READ]`
  - `[SEARCH_HIT]`
  - `[GITNEXUS_CONFIRMED]`
  - `[NEW_SINCE_LAST_PACKET]`

Commit structure:

1. add task context state model and storage
2. add event record commands
3. add context refresh ranking boosts
4. add token-budget refresh renderer
5. add tests for read/search/edit boosts

Verification gates:

- reading `cli.py` in Property Partner boosts it and graph/co-change neighbors
- editing `cli.py` marks it changed and refreshes symbols
- search hits alter ranking without replacing changed-hunk priority
- repeated refresh is deterministic for the same state
- corrupted task context fails closed with a readable error

No-change surfaces:

- one-shot `analyze` remains available
- benchmark harness can still compare static and native-refresh modes
- cache cleanup never removes user worktrees or source files

## PR 4: GitNexus Fusion And Blast-Radius Verification

Branch: `codex/native-gitnexus-fusion`

Owns:

- executable GitNexus checklist schema
- GitNexus result ingestion
- mismatch detection
- verified blast-radius rendering
- stale-index policy

Required behavior:

- for top changed symbols/files, run or request:
  - `gitnexus_context`
  - `gitnexus_impact(direction="upstream")`
  - `gitnexus_detect_changes` before commit/publish flows
- compare GitNexus affected modules/processes against SoulForge targets
- label every blast-radius value with its source:
  - `soulforge_graph`
  - `gitnexus_impact`
  - `merged`
- warn when:
  - SoulForge selects a file but GitNexus sees no supporting relation
  - GitNexus finds impacted processes absent from the prompt packet
  - GitNexus index SHA does not match packet target SHA
  - GitNexus is unavailable
- never block code reading because GitNexus is unavailable, but block
  production blast-radius claims

Commit structure:

1. add GitNexus checklist/result schema
2. add result merge and mismatch model
3. add prompt rendering for verified blast radius
4. add stale-index and unavailable-GitNexus tests

Verification gates:

- stale GitNexus index produces a blocking warning
- GitNexus-only impacted surface appears in refreshed packet
- SoulForge-only low-confidence target is demoted or labelled
- Codex final-review packet distinguishes map-selected from verified impact

No-change surfaces:

- SoulForge remains the map/ranking source
- GitNexus remains the semantic impact source
- plugin still works without MCP tools by emitting actionable required checks

## PR 5: Benchmark, Release Hardening, And Production Acceptance

Branch: `codex/native-benchmark-hardening`

Owns:

- benchmark fixtures
- acceptance reports
- release runbook
- plugin marketplace validation
- regression gates
- docs cleanup

Required benchmark fixtures:

- WorkspaceMCP Gmail draft lifecycle branch
- Property Partner `pp-ops-property-tree-postmerge-proof-fix`
- detached clean wrong-checkout case
- dirty local pre-PR implementation case
- no-change intent-only case

Benchmark modes:

- no map
- current static Repo Context Forge packet
- native SoulForge-style ranking
- native ranking plus turn refresh
- GitNexus-only where available
- native ranking plus GitNexus fusion

Metrics:

- wrong-checkout detection rate
- dirty-contamination rate
- target file precision
- changed production symbol precision
- broad-test-helper over-prioritization rate
- first useful file named
- first irrelevant file named
- GitNexus mismatch rate
- final target-surface accuracy
- token count used by injected packet
- elapsed map/index time

Commit structure:

1. add benchmark fixture runner
2. add acceptance reports for WorkspaceMCP and Property Partner
3. add release runbook and plugin validation docs
4. remove obsolete prototype docs or mark them historical

Verification gates:

- wrong-checkout case fails closed
- PR mode has zero dirty contamination
- native ranking beats static packet on Property Partner target ordering
- native ranking beats static packet on WorkspaceMCP target ordering
- packet stays under configured token budget
- plugin install and fresh-session discovery are documented and smoke-tested
- shippable-code scan finds no unfinished work markers or temporary paths

## Prompt Injection Architecture

Production requires one of these supported injection paths, in this order:

1. Codex plugin pre-task context hook, if available in the host runtime.
2. Codex skill startup path that runs the bootstrap before substantive repo
   analysis.
3. Wrapper command only as a test harness, not the production UX.

Release is blocked unless a fresh Codex session proves the context packet is
available before the agent selects files, edits code, or makes review findings.

The prompt packet must contain:

- checkout state
- target worktree path
- source dirty state
- target dirty state
- base/head SHA
- ranked file list
- ranking reasons
- changed symbols
- exported symbols
- dependent/blast-radius tags
- co-change partners
- semantic summaries when available
- out-of-scope dirty files
- required GitNexus checks
- GitNexus verified results when available
- token-budget metadata

## Minimal Code Shape

The current single-file runtime must be split only where it removes real
complexity. Target final modules:

```text
repo_context_forge/
  cli.py
  git_target.py
  soulforge_adapter.py
  ranking.py
  prompt.py
  task_state.py
  gitnexus_bridge.py
  benchmark.py
```

Rules:

- no new dependency unless the standard library cannot do the job
- no duplicate SoulForge parser paths
- no fake semantic summaries
- no generic plugin framework abstraction
- no second package manager
- no database writes outside cache or selected analysis checkout
- keep each module below the point where tests become harder to read; extract
  only when behavior has a real boundary

## Active Branch Order

1. `codex/native-startup-checkout`
2. `codex/native-map-ranking`
3. `codex/native-turn-refresh`
4. `codex/native-gitnexus-fusion`
5. `codex/native-benchmark-hardening`

Work may proceed with at most three active dependent branches. Do not start PR 4
until PR 2 has ranking fixtures passing. Do not start PR 5 until PR 3 and PR 4
produce merged packets.

## Consolidation And Stop Conditions

Stop and consolidate if:

- Codex plugin runtime cannot support any pre-reasoning context path
- SoulForge DB schema cannot provide enough graph/co-change data and native
  behavior would require reimplementing the entire indexer
- Property Partner ranking still prioritizes broad test containers after PR 2
- WorkspaceMCP or Property Partner benchmarks show no improvement over static
  packet mode
- stack depth would exceed three active branches

If a stop condition triggers, freeze the stack and produce one consolidation PR
that either narrows the product contract or replaces the map adapter with a
better-supported integration.

## Execution Checklist

- [x] PR 1: checkout classifier and strict blocker packets implemented.
- [x] PR 1: wrong detached Property Partner checkout fails closed.
- [x] PR 1: active Property Partner worktree maps clean PR head.
- [x] PR 2: SoulForge graph/co-change/full-text signals extracted.
- [x] PR 2: changed production symbols outrank broad tests.
- [x] PR 2: ranking reasons and dependent tags render in packets.
- [x] PR 3: task context state records read/search/edit/mention events.
- [x] PR 3: refresh re-ranks after material events.
- [x] PR 3: token budget adapts to conversation length.
- [x] PR 4: GitNexus results merge into packet.
- [x] PR 4: stale/missing GitNexus blocks blast-radius claims.
- [x] PR 5: WorkspaceMCP benchmark passes acceptance thresholds.
- [x] PR 5: Property Partner benchmark passes acceptance thresholds.
- [ ] PR 5: fresh Codex session proves pre-reasoning context availability.
- [ ] PR 5: production docs and release runbook complete.

## Implementation Evidence

First production implementation pass on branch
`codex/native-startup-checkout`:

- Detached `/home/prop_/projects/property-partner-ops` now exits non-zero and
  emits a blocker packet with sibling worktree suggestions instead of an empty
  local packet.
- Active `/home/prop_/worktrees/pp-ops-property-tree-postmerge-proof-fix`
  maps PR mode against `origin/main` and ranks production files before broad
  test containers:
  - `workers/browser-automation/browser_automation_worker/cli.py`
  - `workers/browser-automation/browser_automation_worker/config.py`
  - `tests/python/test_browser_automation_worker.py`
  - `tests/integration/browser-automation-worker.test.mjs`
- WorkspaceMCP Gmail fixture still ranks `gmail/gmail_tools.py` above Gmail
  draft tests and excludes source dirty files from PR ranking boosts.
- Prompt packets now include `rank_signals`, `why_selected`,
  `priority_score`, `dependent_count`, semantic summaries when present, graph
  neighbor reasons, and co-change reasons.
- Task context commands now record read/search/edit/mention events and refresh
  packets with per-turn boosts.
- `gitnexus-merge` now tags confirmed files, records absent impacted files, and
  blocks blast-radius claims for stale or unavailable GitNexus results.

Commands verified:

- `python3 -m unittest discover -s tests -v`: 25 tests passed.
- `python3 -m py_compile repo_context_forge.py scripts/codex_context_bootstrap.py scripts/install_local_plugin.py`
- `python3 scripts/codex_context_bootstrap.py --repo /home/prop_/projects/property-partner-ops --out /tmp/rcf-detached.xml`: exited `1` with blocker packet.
- `python3 scripts/codex_context_bootstrap.py --repo /home/prop_/worktrees/pp-ops-property-tree-postmerge-proof-fix --map-build auto --out /tmp/rcf-pp-pr-map.xml`: exited `0` with production files ranked before broad tests.
- `python3 scripts/codex_context_bootstrap.py --repo /home/prop_/projects/fork_google_workspace_mcp --base upstream/main --head HEAD --map-build auto --gitnexus-repo fork_google_workspace_mcp --out /tmp/rcf-wmcp-map.xml`: exited `0` with Gmail production target ranked first.
- `python3 repo_context_forge.py context-start ...`, `context-record-edit ...`,
  and `context-refresh ...`: edited file received `edited_file` signal.
- `python3 repo_context_forge.py gitnexus-merge ...`: stale index warning
  blocked blast-radius claims and `gitnexus_confirmed` was rendered.

## Acceptance Thresholds

WorkspaceMCP Gmail fixture:

- top target file is `gmail/gmail_tools.py`
- Gmail draft tests are verification context, not primary production target
- no unrelated staged/dirty files appear in PR mode
- packet includes GitNexus checks for changed Gmail draft symbols

Property Partner fixture:

- wrong detached checkout emits a blocker, not empty local mode
- active PR worktree maps against `origin/main`
- `workers/browser-automation/browser_automation_worker/cli.py` is ranked above
  broad `BrowserAutomationWorkerTests` when production hunks changed
- tests remain visible as verification context
- packet includes GitNexus checks for changed Property Tree worker symbols

General production:

- packet generation is deterministic for the same repo state and task state
- strict mode fails closed on ambiguous target state
- generated caches are excluded from target files
- SoulForge indexing and cleanup run in cache-owned analysis checkouts, never in
  the user's source checkout
- all touched behavior has unit tests and fixture smoke tests
- no hidden manual command requirement remains in normal user workflow
