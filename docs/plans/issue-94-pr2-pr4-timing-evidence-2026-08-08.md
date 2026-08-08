# Issue 94 PR #2 Versus PR #4 Timing Evidence

## Scope

Focused producer timing only. This is not the governed DeepSWE benchmark, which remains deferred until the claude-skills consumer half of Issue #94 exists.

## Revisions And Environment

- PR #2: `0cbbd10fb7b4d0c98244a0fccb61f6de0a6913b2`
- PR #4 corrected candidate: working tree based on `ec4ee237f4c643e5bfda54654dc8a81b8e8f80f6`, including the exact file-context fix measured here (`gitnexus_analysis.py` SHA-256 `835013b419bc023c0ed5c5853a90c612cf0f159c9cf6304b68549541f03c7f30`)
- GitNexus dependency: unmerged PR #3 `255896f08b645384551b3143917248c90b4e7f95` (the clean active runtime configured for both agents)
- fixture commit: `ddb28437e6200255e01f549d91b4678f52504354`
- host: Linux 6.6.87.2-microsoft-standard-WSL2 x86_64
- Python 3.12.3; Node v24.14.1

Both revisions used the same committed fixture, `repo` mode, `--top 20`, `--map-build never`, GitNexus `auto` mode, isolated HOME/cache directories, and one unrecorded warm-up. PATH selected the executable built from exact GitNexus commit `255896f`; no live Repo Context Forge installation was changed. Five recorded runs were interleaved PR #2 then PR #4 on 2026-08-09.

## Results

| Iteration | PR #2 (ms) | PR #4 (ms) | Delta (ms) |
|---:|---:|---:|---:|
| 1 | 3,063 | 5,942 | 2,879 |
| 2 | 3,973 | 6,171 | 2,198 |
| 3 | 3,809 | 5,661 | 1,852 |
| 4 | 3,040 | 6,190 | 3,150 |
| 5 | 3,247 | 6,987 | 3,740 |
| Mean | 3,426 | 6,190 | 2,764 |
| Median | 3,247 | 6,171 | 2,879 |

The populated corrected PR #4 packet selected 20 checks, truthfully reported 16 omitted checks, executed 20 serial GitNexus processes/calls, and resolved in 5,878 ms in the final evidence run. It produced 14,265 bytes of GitNexus output. PR #2 rendered the same 20-check plan but did not execute the semantic calls.

## Disposition

- The intentional bounded selection remains at 20.
- Every selected check remains mandatory and serial; crash-dump and single-flight protections are unchanged.
- Omitted checks are now a non-blocking coverage fact, not a hidden truncation or refusal condition.
- No performance-driven parallelism, batching, cache policy, or functionality reduction is introduced in this slice.
