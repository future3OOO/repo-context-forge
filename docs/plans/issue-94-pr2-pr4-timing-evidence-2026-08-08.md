# Issue 94 PR #2 Versus PR #4 Timing Evidence

## Scope

Focused producer timing only. This is not the governed DeepSWE benchmark, which remains deferred until the claude-skills consumer half of Issue #94 exists.

## Revisions And Environment

- PR #2: `0cbbd10fb7b4d0c98244a0fccb61f6de0a6913b2`
- PR #4 candidate: working tree based on `b372a037a853b79c9b9831a6b60f24c219b28eb5`
- GitNexus dependency: unmerged PR #3 `1841c6cbd405e7059b68d456477982a79fbe101c` (package version 1.5.3, distinct from the released/installed 1.5.3 build that lacks `impact --uid`)
- fixture commit: `ddb28437e6200255e01f549d91b4678f52504354`
- host: Linux 6.6.87.2-microsoft-standard-WSL2 x86_64
- Python 3.12.3; Node v24.14.1

Both revisions used the same committed fixture, `repo` mode, `--top 20`, `--map-build never`, GitNexus `auto` mode, isolated HOME/cache directories, and one unrecorded warm-up. PATH selected the executable built from exact GitNexus commit `1841c6c`; no live installation was changed. Five recorded runs were interleaved PR #2 then PR #4.

## Results

| Iteration | PR #2 (ms) | PR #4 (ms) | Delta (ms) |
|---:|---:|---:|---:|
| 1 | 5,957 | 5,616 | -341 |
| 2 | 3,244 | 5,692 | 2,448 |
| 3 | 2,876 | 5,867 | 2,991 |
| 4 | 2,882 | 8,467 | 5,585 |
| 5 | 3,030 | 5,674 | 2,644 |
| Mean | 3,598 | 6,263 | 2,665 |
| Median | 3,030 | 5,692 | 2,662 |

The populated PR #4 packet selected 20 checks, truthfully reported 16 omitted checks, executed 20 serial GitNexus processes/calls, and resolved in 4,965 ms inside a 7,725 ms end-to-end evidence run. It produced 13,974 bytes of GitNexus output. PR #2 rendered the same 20-check plan but did not execute the semantic calls.

## Disposition

- The intentional bounded selection remains at 20.
- Every selected check remains mandatory and serial; crash-dump and single-flight protections are unchanged.
- Omitted checks are now a non-blocking coverage fact, not a hidden truncation or refusal condition.
- No performance-driven parallelism, batching, cache policy, or functionality reduction is introduced in this slice.
