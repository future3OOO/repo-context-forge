# Standalone Map Builder Plan

## Status

- current state: planning-only; no implementation in this pass
- governing artifact: `docs/plans/standalone-map-builder-2026-04-25.md`
- last updated: 2026-04-25
- repository: `/home/prop_/projects/repo-context-forge`

## Objective

- primary goal: make Repo Context Forge standalone by vendoring/native-implementing the repo map builder so production paths no longer require `shutil.which("soulforge")`, Bun, or `/home/prop_/soulforge`.
- success condition: `--map-build auto` can produce a fresh map from a clean cache-owned analysis checkout with no `soulforge` binary on `PATH`, while existing packet rendering, target ranking, GitNexus gating, and plugin bootstrap behavior stay compatible.

## Source Of Truth

- authority:
  - `README.md`
  - `skills/repo-context-forge/SKILL.md`
  - `repo_context_forge.py`
  - `scripts/codex_context_bootstrap.py`
  - existing tests in `tests/test_repo_context_forge.py`
  - prior plan `docs/plans/2026-04-19-native-soulforge-codex-plan.md`
- trusted base: `main`, or the current clean successor of `main` when implementation starts
- current planning checkout: branch `codex/native-startup-checkout`, head `257f78a4a2125e8940fbd38e8520670f64517593`
- linked evidence:
  - current `build_soulforge_map()` shells out to `soulforge --headless --quiet --no-render`
  - current `SoulForgeMap` already reads `.soulforge/repomap.db` via SQLite
  - current smoke proof on OpenCock showed external SoulForge works only after copying `/home/prop_/soulforge`, `/home/prop_/.soulforge`, `/home/prop_/.bun`, and command symlinks

## Affected Surface

- changed boundary or behavior:
  - map build path: `find_soulforge_binary()`, `build_soulforge_map()`, `MapBuildResult`, `make_packet()`
  - map read contract: `SoulForgeMap` SQLite schema expectations
  - plugin entrypoint: `scripts/codex_context_bootstrap.py`
  - CLI options and docs around `--map-build`, `--soulforge-bin`, and missing-map behavior
- adjacent consumers/callers:
  - `target_files_for_mode()`
  - `make_target_entries()`
  - `soulforge_target_metadata()`
  - packet renderers for markdown, JSON, and prompt XML
  - GitNexus freshness and required-check planning
  - plugin skill startup flow
- no-change surfaces:
  - PR/local/intent/repo mode selection
  - cache-owned analysis checkout isolation
  - source checkout cleanliness proof
  - existing SQLite reader behavior for old SoulForge-generated maps
  - `--soulforge-bin` compatibility as an explicit fallback
  - GitNexus status semantics and blocked blast-radius claims
  - plugin marketplace/install layout

## Contract And Proof Model

- authoritativeContract:
  - RepoForge owns the production map build contract.
  - Production map generation must not depend on a process found by `shutil.which("soulforge")`.
  - Map generation must write only inside the cache-owned analysis checkout selected by RepoForge.
  - Generated map DBs must remain readable by the existing `SoulForgeMap` adapter until that adapter is deliberately renamed or generalized.
  - External SoulForge may remain as an explicit compatibility fallback, but it must not be required for default `--map-build auto`.
- invariants:
  - `--map-build never` never builds or mutates a map.
  - `--map-build auto` reuses a fresh existing map when valid and builds with the native builder when missing/stale.
  - `--map-build always` rebuilds in the analysis checkout.
  - no `.soulforge` or `.gitignore` mutation remains in the source checkout.
  - packet `target_head_verified` is true only when map metadata matches the analysis checkout head.
  - generated/cache/vendor paths are excluded from target ranking unless explicitly changed.
  - old external-SoulForge map fixtures still load.
- proofPlan:
  - unit tests for builder selection, map freshness, metadata, and no-source-mutation behavior
  - SQLite schema fixture tests for native-generated maps and legacy SoulForge maps
  - smoke bootstrap with `PATH` excluding `soulforge` and `bun`
  - full packet smoke in `repo`, `intent`, `local`, and `pr` modes
  - GitNexus-off and GitNexus-auto smoke to prove map changes do not alter GitNexus gating semantics

## Scope In

- Add an internal map builder interface and native default implementation.
- Build a RepoForge-owned `.soulforge/repomap.db` compatible with the current reader contract.
- Implement gitignore-aware file walking, file metadata, symbol extraction, import/reference edges, simple call/reference data where practical, co-change signals, PageRank or deterministic graph centrality, and map metadata.
- Keep external SoulForge behind explicit fallback or compatibility mode.
- Update tests, README, skill docs, and bootstrap behavior.
- Preserve existing cache-owned checkout safety and plugin startup flow.

## Scope Out

- Rewriting RepoForge into a separate service.
- Replacing GitNexus or weakening GitNexus freshness requirements.
- Live LLM semantic summary generation during bootstrap.
- Full SoulForge UI, chat, editor, memory, provider, or Bun runtime parity.
- Vendoring `/home/prop_/soulforge/node_modules` or requiring Bun in production RepoForge.
- Changing Codex plugin marketplace mechanics except where needed to expose the standalone plugin behavior.
- Transaction, lease, replay, finalize, or persistent mutation-system changes; this plan is not transaction-sensitive.

## Authority And Conflict Rule

- authorities:
  - this plan governs implementation order for standalone map-builder work
  - `README.md` and `skills/repo-context-forge/SKILL.md` govern user-facing contract wording
  - existing tests and prior plans govern compatibility expectations
- conflict rule:
  - preserve packet and plugin behavior over internal naming preferences
  - prefer native standalone behavior over external SoulForge fallback in default paths
  - if a slice exceeds the changed-line budget, split it by builder interface, schema writer, language analyzer, or docs/proof boundary before coding

## Delivery Map

- plan type: implementation roadmap for later execution
- PR count: 4
- stack depth: 4 dependent PRs maximum
- regroup rule: if PR 2 exceeds 1,000 changed code lines, split language analyzers from graph/schema writer before review; if any PR would exceed 1,300 changed code lines, stop and split before implementation
- consolidation rule: if the same map schema or builder interface changes in 3 or more active branches, freeze new stack work and consolidate into the lowest active builder PR
- deploy freeze rule: do not publish the plugin as standalone until PR 4 proves bootstrap works without `soulforge`, Bun, or `/home/prop_/soulforge`

## PR Plan

| PR | Branch | Base | Owner Slice | Commit Structure | Changed-Code-Line Budget | Verification | Entry | Exit |
|---|---|---|---|---|---:|---|---|---|
| A | `codex/standalone-builder-interface` | `main` | Map builder abstraction, map metadata contract, compatibility fallback policy | 1. introduce `MapBuilder`/`MapBuildResult` boundary 2. add native/external selector 3. add metadata freshness checks 4. update CLI help/tests | 450 | py_compile, unit tests for selection/freshness, legacy external fallback test | existing shell-out builder | default builder path can be selected without calling `shutil.which("soulforge")` |
| B | `codex/native-map-db-writer` | PR A | Native SQLite writer and repository scanner | 1. schema writer 2. gitignore-aware file walker 3. symbol extractor baseline 4. import/reference edges 5. deterministic centrality/PageRank 6. co-change extraction | 950 | unit schema tests, generated/cache exclusion tests, smoke `--map-build auto` with no `soulforge` on PATH | builder interface exists | native builder writes readable `.soulforge/repomap.db` with fresh target metadata |
| C | `codex/native-builder-packet-parity` | PR B | Packet parity, ranking preservation, mode behavior | 1. adapt `SoulForgeMap` naming/metadata reads 2. prove target ranking on native maps 3. add PR/local/intent/repo smoke fixtures 4. keep external map read compatibility | 800 | packet snapshot/semantic assertions, repo/local/intent/pr smokes, no-source-mutation proof | native DB exists | packet quality and warnings match current production contract without external SoulForge |
| D | `codex/standalone-release-hardening` | PR C | Docs, plugin contract, release hardening, fallback deprecation | 1. README/skill updates 2. migration notes 3. install/update docs 4. benchmark fixtures 5. final fallback policy | 500 | full gate, standalone bootstrap proof with `PATH=/usr/bin:/bin`, config/plugin smoke | parity proven | documented standalone release candidate |

## Verification Plan

- targeted tests:
  - `python3 -m unittest discover -s tests -v`
  - `python3 -m py_compile repo_context_forge.py scripts/*.py`
  - builder selection tests with `PATH` excluding `soulforge`
  - native map schema test for `files`, `symbols`, `edges`, `refs`, `cochanges`, `calls`, `semantic_summaries` where available, and metadata
  - no-source-mutation tests for PR, local, intent, and repo modes
- combined workflow proof:
  - `PATH=/usr/bin:/bin python3 scripts/codex_context_bootstrap.py --repo /home/prop_/projects/repo-context-forge --mode repo --map-build auto --gitnexus-mode off --out /tmp/rcf-standalone.xml`
  - assert map status `fresh`, `target_head_verified=true`, and no external SoulForge command appears in build metadata
- focused invariant checks:
  - `--map-build never` does not create `.soulforge`
  - `--map-build auto` reuses fresh native map
  - `--map-build always` rebuilds map and updates metadata
  - dirty source checkout remains unchanged before/after bootstrap
  - legacy SoulForge-generated map fixture remains readable
- full gate:
  - `python3 -m unittest discover -s tests -v`
  - `python3 -m py_compile repo_context_forge.py scripts/*.py`
  - run README example commands in a temporary git fixture
- post-merge checks:
  - reinstall local plugin with `python3 scripts/install_local_plugin.py`
  - start a fresh Codex session and verify `$repo-context-forge` injects a packet without `soulforge` on PATH

## Affected Surface Detail

- builder selector:
  - default must be native
  - explicit `--soulforge-bin` must remain supported for compatibility until a later removal decision
  - warnings must distinguish native builder failures from external fallback failures
- map DB schema:
  - keep columns already read by `SoulForgeMap`
  - add metadata table rather than inferring freshness only from file mtime
  - include builder name/version, source head, analysis head, build mode, and timestamp
- language/symbol extraction:
  - first production slice should support Python and JavaScript/TypeScript enough for existing fixture repositories
  - unsupported languages still appear as files with centrality, co-change, and text/path signals
  - symbol gaps must degrade explicitly, not fake high-confidence summaries
- graph construction:
  - import/reference edges are enough for first standalone release
  - call graph can be best-effort and tested only where deterministic
  - co-change should come from git history and exclude generated/cache paths
- packet behavior:
  - target quality must not regress below current external SoulForge smoke behavior
  - GitNexus remains the only source for verified blast-radius claims

## Self-Critique Before Finalizing

- authority model: the plan uses existing RepoForge docs and code as authority, and records the prior SoulForge plan as evidence rather than replacing it.
- scope clarity: the plan explicitly excludes Bun, SoulForge UI/runtime parity, GitNexus replacement, and live LLM summaries.
- PR count/order: 4 PRs is within the delivery-governance limit and keeps interface, builder, packet parity, and release proof separate.
- owner boundaries: PR B is the largest risk. It must split if it crosses 1,000 changed code lines or if language analyzers start expanding beyond the first deterministic set.
- verification completeness: includes unit, schema, no-source-mutation, no-external-binary, plugin bootstrap, and legacy map compatibility proof.
- execution-agent prompt: must not tell the implementation agent to re-plan; the agent should follow this artifact and update the checklist.

## Execution Checklist

- [x] planning artifact created
- [x] authority verified
- [x] affected surface mapped
- [x] delivery map and PR ownership defined
- [x] self-critique recorded
- [ ] PR A implemented and checklist updated
- [ ] PR B implemented and checklist updated
- [ ] PR C implemented and checklist updated
- [ ] PR D implemented and checklist updated
- [ ] standalone bootstrap verified without `soulforge` or Bun on PATH
- [ ] final release classification complete

## Linked Review Artifacts

- none yet

## Change Log

- 2026-04-25: created standalone map-builder implementation plan.
