# Issue 94 Repo Context Forge Producer Plan

## Status

- current state: in progress
- governing artifact: this file
- last updated: 2026-08-08

## Objective

- Canonicalize Repo Context Forge on one branch and implement only Issue #94's producer slice.
- Success means one pushed PR head owns PR #2 behavior, PR #3's demonstrated reindex protection, and semantic GitNexus results from the existing `make_packet` Interface.

## Source Of Truth

- authority: Issue #94 and the user's bounded producer-slice request
- trusted base: Repo Context Forge PR #2 head `0cbbd10fb7b4d0c98244a0fccb61f6de0a6913b2`
- live authorities: Claude executes clean `/home/prop_/projects/repo-context-forge` at PR #3 head `63be87513ec51b05976726977e3a43a0ce5bb774`; Codex executes separate dirty `/home/prop_/.codex/plugins/cache/local-codex-plugins/repo-context-forge/0.1.0` at `b5910a6c772c5640eed254c9d66ef8928e882837` plus its overlay.
- donors: PR #3 `f5b6f9421b2e76169614c42588b7ed0ce2803f9a` for single-flight/core-dump protection; both live authorities are comparison evidence and must be reconciled, not described as one install.

## Affected Surface

- changed boundary: `make_packet` creates a bounded, normalized semantic GitNexus result and the bootstrap can atomically write that same packet as JSON.
- adjacent consumers: CLI analyze/wrap/benchmark, Codex bootstrap, prompt renderer, and existing tests.
- no-change surfaces: checkout/index authority, workflow-index ranking, installer, default prompt stdout, source cleanliness, and all claude-skills/Issue #95 behavior.

## Contract And Proof Model

- authoritativeContract: each check is identified by kind, file, target/resolved UID, and direction; results are accepted only against that identity and execute serially once.
- invariants: maximum 20 checks; empty plan makes zero semantic calls; stale, malformed, failed, ambiguous, or mismatched results block; reindex is single-flight and suppresses core dumps.
- proofPlan: real GitNexus duplicate-name public-bootstrap RED/GREEN, focused invariant tests, full suite, quality gate, GitNexus reanalysis, code review, Claude challenge, and current-head reviewer audit.

## Scope In

- Deepen the existing GitNexus owner as one cohesive `gitnexus_analysis.py` boundary Module; keep plan construction, rendering, and `make_packet` orchestration in `repo_context_forge.py`.
- Preserve PR #3 commit `f5b6f94` before adding semantic calls.
- Add normalized context/impact answers, bounds/metrics, truthful blocker behavior, prompt summary, and optional atomic machine JSON output.
- Update only the matching operator contract.

## Scope Out

- claude-skills Issue #95 and manual workflow bookkeeping changes.
- New graph frameworks, coverage scorers, repository resolvers, query languages, or Adapter hierarchies.
- PR #3 cache-retention commit `63be875` and the installed overlay's duplicate `architecture_summary.py`.
- Merge actions.

## Authority And Conflict Rule

- Preserve demonstrated runtime behavior, but Issue #94 producer scope wins over unrelated donor changes.
- A GitNexus result that cannot be bound to the planned identity is unresolved, never guessed.

## Delivery Map

- plan type: single-PR consolidation
- PR count: 1
- stack depth: 1
- estimated net implementation: at or below 500 lines; stop and shrink before 1,000.
- regroup rule: stop if the implementation creates a second owner or exceeds the split threshold.
- deploy freeze: do not replace either live authority until the exact pushed head passes verification and reviewer audit; preserve each old location independently as a recoverable backup.

## PR Plan

| PR | Branch | Base | Owner Slice | Commit Structure | Verification | Entry | Exit |
|---|---|---|---|---|---|---|---|
| A | `codex/issue-94-graph-analysis` | PR #2 `0cbbd10` | Repo Context Forge producer only | PR #3 safety; producer TDD/implementation; review fixes if needed | real-seam test, full suite, quality/GitNexus/advisor/reviewer gates | clean isolated clone | pushed clean head and matching clean install |

## Verification Plan

- targeted tests: duplicate symbol identity, empty plan, canonical dedup, normalization/bounds/failure, packet JSON atomicity.
- combined workflow proof: public bootstrap against a real temporary Git repository and real GitNexus CLI.
- focused invariant checks: PR #3 lock/core-dump tests and source-cleanliness tests.
- full gate: `PYTHONPATH=. python3 tests/test_repo_context_forge.py`, `git diff --check`, production-code gate.
- post-push: exact head/tree/upstream checks, live PR checks/threads, then independently back up and replace Claude's source checkout and Codex's plugin checkout with clean checkouts of that exact commit; compare commit/tree and engine-file hashes across both.

## Execution Checklist

- [x] refs, authority, isolated checkout, Repo Context Forge intake, GitNexus scope, diagnosis, Claude scope check, and preflight complete
- [x] preserve PR #3 safety and produce duplicate-name RED
- [x] implement producer result and atomic packet output
- [ ] run full verification, review, and Claude challenge
- [ ] commit, push, open/update PR, and close current-head reviewer loop
- [ ] back up and replace live install with the exact pushed commit
- [ ] record exact donor/overlay dispositions and final identity proof

## Change Log

- 2026-08-08: created from re-queried refs and preflight evidence.
- 2026-08-08: quality-gate precheck required the touched 3,454-line owner to shrink; moved only its cohesive GitNexus registry/freshness/locking/execution boundary into the issue-specified Graph Analysis Module. Serial process count and runtime behavior remain unchanged.
- 2026-08-08: precommit advisor findings: fixed missing public exit-0 proof and blocker-exit documentation; rejected weakening duplicate-name blockers because Issue #94 explicitly requires unresolved results to block; rejected removing authority/producer revision because both are required result fields; retained two pre-existing reindex unit stubs as non-load-bearing baseline coverage rather than broadening this slice.
