# Issue 10 Intent Graph Freshness Plan

## Status

- current state: governing plan complete; implementation not started
- governing artifact: this file
- last updated: 2026-08-23
- workflow slug: `issue-10-intent-graph-freshness`

## Objective

- primary goal: make the public Repo Context Forge producer preserve intent relevance through bounded graph planning, bind GitNexus evidence to the exact candidate tree, and emit one bounded `advisorProjection` schema version 1.
- success condition: every Issue #10 acceptance item passes through the public producer and configured real GitNexus Seam; legacy packet and quality-gate consumers retain their full evidence; the branch is reviewed, pushed, installed, and has a closed reviewer gate without merge.

## Source Of Truth

- sole product-contract authority: [Repo Context Forge issue #10](https://github.com/future3OOO/repo-context-forge/issues/10).
- occurrence evidence: `future3OOO/claude-skills#152` comments `5383217053` and `5383251146`.
- replay input: `future3OOO/claude-skills#143` and its preserved checkout at `/home/prop_/projects/wt-issue143`.
- execution authority: repository/global production workflow doctrine; it governs sequencing and proof, not product semantics.
- trusted base: `origin/main` at `53400d37def27b71a9a5c367ec5ae3da8a9029ac`.
- branch and checkout: `fix/issue10-intent-graph-freshness` in `/home/prop_/projects/wt-rcf-issue10`.
- conflict rule: any conflict with Issue #10's schema-version-1 field semantics or ownership boundaries blocks execution for clarification. Downstream plans cannot override producer ownership, synthesize producer evidence, or introduce a second producer Interface.

## Diagnosis

- public producer replay against `/home/prop_/projects/wt-issue143` omitted all five intended Seams, filled the 20-call plan with generic declarations, omitted 118 checks, exited unblocked, and reported `required_checks_resolved=true`.
- root cause 1: `WorkflowIndex.rank_intent()` computes relevance but returns paths only; final generic re-ranking discards score and match evidence.
- root cause 2: exact resolution is partial and occurs after file truncation; exact unqualified references and deterministic ambiguity/absence gaps are absent.
- root cause 3: the complete symbol inventory is reduced to the 12-symbol display slice before `build_gitnexus_plan()`.
- root cause 4: GitNexus freshness accepts registry `lastCommit == HEAD` plus storage existence; the reused ignored `.gitnexus` graph survives while a new dirty overlay is copied into the same analysis checkout.

## Affected Surface Matrix

| Disposition | Paths / Interfaces | Reason |
|---|---|---|
| update | `workflow_index.py` | own immutable intent resolution and complete symbol relevance |
| update | `repo_context_forge.py` | consume one resolution result for targets, coverage, planning, packet projection, and candidate identity |
| update | `gitnexus_analysis.py` | own candidate-aware freshness receipt, locked rebuild, indexed-candidate truth, and omission accounting |
| update | `tests/test_repo_context_forge.py` | direct and public-producer proof |
| update | focused `README.md` production-contract text | document the machine projection and candidate-tree semantics |
| verify only | `scripts/codex_context_bootstrap.py`, `skills/repo-context-forge/scripts/bootstrap.py`, CLI analyze/wrap/benchmark entry points | public producer Seams must inherit behavior without new ownership |
| verify only | prompt/XML/Markdown renderers, quality-gate consumption, `scripts/install_local_plugin.py` | retain current full evidence, rendering, and install behavior |
| no change | `skills/repo-context-forge/SKILL.md`, `.codex-plugin/plugin.json` | no demonstrated contract requires metadata or operator workflow changes |
| no change | downstream `claude-skills` workflow/advisor/persistence code | explicitly downstream-owned |
| no change | GitNexus source/registry schema, cache checkout naming, source checkout ignore/configuration | out of producer scope |

The existing machine packet gains exactly one top-level `advisorProjection`; existing packet fields and human renderers remain unchanged.

## Module Shape

- `WorkflowIndex.resolve_intent(intent)` returns one immutable resolution result: ordered file evidence, exact-file status, matched terms and symbols, required symbol identities, per-file symbol relevance, and deterministic coverage gaps. Target ordering, coverage, planning, and projection consume this result; callers do not re-resolve or re-rank it.
- target preparation keeps bounded display records separate from a complete internal planner inventory. Export/declaration order is only an optional-relevance tie-breaker.
- `gitnexus_analysis.ensure_index()` remains the freshness/receipt owner. Receipt paths, registry checks, stale decisions, rebuild, and publication stay inside that Module.
- `make_packet()` enters one producer transaction lock keyed to the cache-owned analysis checkout before `resolve_target_state()` materializes it and holds that same lock through `gitnexus_analysis.execute()` and receipt publication. `ensure_index()` accepts the held transaction rather than acquiring a narrower nested lock. This measured mechanism covers materialization, candidate-tree equality checks, freshness recheck, rebuild, graph execution, post-build candidate recheck, and atomic receipt publication without moving receipt policy into callers.
- `build_advisor_projection()` consumes existing prepared target and graph records and emits stable same-packet references. It is not a second ranking, planning, persistence, or graph Interface.

## Candidate Identity And Freshness Contract

- capture the analyzed source candidate through a temporary Git index without mutating the source index or worktree.
- candidate identity includes tracked and eligible untracked content, deletions, renames/path changes, executable modes, and symlinks, while applying the same tool-cache exclusions as overlay copying.
- capture sequence:
  1. capture source candidate tree;
  2. materialize the cache-owned analysis checkout;
  3. compute its candidate tree and require equality with the source capture;
  4. block if source mutation during capture or materialization invalidates equality;
  5. run workflow-index and graph analysis against that stable checkout.
- clean candidates equal `HEAD^{tree}`.
- one cache-owned receipt is stored with the existing analysis/index state. It is index freshness metadata, not a second registry or candidate-state owner.
- receipt validation covers analysis-checkout identity, committed-head provenance, exact candidate tree, current GitNexus index generation, and index storage presence.
- missing, malformed, mismatched, or stale receipt: reindex in `auto`; block in check-only mode.
- publish a receipt only after successful real GitNexus analysis, metadata verification, and post-build candidate equality. A failed rebuild never certifies old or new state.
- `indexedCandidateTree` comes only from a validated receipt.

## Projection Contract

- `schemaVersion`: integer `1`; the compatibility key.
- `producerRevision`: non-empty producer provenance.
- `sourceRepo`: canonical repository identity selected from the `origin` fetch URL, otherwise the first remote name in deterministic order. Normalize HTTPS/SSH URLs to `host/owner/repository` with credentials and trailing `.git` removed. If unavailable, emit `{"gap":"source_repo_unavailable"}` and the matching coverage gap.
- `sourceBaseOid`: full merge-base OID of the selected base and analyzed head. Never use a ref name, abbreviated OID, cache HEAD, or silent committed-HEAD fallback. On failure, emit `{"gap":"source_base_unavailable"}` and the matching coverage gap.
- `committedHeadOid`: committed HEAD provenance.
- `expectedCandidateTree`: exact source candidate tree captured for the run.
- `indexedCandidateTree`: exact tree certified by the validated graph receipt, or `{"gap":"indexed_candidate_tree_unavailable"}` on a blocked packet.
- `targets`: deterministic whole ranked records for required and planned optional file/symbol evidence.
- `graph`: status, stable references into the same packet's graph entries, complete required omissions, and optional omission count. Graph bodies are not duplicated.
- `coverageGaps`: deterministic explicit gaps for ambiguous/absent/excluded references, no relevant Seam, unavailable governing-design Seam coverage, identity gaps, and unresolved required graph coverage.
- exactly one projection appears in normal and blocker machine packets. Every graph reference resolves uniquely within the same packet.

## Architecture Families And Decisions

### Intent resolution

- chosen: deepen `WorkflowIndex` with one immutable resolution result consumed throughout packet construction. This keeps global symbol inventory, exact reference resolution, scoring, ambiguity, and evidence ordering behind one Interface.
- rejected: continue parsing and scoring independently in `target_files_for_mode()`, `make_target_entries()`, and `build_gitnexus_plan()`. This is the demonstrated shallow pipeline that discards evidence.
- rejected: add a new generic ranking package. The behavior has one production owner and no second runtime variant; a new package would add Interface surface without leverage.

### Candidate freshness

- chosen: retain the existing per-HEAD cache checkout, add exact candidate-tree identity, acquire one producer transaction lock before checkout materialization and hold it through graph execution, and bind one receipt to the existing graph index generation. This fixes sequential and concurrent stale reuse without changing cache naming; a narrow `ensure_index()`-only lock is explicitly rejected because it cannot cover current call ordering.
- rejected: candidate-addressed checkout paths. They prevent aliasing but change cache/registry naming, can create a large index per edit, and exceed Issue #10's minimal producer scope.
- rejected: temporary candidate commits as the normal contract. They can bind GitNexus's native HEAD metadata but add checkout mutation/restoration complexity and unnecessarily force commit-shaped analysis.
- rejected: always force GitNexus. It avoids stale reuse but discards valid exact-candidate reuse, adds avoidable runtime cost, and still needs an indexed-candidate identity for the public projection.

### Advisor projection

- chosen: exactly one same-packet semantic projection with stable references to existing graph records.
- rejected: copying graph bodies into the projection because it duplicates ownership and recreates the measured prompt-size defect.
- rejected: a second output endpoint, registry, or persistence store because the issue assigns persistence to the installed downstream adapter.

## Preservation Obligations

- `PRES-1` (behavioral): existing legacy packet fields, full GitNexus evidence, prompt/XML/Markdown renderers, bootstrap exit behavior, and quality-gate consumption retain their current Interface.
- `PRES-2` (behavioral): PR/local/repo target ordering without intent evidence does not change.
- `PRES-3` (behavioral): source checkout files, index, ignore rules, and status remain unchanged by candidate capture, graph indexing, and installation verification.
- `PRES-4` (behavioral): the 20-call cap, file-scoped graph identity validation, required context/impact pair atomicity, and existing real-GitNexus failure semantics remain intact except for the explicitly changed omission truth.
- `PRES-5` (nonbehavioral): GitNexus and downstream `claude-skills` remain the owners of their registry/runtime and workflow/advisor persistence respectively; this producer introduces no competing owner.

## Load-Bearing Assumptions

- `ASSUMP-1` (behavioral, unresolved): a temporary-index Git tree over the admitted candidate file set exactly matches the content GitNexus analyzes, including modes, symlinks, additions, and deletions while excluding tool caches. Falsify through clean-tree equality, dirty transitions, and real public-producer resolution.
- `ASSUMP-2` (behavioral, measured): three forced real GitNexus 1.5.3 rebuilds of one unchanged candidate kept `lastCommit` fixed while both registry and local-meta `indexedAt` changed together on every generation (`10:47:42.949Z`, `10:47:45.069Z`, `10:47:47.192Z`). The receipt binds to checkout identity, committed provenance, candidate tree, and matching registry/local `indexedAt`; mismatched or absent generation metadata blocks.
- `ASSUMP-3` (behavioral, unresolved): the existing per-analysis lock can be widened across candidate materialization through graph execution without deadlock or permitting checkout mutation during queries. Prove lock contention and candidate-mutation cases through the production Seam.
- `ASSUMP-4` (nonbehavioral, unresolved): normalized `origin` remote identity and merge-base OID match the schema-version-1 downstream consumer semantics. Verify against the live downstream issue/consumer before freezing serialization.
- `ASSUMP-5` (nonbehavioral, unresolved): the cohesive implementation can remain below the 1,000-net-line split threshold while retaining real-Seam proof. Measure cumulative net source growth after each GREEN and apply the recorded regroup rule.

<!-- governed-design-labels:v1 -->
```json
{"schemaVersion":1,"labels":[{"id":"PRES-1","kind":"preservation"},{"id":"PRES-2","kind":"preservation"},{"id":"PRES-3","kind":"preservation"},{"id":"PRES-4","kind":"preservation"},{"id":"PRES-5","kind":"preservation"},{"id":"ASSUMP-1","kind":"assumption","behavioral":true},{"id":"ASSUMP-2","kind":"assumption","behavioral":true},{"id":"ASSUMP-3","kind":"assumption","behavioral":true},{"id":"ASSUMP-4","kind":"assumption","behavioral":false},{"id":"ASSUMP-5","kind":"assumption","behavioral":false}]}
```

## Contract And Proof Model

- authoritativeContract: exact file, qualified symbol, and unambiguous exact unqualified symbol references are required; ambiguous or absent references are explicit gaps; required graph groups allocate before optional breadth; graph freshness is exact candidate-tree equality; projection schema version 1 is the consumer compatibility key.
- invariants:
  - required targets bypass optional `top` truncation;
  - optional breadth remains bounded and deterministic;
  - task-state or generic graph scores cannot overtake required intent targets;
  - context/impact pairs are never split;
  - if any required group exceeds the cap, every omitted required check is named, no optional breadth is allocated, and the packet blocks;
  - optional omissions are counted separately and remain non-blocking;
  - no relevant Seam is a discovery gap and keeps `required_checks_resolved=false`;
  - expected and indexed candidate trees match before graph evidence is trusted;
  - a second edit at the same HEAD invalidates the prior receipt automatically;
  - legacy full evidence, packet fields, and human renderers remain unchanged;
  - non-intent ranking behavior remains unchanged when no intent evidence is supplied.
- proofPlan: mapped public-producer RED/GREEN, deterministic focused tests, preserved legacy/corpus cases, repeated real GitNexus replay, one current-tree full suite and quality gate, post-edit GitNexus analysis/detect-changes, structured review, final Codex Advisor, install proof, and current-head PR reviewer audit.

## Scope In

- preserve file and symbol relevance through final ordering.
- exact required reference resolution and deterministic coverage gaps.
- complete planner symbol inventory independent of display limits.
- honest required/optional graph allocation and omission accounting.
- canonical repository/base resolution.
- exact candidate-tree identity, locked analysis, freshness receipt, and automatic stale-overlay reindex.
- one `advisorProjection` schema version 1 in existing normal and blocker machine packets.
- focused README wording for the public contract.

## Scope Out

- downstream checkpoint/prompt assembly or persistence.
- a second context system, graph registry, evidence store, candidate-state owner, workflow phase, or generic ranking framework.
- GitNexus registry-schema or source changes.
- cache checkout naming changes.
- persistence of the projection outside the packet.
- changing the 20-call cap.
- manual cache deletion or required local commits as the runtime contract.
- unrelated ranking, parser, transport, or workflow changes.
- merge.

## Delivery Map

- plan type: one cohesive producer PR because relevance, graph truth, candidate identity, and projection form one atomic public packet Interface and acceptance replay.
- PR count: 1.
- branch: `fix/issue10-intent-graph-freshness` from `origin/main`.
- stack depth: 1.
- owner slice: Issue #10 producer only.
- target budget before coding:
  - `workflow_index.py`: 50–75 net lines.
  - intent/planner portions of `repo_context_forge.py`: 90–130 net lines.
  - `gitnexus_analysis.py`: 80–120 net lines.
  - candidate/projection portions of `repo_context_forge.py`: 90–130 net lines.
  - tests: 180–230 net lines through reused public-bootstrap helpers and table-driven cases.
  - README: 10–20 net lines.
  - total target: approximately 500–650 net human-authored source lines.
- review-budget warning: the forecast may remain materially above the ~500 target. Before coding, reuse existing test helpers and remove duplicated parsing/ordering rather than compressing proof. The one-PR exception is justified only while the atomic Interface stays under the 1,000 split threshold.
- regroup rule: if measured forecast or cumulative growth reaches 1,000 net lines, stop. Split only if two independently reviewable and verifiable Interfaces exist: A intent resolution/planning; B candidate freshness/projection based on A. Lack of independent verification is a reason not to split.
- consolidation stop: no more than two dependent PRs; if reviewer feedback crosses both Interfaces or ownership overlaps, consolidate rather than extend the stack.

## Commit Ownership

1. `fix: preserve intent graph relevance`
   - owns `workflow_index.py`;
   - owns intent-resolution, target-ordering, coverage, and planner portions of `repo_context_forge.py`;
   - owns corresponding focused/public tests;
   - must pass focused tests and leave no duplicate resolver, temporary compatibility layer, or test-only Seam.
2. `fix: bind graph evidence to candidate trees`
   - owns `gitnexus_analysis.py`;
   - owns candidate identity and projection portions of `repo_context_forge.py`;
   - owns corresponding focused/public tests and `README.md`;
   - must pass focused tests and leave no manual recovery path or optimistic indexed identity.

`codex_context_bootstrap.py`, installer code, Skill metadata, and plugin metadata are verification-only in both commits.

## PR Plan

| PR | Branch | Base | Owner Slice | Commit Structure | Verification | Entry | Exit |
|---|---|---|---|---|---|---|---|
| A | `fix/issue10-intent-graph-freshness` | `origin/main` `53400d3` | complete producer contract for Issue #10 | two commits with ownership above | focused/public real-GitNexus tests, full suite, quality gate, GitNexus reanalysis, structured review, final advisor, install and reviewer gates | clean aligned worktree and completed governed preflight | pushed PR head; every signal fixed or rejected with evidence; checks green; zero unresolved non-outdated threads; not merged |

## Verification Plan

- first behavior-specific RED/GREEN: one public-bootstrap intent names one globally unambiguous symbol placed beyond the display slice and asserts its real GitNexus context/impact pair is absent before the fix and resolved after it.
- later acceptance replay requires `_finding_dispositions`, `advisor_disposition`, `advisor_disposition_document`, `_apply_finding_dispositions`, and `tree_manifest`; generic declarations do not consume required slots.
- repeat the preserved `#143` replay twice with configured real GitNexus and compare target ranks, plan, required/optional omissions, gaps, projection, and coverage result.
- intent resolution: exact/root file, qualified symbol, same-file class-qualified method, globally unambiguous unqualified symbol, global ambiguity outside selected targets, absence, exclusion, no relevant Seam, summary match, required-over-`top`, task-state ordering.
- planner/display: required symbol after position 80, relevant optional symbol after display position 12, display-limit independence, identity-scoped duplicates, required-before-optional allocation, required-over-cap block, separate omission ordering/counting.
- candidate identity: clean PR/repo candidates; dirty local/intent candidates; tracked edit, eligible untracked file, deletion, rename/path, executable mode, symlink, second edit, tool-cache exclusion, deterministic repeat, source/index cleanliness, source mutation during capture.
- receipt/freshness: missing, malformed, wrong checkout, wrong candidate, stale committed provenance, missing index storage, failed rebuild, candidate mutation during rebuild, lock contention, check-only block, unchanged-candidate reuse.
- real freshness proof: same temporary repository, cache, registry HOME, and committed HEAD; first uncommitted symbol causes reindex and real context/impact resolution; second edit causes a new tree and reindex without deletion; unchanged third run is fresh.
- identity projection: canonical `sourceRepo`; mode-specific `sourceBaseOid`; detached/no-remote/unresolved-base named gaps; normal and blocker projection exactly once; whole records beyond display limit; stable references resolve; no graph duplication; deterministic repeat.
- compatibility: all legacy packet fields, required full graph evidence, prompt/XML/Markdown output, quality-gate consumption, bootstrap exit behavior, installer behavior, and non-intent ranking remain unchanged.
- ownership proof for `PRES-5`: inspect the final diff and executable Interfaces to require zero GitNexus registry-schema changes, zero projection persistence, zero downstream workflow/advisor edits, and exactly one producer receipt colocated with the cache-owned graph state; the typed quality gate and structured review disposition this explicitly.
- unit substitutions may prove serialization and failure branches only. Acceptance freshness and symbol-resolution proof cross the public producer and real GitNexus Seam.
- final sequence: focused iteration; one current-tree full suite and typed quality gate; public real-GitNexus replay; post-edit Repo Context Forge/GitNexus evidence; detect-changes; structured review; final advisor; commit/push; scoped install; current-head reviewer audit.
- commands:
  - mapped focused tests through workflow TDD producers.
  - `PYTHONPATH=. python3 tests/test_repo_context_forge.py`.
  - `git diff --check`.
  - typed production quality gate against the recorded base.
  - `gitnexus analyze --force --skip-agents-md "$PWD"` and source-checkout detect-changes.

## Execution Checklist

- [x] complete planning delegate fan-in and draft critique
- [x] tracked governing artifact created
- [x] authority/base/branch verified
- [x] public producer occurrence reproduced
- [ ] preflight Codex Advisor scope disposition recorded
- [ ] production preflight and Behavior Map recorded
- [ ] mapped TDD RED for intent relevance and planning
- [ ] invoke and record production-code baseline
- [ ] implement and GREEN intent resolution/planning; record Behavior Map GREEN and required reassessment evidence
- [ ] mapped TDD RED for candidate freshness and projection
- [ ] implement and GREEN candidate freshness/projection; record Behavior Map GREEN and required reassessment evidence
- [ ] update only focused README contract text
- [ ] focused and full verification complete on the current tree
- [ ] post-edit Repo Context Forge and GitNexus evidence current
- [ ] structured code-review findings dispositioned
- [ ] independent final Codex Advisor result is commit-ready
- [ ] workflow complete
- [ ] two coherent commits created and pushed; PR opened or updated
- [ ] scoped installation records branch, commit, and changed path set
- [ ] reviewer/check roster classified on the current head
- [ ] every legitimate signal resolved or rejected with evidence; zero unresolved non-outdated threads
- [ ] no merge performed

A newly discovered scope change amends this artifact explicitly or opens a new governed pass; implementation does not re-plan this pass.

## Change Log

- 2026-08-23: created after complete four-delegate fan-in, public-producer reproduction, and integration of the independent draft critique.
