# Issue 94 Repo Context Forge Producer Plan

## Status

- current state: canonical producer implementation complete; merge and dependency activation remain
- governing artifact: this file
- last updated: 2026-08-09
- canonical PR: Repo Context Forge PR #4, branch `codex/issue-94-graph-analysis`, targets `main`
- canonical producer/runtime commit/tree: `68819f7135f517e4a23b97e4b2c6a0413a5a012b` / `07377bb7d83abebc9eac7a385922b1cbd27c1531`

## Objective

- Canonicalize Repo Context Forge on one branch and implement only Issue #94's producer slice.
- Success means one pushed Repo Context Forge PR head owns PR #2 behavior, PR #3's demonstrated reindex protection, truthful bounded semantic GitNexus results, and UID-selected impact through the existing `make_packet` Interface.

## Source Of Truth

- authority: Issue #94 and the user's bounded producer-slice request
- base: Repo Context Forge `main`; PR #4 contains PR #2's complete ancestry through `0cbbd10fb7b4d0c98244a0fccb61f6de0a6913b2`
- initial live authorities: Claude executed clean PR #3 head `63be87513ec51b05976726977e3a43a0ce5bb774`; Codex executed dirty cache head `b5910a6c772c5640eed254c9d66ef8928e882837` plus an overlay. Both installations are now clean at canonical commit `68819f7135f517e4a23b97e4b2c6a0413a5a012b` and tree `07377bb7d83abebc9eac7a385922b1cbd27c1531`; no runtime overlay remains.
- donor disposition: PR #4 reconciles PR #3 commit `f5b6f9421b2e76169614c42588b7ed0ce2803f9a` for demonstrated single-flight/core-dump protection. PR #3's unrelated cache-retirement remainder was deliberately excluded.

## Affected Surface

- changed boundary: `make_packet` creates a bounded, normalized semantic GitNexus result and the bootstrap can atomically write that same packet as JSON.
- adjacent consumers: CLI analyze/wrap/benchmark, Codex bootstrap, prompt renderer, and existing tests.
- no-change surfaces: checkout/index authority, workflow-index ranking, installer, default prompt stdout, source cleanliness, and all claude-skills/Issue #95 behavior.

## Contract And Proof Model

- authoritativeContract: each check is identified by kind, file, target/resolved UID, and direction; results are accepted only against that identity and execute serially once.
- invariants: maximum 20 selected checks; omitted checks are counted exactly and remain non-blocking; empty plan makes zero semantic calls; selected stale, malformed, failed, ambiguous, or mismatched results block; reindex is single-flight and suppresses core dumps.
- proofPlan: real GitNexus duplicate-name public-bootstrap RED/GREEN, focused invariant tests, full suite, quality gate, GitNexus reanalysis, code review, Claude challenge, and current-head reviewer audit.

## Scope In

- Deepen the existing GitNexus owner as one cohesive `gitnexus_analysis.py` boundary Module; keep plan construction, rendering, and `make_packet` orchestration in `repo_context_forge.py`.
- Preserve PR #3 commit `f5b6f94` before adding semantic calls.
- Add normalized context/impact answers, bounds/metrics, truthful blocker behavior, prompt summary, and optional atomic machine JSON output.
- Update only the matching operator contract.

## Scope Out

- manual workflow bookkeeping changes.
- New graph frameworks, coverage scorers, repository resolvers, query languages, or Adapter hierarchies.
- PR #3's second cache-retention/docs delta in `63be875` and the installed overlay's duplicate
  `architecture_summary.py`.
- Merge actions.
- The claude-skills consumer half of Issue #94 and Issue #95.

## Authority And Conflict Rule

- Preserve demonstrated runtime behavior, but Issue #94 producer scope wins over unrelated donor changes.
- A GitNexus result that cannot be bound to the planned identity is unresolved, never guessed.

## Delivery Map

- plan type: cross-repository dependency plus single Repo Context Forge consolidation PR
- PR count: 2 across repositories
- ancestry: Repo Context Forge PR #4 targets `main` and contains PR #2's complete ancestry; after PR #4 merges, PR #2 may close as included and PR #3 may close as superseded, not merged
- dependency order: GitNexus PR #3 is a separate-repository dependency at `255896f08b645384551b3143917248c90b4e7f95` and must merge and ship before UID-dependent activation
- review-size disposition: the combined main-based size/quality-gate complaint is accepted as a non-blocker; do not split or refactor PR #4 on that basis
- regroup rule: stop if the implementation creates a second owner or exceeds the split threshold.
- installations: Claude and Codex are clean at canonical commit/tree `68819f7135f517e4a23b97e4b2c6a0413a5a012b` / `07377bb7d83abebc9eac7a385922b1cbd27c1531`; no runtime overlay remains.
- dependency gate: runtime activation requires GitNexus PR #3 commit `255896f` or a reviewed successor containing it; the released/installed 1.5.3 CLI lacks `impact --uid`. This task does not merge either PR and must not add a bare-name compatibility fallback.

## PR Plan

| PR | Branch | Base | Owner Slice | Commit Structure | Verification | Entry | Exit |
|---|---|---|---|---|---|---|---|
| GitNexus #3 | `codex/uid-impact-selector` | GitNexus `main` `cb772b9` | expose existing UID impact through current CLI/MCP Interface | one dependency commit | real duplicate-name DB test, CLI E2E, typecheck/build, reviewer gates | clean isolated clone | pushed clean dependency head; no merge |
| A | `codex/issue-94-graph-analysis` | Repo Context Forge `main` | Repo Context Forge producer only | PR #2 ancestry; PR #3 demonstrated safety; producer implementation and accepted review fixes | real-seam test, full suite, quality/GitNexus/advisor/reviewer gates | clean isolated clone | canonical producer/runtime commit `68819f7`; both installations clean and identical; no merge performed |

## Verification Plan

- targeted tests: duplicate symbol identity, empty plan, canonical dedup, normalization/bounds/failure, packet JSON atomicity.
- performance evidence: repeated, same-fixture PR #2 versus PR #4 bootstrap timings and graph process counts; the governed DeepSWE benchmark remains deferred to the consumer half of Issue #94.
- combined workflow proof: public bootstrap against a real temporary Git repository and real GitNexus CLI.
- focused invariant checks: PR #3 lock/core-dump tests and source-cleanliness tests.
- full gate: `PYTHONPATH=. python3 tests/test_repo_context_forge.py`, `git diff --check`, production-code gate.
- post-push: exact head/tree/upstream checks, live PR checks/threads, then independently back up and replace Claude's source checkout and Codex's plugin checkout with clean checkouts of that exact commit; compare commit/tree and engine-file hashes across both.

## Execution Checklist

- [x] refs, authority, isolated checkout, Repo Context Forge intake, GitNexus scope, diagnosis, Claude scope check, and preflight complete
- [x] preserve PR #3 safety and produce duplicate-name RED
- [x] implement producer result and atomic packet output
- [x] run full verification, review, and Claude challenge
- [x] publish GitNexus UID-impact dependency PR #3 and its reviewer-fix head at `255896f08b645384551b3143917248c90b4e7f95`
- [x] report exact omitted-check count non-blockingly through analysis, JSON, and prompt
- [x] consume context-resolved UID through the dependency and make the real duplicate-name test pass
- [x] reproduce the nested no-symbol `file_context` blocker against GitNexus `255896f`
- [x] add a real public-bootstrap RED/GREEN test and resolve files through exact `File:<repository-path>` UID identity
- [x] refresh repeated PR #2-versus-corrected-PR #4 timing evidence against GitNexus `255896f`
- [x] rerun full verification and Standards/Spec code review on the correction
- [x] complete the Claude precommit challenge on the live correction diff
- [x] triage the retargeted exact-head OPS findings with premise and occurrence evidence
- [x] fix the four demonstrated OPS occurrences through existing SoulForge/workflow-index owners; reject three zero-occurrence findings without code
- [x] validate the final parser over 1,470 content-deduplicated captured files: 197 false callables removed, 12 named default exports added, demonstrated multiline/deeply nested arrows retained, and zero unexpected additions
- [x] refresh final-candidate timing, verification, code review, and Claude challenge after OPS remediation
- [x] close the demonstrated Cubic file-context UID mismatch and three operator-contract defects without adding code for zero-occurrence findings
- [x] close the exact-head reviewer loop: 103/103 tests; Cubic success; OPS terminal neutral/unavailable; CodeRabbit rate-limited; zero unresolved non-outdated threads
- [x] install canonical commit/tree cleanly for Claude and Codex with no runtime overlay
- [x] record exact PR #2 ancestry, PR #3 donor/cache-remainder disposition, and final identity proof
- [ ] merge PR #4 when authorized; then close Repo Context Forge PR #2 as included and PR #3 as superseded, not merged
- [ ] implement Issue #94's claude-skills consumer half and Issue #95 in their own scopes

## Change Log

- 2026-08-08: created from re-queried refs and preflight evidence.
- 2026-08-08: quality-gate precheck required the touched 3,454-line owner to shrink; moved only its cohesive GitNexus registry/freshness/locking/execution boundary into the issue-specified Graph Analysis Module. Serial process count and runtime behavior remain unchanged.
- 2026-08-08: precommit advisor findings: fixed missing public exit-0 proof and blocker-exit documentation; rejected weakening duplicate-name blockers because Issue #94 explicitly requires unresolved results to block; rejected removing authority/producer revision because both are required result fields.
- 2026-08-08: current-head review fixes write machine JSON for early blockers, preserve context/impact pairs and the 20-check cap, align operator wording, and clarify the PR #3 authority/donor disposition. Replaced the two programmed-collaborator reindex tests with an explicit assertion on the real public reindex path. Full suite: 94 passed; producer-donor and review-fix quality gates: `ok: true`; Standards and producer-slice Spec reviews: zero findings.
- 2026-08-08: OPS follow-up admitted two production occurrences: silent cap omission and duplicate-name impact refusal. Published the minimal GitNexus dependency as future3OOO/GitNexus#3; this plan now governs its Repo Context Forge consumer, truthful non-blocking omission reporting, and focused timing evidence.
- 2026-08-08: follow-up RED/GREEN complete against exact GitNexus dependency `1841c6c`: 24 canonical checks report 20 selected and 4 omitted without blocking; duplicate-name context and impact resolve both file-scoped UIDs. Full suite: 95 passed. Follow-up and PR #2-base production gates pass; Standards and producer-slice Spec reviews have zero findings.
- 2026-08-08: GitNexus reviewer follow-up `255896f` preserves JVM Class/Interface constructor and file traversal for UID impact and removes two fake-green CLI test exits. Repo Context Forge runtime activation now binds that reviewed successor; the original timing evidence remains attributed to the exact `1841c6c` build measured there.
- 2026-08-08: precommit challenge confirmed the implementation but blocked live activation on dependency ordering: installed package version 1.5.3 is not the PR #3 build. Recorded the exact-commit gate and rejected a compatibility shim; no merge performed.
- 2026-08-09: admitted the demonstrated nested-file blocker: `file_context` passed a repository path as a symbol name. A real public-bootstrap test now selects a nested no-symbol file with a duplicate basename, and the existing Graph Analysis Module resolves it through GitNexus's exact `File:<repository-path>` UID Interface while retaining file/UID validation. Refreshed timing uses corrected PR #4 code and GitNexus `255896f`.
- 2026-08-09: acceptance evidence is reported precisely: the prior OPS run ended `review_incomplete` with one medium notice; Repo Context Forge CodeRabbit skipped the non-default-base diff; GitNexus CodeRabbit was rate-limited; and the GitNexus full suite result was 4,665 passed, 143 documented environment/optional-parser failures, and 171 skipped, with the directly affected suites green. `/bin/false` remains diagnostic-only evidence, not real-runtime Issue #94 proof.
- 2026-08-09: after retargeting PR #4 to `main`, OPS reported seven full-ancestry findings. Admitted four with captured occurrences: bounded direct-dependent reporting (three maps at nine), false JavaScript callables, omitted named default exports, and the live Python test-companion pair. Fixed them within the existing SoulForge/workflow-index owners with public RED/GREEN tests. Claude's challenge exposed that the first regex correction also dropped real multiline and deeply nested arrow declarations; a second RED/GREEN now validates the matching-parenthesis boundary. The final 1,470-file content-deduplicated corpus differential removes 197 false callables, adds 12 named default exports, retains the demonstrated arrow declarations, and has zero unexpected additions. Changed-file truncation (current PRs: 8/4/16 files), `gh pr view` timeout (0.66-second observed call), and failed-fetch freshness (successful exact-head fetch) remain reported-not-actioned after zero demonstrated occurrence.
- 2026-08-09: the OPS remediation passes its `302a340` fixed-point production gate (71 source additions, 25 deletions) and 99 tests. A temporary retarget to `main` exposed inherited PR #2 ancestry and failed the full-base gate with 27 duplicate added blocks and 3,594 added versus 134 deleted source lines; that was a review-base error, not PR #4 implementation scope.
- 2026-08-09: the resumed Claude challenge first blocked the one-line parser correction after reproducing lost multiline and deeply nested arrow declarations. The matching-parenthesis RED/GREEN and corpus validation closed that regression; the resumed final verdict was commit-ready. Its requested packet-field assertion now proves the same authoritative dependent count through `make_target_entries`. The refreshed timing is a new exact-candidate baseline, not an attribution against the earlier run because the recorded method now states `--allow-missing-map` and the shared active GitNexus registry explicitly.
- 2026-08-09: current-head Cubic triage reproduced one Issue #94 identity defect: a `file_context` result with `Method:docs/ARCHITECTURE.md:render` and the planned file path was accepted as resolved. The existing executor now requires exact `File:<repository-path>` identity inline. The existing real-CLI nested-file bootstrap remains the non-regression proof; no fake GitNexus test was added. Three operator corrections use the verified installed `$SKILL_DIR` layout and align two inherited plans with real symbols/fail-closed behavior. Failed fetch, timeout metrics, malformed registry, and machine-local fixture paths remain measured report lines after zero demonstrated occurrence.
- 2026-08-09: per operator correction, PR #4 was restored from `main` to PR #2's branch `codex/app-grade-forge-consolidation`. The resulting PR #4 diff is 11 files and 464 net human-authored source lines over PR #2, restoring reviewability. PR #2 now merges first when authorized; PR #4 is then retargeted to `main`. No merge was performed.
- 2026-08-09: final wording reconciliation records canonical PR #4 at `68819f7135f517e4a23b97e4b2c6a0413a5a012b` / tree `07377bb7d83abebc9eac7a385922b1cbd27c1531`, targeting `main` with PR #2's complete ancestry. PR #3's demonstrated single-flight/core-dump protection is reconciled; its unrelated cache-retirement remainder remains deliberately excluded. Current acceptance is 103/103 tests, exact-head Cubic success, OPS terminal neutral/unavailable, CodeRabbit rate-limited, and zero unresolved non-outdated threads. The recorded final-head timing is PR #2 median 14.18s versus PR #4 median 15.56s, a +1.38s / 9.7% delta. Both installations are clean and identical with no runtime overlay. The combined main-based size/quality-gate complaint is accepted as non-blocking; no split or refactor is required.
