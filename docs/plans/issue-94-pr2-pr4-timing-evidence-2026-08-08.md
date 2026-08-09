# Issue 94 PR #2 Versus PR #4 Timing Evidence

## Scope

Focused producer timing only. This is not the governed DeepSWE benchmark, which remains deferred until the claude-skills consumer half of Issue #94 exists.

## Revisions And Environment

- PR #2: `0cbbd10fb7b4d0c98244a0fccb61f6de0a6913b2`
- historical corrected-candidate run: working tree based on pushed head `302a3407883f810f76aed0857fee1549672e2ed4`, including the exact file-context fix and admitted OPS remediation measured in the table below (`gitnexus_analysis.py` SHA-256 `835013b419bc023c0ed5c5853a90c612cf0f159c9cf6304b68549541f03c7f30`; `repo_context_forge.py` `06f2e6a8ba332a642f88726258827434d8c14b113810cc0d0940d93ba86f852a`; `workflow_index.py` `20f966ff23e6a848a2adee78b21fc9ef993d8f42bc7247e4c427043c1a67d6cd`)
- canonical PR #4 producer/runtime commit/tree: `68819f7135f517e4a23b97e4b2c6a0413a5a012b` / `07377bb7d83abebc9eac7a385922b1cbd27c1531`
- GitNexus dependency: unmerged PR #3 `255896f08b645384551b3143917248c90b4e7f95` (the clean active runtime configured for both agents)
- fixture commit: `ddb28437e6200255e01f549d91b4678f52504354`
- host: Linux 6.6.87.2-microsoft-standard-WSL2 x86_64
- Python 3.12.3; Node v24.14.1

Both revisions used the same committed fixture, `repo` mode, `--top 20`, `--map-build never`, `--allow-missing-map`, GitNexus `auto` mode, isolated cache directories, the same active GitNexus registry, and one unrecorded warm-up. PATH selected the executable built from exact GitNexus commit `255896f`; no live Repo Context Forge installation was changed. Five recorded runs were interleaved PR #2 then PR #4 on 2026-08-09.

## Historical Corrected-Candidate Results

| Iteration | PR #2 (ms) | PR #4 (ms) | Delta (ms) |
|---:|---:|---:|---:|
| 1 | 2,977 | 6,156 | 3,179 |
| 2 | 2,960 | 6,196 | 3,236 |
| 3 | 3,164 | 6,295 | 3,131 |
| 4 | 3,003 | 7,193 | 4,190 |
| 5 | 3,118 | 6,212 | 3,094 |
| Mean | 3,044 | 6,410 | 3,366 |
| Median | 3,003 | 6,212 | 3,209 |

The populated corrected PR #4 packet selected 20 checks, truthfully reported 16 omitted checks, executed 20 serial GitNexus processes/calls, and resolved in 6,083 ms in the final evidence run. It produced 14,265 bytes of GitNexus output. PR #2 rendered the same 20-check plan but did not execute the semantic calls.

## Recorded Final-Head Result

The separately recorded final-head run measured PR #2 at a 14.18s median and canonical PR #4 producer/runtime commit `68819f7135f517e4a23b97e4b2c6a0413a5a012b` at a 15.56s median: +1.38s / 9.7%. These values apply only to that recorded final-head run; the historical table above remains attributed to its documented corrected-candidate revision and method.

## Disposition

- The intentional bounded selection remains at 20.
- Every selected check remains mandatory and serial; crash-dump and single-flight protections are unchanged.
- Omitted checks are now a non-blocking coverage fact, not a hidden truncation or refusal condition.
- No performance-driven parallelism, batching, cache policy, or functionality reduction is introduced in this slice.
