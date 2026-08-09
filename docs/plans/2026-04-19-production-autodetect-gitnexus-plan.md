# Production Autodetect And GitNexus Freshness Plan

Date: 2026-04-19
Project: Repo Context Forge
Status: implemented locally

## Objective

Make Repo Context Forge production-seamless for any git project folder Codex is
running in.

The plugin must automatically build a useful context packet for the current
project folder before substantive code reasoning, prove that SoulForge mapped
the selected target head, and ensure GitNexus is fresh for that same target
before any blast-radius claim is trusted.

This is not a Property Partner Ops special case. Property Partner Ops is only an
acceptance fixture.

## Scope

In scope:

- current-project-folder target detection
- whole-repo fallback mode for clean folders with no PR or dirty surface
- SoulForge target-head proof in packet metadata
- GitNexus exact-target freshness check
- automatic GitNexus reindex when the index is missing or stale
- fail-closed GitNexus status when reindex fails or required symbols are absent
- plugin skill/runtime contract updates
- tests and smoke fixtures proving source checkout safety

Out of scope:

- sibling worktree auto-routing
- asking users to run long manual test prompts
- replacing GitNexus
- reimplementing SoulForge indexing
- adding a daemon or background service
- indexing non-git directories

## Trusted Base And Target Branch

Trusted base: current `main` plus the existing production baseline commits up to
`87cc85c Isolate Repo Context Forge analysis checkouts`.

Target branch: `codex/native-startup-checkout`.

This plan supersedes the parts of
`docs/plans/2026-04-19-native-soulforge-codex-plan.md` that treated detached
clean checkouts as the only valid production outcome. A clean current folder
must still get useful repo context unless it is not a git repository or the map
engines cannot produce a safe packet.

## Delivery Map

| Slice | Type | Ownership | Mergeable Outcome |
| --- | --- | --- | --- |
| PR 1 | foundation/runtime | current-folder mode contract | plugin always targets the project folder Codex is in |
| PR 1 | runtime | SoulForge target proof | packet states and verifies source head, analysis head, and map DB target |
| PR 1 | runtime/verification | GitNexus freshness gate | stale or missing GitNexus indexes are refreshed or marked blocked |
| PR 1 | operator UX | packet and skill contract | no prompt rituals; normal coding requests trigger automatic context |
| PR 1 | proof/tests/docs | fixtures and docs | production behavior is verifiable across generic repos and Property Partner Ops |

Single PR only. These behaviors are coupled at the startup contract and should
land together.

Maximum active stack depth: 1.

Consolidation trigger: if the implementation starts adding a second
orchestrator, daemon, or broad config layer, stop and reduce to the single
bootstrap/runtime path in this plan.

Deploy freeze rule: do not present the plugin as production-ready until the
GitNexus freshness gate and SoulForge target proof pass on a generic clean repo,
a dirty local repo, and the Property Partner Ops PR fixture.

## Runtime Contract

The current working git project folder is authoritative.

Repo Context Forge must not silently switch to another worktree, even if sibling
worktrees exist. Sibling worktrees may be listed as diagnostic hints only when
the current folder cannot be analyzed, but they must never be selected
automatically.

Mode selection:

1. `pr`: base ref exists and `base...HEAD` has changed files in the current
   project folder.
2. `local`: current folder has real dirty tracked or untracked files, excluding
   Repo Context Forge and SoulForge cache noise.
3. `intent`: no changed files, but the caller provides task intent.
4. `repo`: clean current folder with no changed files and no intent. Use the
   whole-repo SoulForge map to provide ambient project context.

Block only when:

- the folder is not a git worktree
- the target head cannot be resolved
- SoulForge map generation is required and cannot produce a map
- GitNexus is required, stale, and cannot be reindexed
- generated packet metadata fails target-head consistency checks

## SoulForge Target-Head Contract

Every packet must include explicit map target metadata:

- `source_repo`
- `source_head_sha`
- `analysis_repo`
- `analysis_head_sha`
- `analysis_repo_is_cache_owned`
- `analysis_head_matches_source_head`
- `soulforge_db_path`
- `soulforge_db_exists`
- `soulforge_db_mtime`
- `soulforge_build_attempted`
- `soulforge_build_returncode`
- `soulforge_status`: `fresh`, `missing`, `failed`, or `unknown`

Required invariants:

- PR mode maps a clean cache-owned checkout at the current folder's `HEAD`.
- Local and intent modes map a cache-owned checkout with dirty local files
  overlaid.
- Repo mode maps a cache-owned checkout at the clean current folder `HEAD`.
- `analysis_head_sha` must equal `source_head_sha`.
- Source `.gitignore`, `.soulforge`, and `.codex` must not be modified by map
  generation.
- `.soulforge`, `.codex`, `__pycache__`, and cache paths must never appear as
  targets.

## GitNexus Freshness Contract

GitNexus is the only trusted source for semantic impact and blast radius.

Every run must perform a freshness preflight before treating GitNexus results as
authoritative:

1. Resolve the target identity from the packet:
   - `analysis_repo`
   - `source_head_sha`
   - `gitnexus_repo`
2. Check whether a GitNexus index exists for that target and whether its
   recorded commit equals `source_head_sha`.
3. If missing or stale, automatically run the GitNexus CLI index command against
   `analysis_repo`.
4. Re-check the index commit.
5. Run required GitNexus context/impact checks only after freshness is confirmed.
6. If reindex fails or the refreshed index still cannot resolve required
   symbols, set `gitnexus.status = blocked` and include explicit missing
   symbols/files.

Exact-head freshness is mandatory. Blindly using an older index is a production
failure.

Do not reindex wastefully when the existing index is already for the exact
target head. The production guarantee is "fresh every run", implemented as
"check every run and reindex on mismatch".

## Packet Contract

Packets must make trust boundaries explicit:

- `soulforge.status`
- `soulforge.target_head_verified`
- `gitnexus.status`: `fresh`, `reindexed`, `blocked`, or `unavailable`
- `gitnexus.indexed_head_sha`
- `gitnexus.expected_head_sha`
- `gitnexus.reindex_attempted`
- `gitnexus.required_checks_resolved`
- `gitnexus.missing_required_symbols`
- warning when GitNexus is unavailable or blocked

The rendered prompt must instruct the agent:

- use SoulForge targets for first-pass orientation
- run or respect GitNexus checks before editing production code
- do not claim blast radius when GitNexus is blocked, stale, or incomplete

## Commit Structure

One PR with four commits:

1. `Add current-folder repo fallback mode`
   - add `repo` mode
   - update mode selection
   - keep detached clean current folders analyzable without sibling rerouting

2. `Add SoulForge target-head metadata`
   - extend target/map metadata
   - verify source and analysis heads match
   - render map status in Markdown, JSON, and prompt XML

3. `Gate GitNexus on exact target freshness`
   - add GitNexus index status adapter
   - add automatic stale/missing reindex path
   - block blast-radius claims when reindex or symbol resolution fails

4. `Prove production startup contract`
   - tests for `pr`, `local`, `intent`, and `repo` modes
   - tests for cache-noise exclusion
   - tests for stale GitNexus reindex and missing-symbol block
   - docs and plugin skill updates

## PR Ownership Map

PR: `codex/native-startup-checkout`

Owns:

- `repo_context_forge.py`
- `scripts/codex_context_bootstrap.py`
- `skills/repo-context-forge/SKILL.md`
- `README.md`
- `tests/test_repo_context_forge.py`
- this plan and updates to the older native plan where superseded

Does not own:

- changes to Property Partner Ops
- changes to GitNexus internals
- global Codex host behavior outside this plugin
- new external services

## Active Branch Order

1. Finish this plan on `codex/native-startup-checkout`.
2. Implement `repo` mode and target metadata.
3. Implement GitNexus freshness adapter and reindex gate.
4. Update tests and docs.
5. Reinstall/register the plugin locally.
6. Run acceptance fixtures.
7. Commit and push only after gates pass.

## Verification Gates

Required local gates:

```bash
python3 -m unittest tests/test_repo_context_forge.py
python3 -m py_compile repo_context_forge.py scripts/codex_context_bootstrap.py scripts/install_local_plugin.py
git diff --check
```

Required fixture gates:

```bash
python3 scripts/codex_context_bootstrap.py --repo /home/prop_/projects/repo-context-forge --map-build auto
python3 scripts/codex_context_bootstrap.py --repo /home/prop_/projects/property-partner-ops --map-build auto
python3 scripts/codex_context_bootstrap.py --repo /home/prop_/worktrees/pp-ops-property-tree-postmerge-proof-fix --map-build auto
```

Expected Property Partner Ops root behavior:

- real packet, not sibling-worktree auto-selection
- mode `repo` when clean except `.soulforge`/`.codex` noise
- `analysis_repo` under `~/.cache/repo-context-forge`
- `.soulforge` and `.codex` excluded from targets
- `.gitignore` unchanged

Expected Property Partner Ops PR worktree behavior:

- mode `pr`
- top targets include changed production files before broad tests
- SoulForge target metadata proves analysis head equals source head
- GitNexus index is fresh or reindexed to the same head
- required GitNexus symbols resolve or packet blocks blast-radius confidence

## Stop Conditions

Stop implementation and report the blocker if:

- GitNexus CLI has no stable command for indexing a specified repo path
- GitNexus MCP cannot reveal indexed commit/head metadata
- SoulForge DB cannot be associated with the target checkout well enough to
  prove target-head freshness
- implementing freshness requires broad GitNexus internal changes

## Acceptance Criteria

The feature is complete only when:

- users can make normal coding requests without writing context-test prompts
- plugin startup targets the current project folder
- clean folders receive useful repo context
- dirty folders receive local dirty context
- PR folders receive PR-head context
- SoulForge target-head proof is rendered
- GitNexus is automatically checked every run
- stale GitNexus is reindexed or explicitly blocked
- no source checkout files are modified by context generation
- Property Partner Ops fixture no longer reports "PASS" while GitNexus is stale

## Implementation Evidence

Implemented on `codex/native-startup-checkout`.

Local gates:

- `python3 -m unittest tests/test_repo_context_forge.py`
- `python3 -m py_compile repo_context_forge.py scripts/codex_context_bootstrap.py scripts/install_local_plugin.py`
- `git diff --check`

Fixture gates:

- Property Partner Ops detached root now emits a real `repo` packet, not a
  detached-checkout blocker.
- Property Partner Ops detached root maps a cache-owned analysis checkout at
  `3d4ca7d7328ca4405a1201bd7410745fe85dd538`.
- Property Partner Ops detached root reports SoulForge `fresh`, target-head
  verified, GitNexus `fresh`, and required checks resolved.
- Property Partner Ops PR worktree emits `pr` mode for head
  `458698387be8f432ad69309375098560fc490d1e`.
- Property Partner Ops PR worktree reports SoulForge `fresh`, GitNexus
  `reindexed`, exact indexed head match, and required checks resolved.
- `.gitignore` diffs remained empty in both Property Partner Ops source
  checkouts.
