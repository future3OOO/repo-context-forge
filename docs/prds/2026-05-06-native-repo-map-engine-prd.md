# PRD: Native Repo Map Engine For RepoForge

## Problem Statement

RepoForge currently depends on SoulForge to produce the repository map that powers target selection, impact hints, semantic summaries, and packet rendering. That dependency makes RepoForge harder to run consistently across WSL, Mac, and clean agent environments because production bootstrap depends on an external SoulForge binary, Bun runtime availability, copied local paths, and a generated map database that RepoForge does not own.

From the user's perspective, RepoForge should be a dependable Codex workflow module that can run in any target checkout without installing or copying SoulForge. It should still provide rich map context before code reasoning, but the map generation contract should belong to RepoForge itself.

The current architecture already has a useful separation: RepoForge builds or receives a map, reads it through a SQLite map adapter, ranks targets, builds a coverage plan, checks GitNexus freshness, and renders a packet. The missing piece is a RepoForge-owned native map engine that can generate a high-quality map directly from a cache-owned analysis checkout.

## Solution

Build a native RepoForge map engine that replaces SoulForge as the default map builder while preserving compatibility with existing SoulForge-generated map databases during migration.

The native engine will parse repository files, extract symbols, resolve imports and references, build file and symbol graph edges, compute ranking signals, record co-change history, and write a map database that the existing packet pipeline can consume. The engine should be designed as a deep module: callers ask for a fresh map for an analysis checkout, and the implementation hides language parsing, graph construction, centrality scoring, metadata, caching, and fallback behavior.

SoulForge remains an explicit compatibility adapter, not a default runtime dependency. GitNexus remains the source of verified execution-flow and blast-radius claims. The native map orients the agent; GitNexus verifies the highest-risk relationships before edits.

## User Stories

1. As a Codex user, I want RepoForge to run without installing SoulForge, so that a new machine can use the workflow without copying private local tooling.
2. As a Codex user, I want RepoForge to work the same on WSL and Mac, so that both environments can participate in the same GitHub workflow.
3. As a Codex user, I want RepoForge to build its own map from the target checkout, so that packet quality does not depend on external runtime drift.
4. As a Codex user, I want RepoForge to prove the map matches the exact target head, so that context is never silently stale.
5. As a Codex user, I want RepoForge to preserve clean source checkout behavior, so that map generation never leaves cache files or ignore-rule changes in my repository.
6. As a Codex user, I want RepoForge to keep using cache-owned analysis checkouts, so that PR, local, intent, and repo modes remain isolated and repeatable.
7. As a Codex user, I want RepoForge to rank changed production files before broad tests, so that agents start in the behavior path that matters most.
8. As a Codex user, I want RepoForge to rank tests as verification context, so that agents still inspect the right proof surface.
9. As a Codex user, I want RepoForge to rank graph neighbors and reverse dependents, so that callers and affected files are visible before edits.
10. As a Codex user, I want RepoForge to use co-change history, so that files that usually change together are surfaced even when import edges miss them.
11. As a Codex user, I want RepoForge to understand task intent, so that clean-checkout planning can still identify likely target files.
12. As a Codex user, I want RepoForge to extract exported symbols, so that packets show the public interface of likely target modules.
13. As a Codex user, I want RepoForge to extract local symbols where useful, so that agents can orient inside large files without opening everything.
14. As a Codex user, I want RepoForge to support JavaScript and TypeScript well, so that Property Partner OPS workflows get accurate imports, exports, route-like declarations, hooks, and tests.
15. As a Codex user, I want RepoForge to support Python well, so that RepoForge itself and Python-based tooling get accurate symbols and imports.
16. As a Codex user, I want RepoForge to support Lean where it matters, so that lean-code can be mapped without pretending it is JavaScript or Python.
17. As a Codex user, I want unsupported languages to degrade honestly, so that missing symbol extraction does not create false confidence.
18. As a Codex user, I want confidence labels on map signals, so that exact AST edges, probable framework edges, and textual matches are not treated as equal.
19. As a Codex user, I want GitNexus claims separated from map hints, so that agents do not confuse orientation with verified blast-radius proof.
20. As a Codex user, I want RepoForge to keep old SoulForge map compatibility, so that existing fixtures and fallback behavior remain useful during migration.
21. As a Codex user, I want explicit native-builder warnings, so that a failed map build is distinguishable from an unavailable external fallback.
22. As a Codex user, I want the default path to avoid looking for SoulForge, so that hidden machine state does not decide whether RepoForge works.
23. As a Codex user, I want an explicit fallback option for SoulForge, so that old maps and emergency compatibility remain available while native parity matures.
24. As a Codex user, I want map metadata to record builder name, builder version, source head, analysis head, build mode, and build time, so that freshness can be audited.
25. As a Codex user, I want generated, vendor, and cache paths excluded unless explicitly changed, so that packets do not waste attention on tool artifacts.
26. As a Codex user, I want the ranking policy to be deterministic, so that test fixtures can prove behavior and agents see stable packets.
27. As a Codex user, I want RepoForge packet quality to match or beat current SoulForge-backed packets, so that the migration improves the workflow rather than only simplifying setup.
28. As a Codex user, I want the native engine to improve symbol extraction over time, so that RepoForge becomes better at choosing the right module and seam.
29. As a Codex user, I want map generation to be fast enough for startup, so that Codex receives context before substantive reasoning without a long delay.
30. As a Codex user, I want map caching to reuse fresh maps safely, so that repeated sessions are fast while stale maps are rejected.
31. As a Codex user, I want RepoForge to continue producing coverage plans, so that agents know what must be read before edits or GitNexus claims.
32. As a Codex user, I want RepoForge to continue producing GitNexus required checks, so that map-selected surfaces are verified before production changes.
33. As a Codex user, I want RepoForge to preserve prompt token budgeting, so that packets stay useful without overwhelming the conversation.
34. As a Codex user, I want RepoForge to preserve per-turn refresh behavior, so that files read, searched, edited, and mentioned can influence later packets.
35. As a Codex maintainer, I want a clear map-builder seam, so that native and external builders can be tested independently.
36. As a Codex maintainer, I want a clear map-reader module, so that database schema details do not leak into ranking and rendering callers.
37. As a Codex maintainer, I want language analyzers behind adapters, so that JavaScript, TypeScript, Python, and Lean support can evolve independently.
38. As a Codex maintainer, I want a graph builder module, so that file edges, symbol edges, reverse dependents, co-change, and centrality are built in one place.
39. As a Codex maintainer, I want ranking to consume map facts rather than parse files, so that ranking tests can focus on policy instead of syntax.
40. As a Codex maintainer, I want the native engine tested through the same packet interface agents use, so that implementation details can change without weakening behavior.
41. As a Codex maintainer, I want the old SoulForge adapter tested as a compatibility adapter, so that fallback behavior stays deliberate.
42. As a Codex maintainer, I want no-source-mutation tests for every mode, so that cache safety remains a production contract.
43. As a Codex maintainer, I want builder selection tests with SoulForge absent from PATH, so that standalone behavior is proven.
44. As a Codex maintainer, I want native and legacy map fixture tests, so that schema compatibility is explicit.
45. As a Codex maintainer, I want PR, local, intent, and repo packet smoke tests, so that the native engine works across all bootstrap modes.
46. As a Codex maintainer, I want GitNexus-off and GitNexus-auto tests, so that native map changes do not weaken GitNexus gating.
47. As a Codex maintainer, I want benchmark fixtures from Property Partner OPS, RepoForge, and lean-code, so that quality is measured against real workflow repositories.
48. As a Codex maintainer, I want changed-line budgets and PR slices, so that the native engine does not become a sprawling rewrite.
49. As a Codex maintainer, I want a release gate proving bootstrap works without SoulForge or Bun, so that standalone release readiness is measurable.
50. As a Codex maintainer, I want clear documentation for the new default and fallback modes, so that future agents do not rediscover the same setup problem.

## Implementation Decisions

- RepoForge will own the production map build contract. The default map build path must not depend on discovering a SoulForge binary or Bun runtime.
- The core deep Module is the native map builder. Its Interface should be small: given an analysis checkout and build policy, produce a fresh map result with metadata, warnings, and failure information. Its Implementation hides walking, parsing, graph construction, centrality, co-change, caching, and database writing.
- The map-builder seam should have at least two adapters during migration: a native map builder as the default adapter and an external SoulForge builder as an explicit compatibility adapter. This makes the seam real rather than hypothetical.
- The map reader should remain compatible with existing SoulForge-generated SQLite maps during the first migration phase. The reader can later be renamed from SoulForge-specific language to repo-map language after native parity is proven.
- The target checkout module remains the sole owner of source-vs-analysis checkout safety. Native map generation must receive only the selected analysis checkout and must write only inside that checkout.
- The packet orchestrator remains the public workflow interface for bootstrap, CLI, benchmark, and plugin use. The native engine should deepen the map-building implementation without forcing callers to understand language parsing or graph construction.
- The ranking module should consume map facts rather than parse source files directly. Ranking remains responsible for changed-file priority, production/test role, PageRank or centrality, graph neighbors, co-change partners, intent matches, task refresh boosts, and why-selected explanations.
- GitNexus remains separate from the native map engine. Native map data can guide target selection and first-pass impact hints, but GitNexus remains the verified execution-flow and blast-radius authority.
- Confidence should be modeled explicitly. Exact AST/import resolution, probable framework inference, and textual fallback references should be distinguishable in packet data and tests.
- The first native language analyzers should focus on JavaScript, TypeScript, and Python because they cover RepoForge and Property Partner OPS workflow risk. Lean support should be added where lean-code requires useful declaration/import mapping.
- Unsupported languages should still contribute file metadata, centrality, text/path relevance, and co-change signals. They must not receive fake high-confidence symbol summaries.
- The native engine should build import/reference edges before attempting ambitious call graph parity. Call graph data can be best effort and should only be asserted where deterministic.
- Co-change should come from git history and exclude generated, vendor, and cache paths unless those files are explicitly changed.
- Map metadata should record builder name, builder version, source head, analysis head, build mode, build timestamp, and enough freshness data to set target-head verification truthfully.
- Existing packet field names can remain during migration for compatibility. A later cleanup can introduce neutral repo-map naming with backward-compatible aliases.
- The delivery should be split into staged PRs: builder interface and selection, native database writer and repository scanner, packet parity and ranking preservation, then release hardening and documentation.
- The implementation should avoid shallow modules with names like manager or service. Useful modules are those that create locality and leverage: map builder, map reader, language analyzer, graph builder, ranking policy, GitNexus bridge, and prompt renderer.

## Production Readiness Decisions

- Native map engine code should live outside the current single-file orchestrator. Use a dedicated map-builder package or equivalent module group rather than growing the orchestrator. The orchestrator may call the map-builder seam, but language parsing, graph construction, schema writing, and external fallback invocation belong behind that seam.
- The first implementation should preserve existing packet orchestration, ranking, GitNexus bridge, and prompt rendering surfaces unless a later slice deliberately deepens them. The initial deepening target is the map-builder seam, not a broad rewrite of every packet module.
- The map-builder Interface should be explicit before production code lands. Minimum shape:
  - input: analysis checkout path, map build mode, timeout, optional task intent, and source/analysis head metadata supplied by the orchestrator
  - output: map build result with built flag, builder name, builder version, command or native invocation details, return code when applicable, stdout/stderr when applicable, warning, metadata, and database path
  - invariant: native and external adapters return the same result shape
- Required map metadata fields: builder name, builder version, source head, analysis head, build mode, build timestamp, language coverage, confidence summary, source checkout path identity, analysis checkout path identity, and schema version.
- Builder-version freshness is part of the map freshness contract. A map built by an older native builder version or incompatible schema version must be treated as stale under automatic map build mode.
- Confidence should be stored with graph facts rather than inferred later. Import/reference/call/co-change records should distinguish exact AST resolution, probable framework or alias inference, textual fallback, and historical co-change. Unsupported-language facts default to file/path/co-change confidence and must not be promoted to exact symbol confidence.
- JavaScript, TypeScript, and Python are in the first production language slice. Lean is not a first-slice blocker unless a lean-code benchmark fixture proves the first release cannot be useful without declaration/import extraction. Until then, Lean support is a follow-up slice with file metadata, path relevance, and co-change fallback only.
- Unsupported-language behavior belongs in the repository walker and graph builder, not in shallow per-language adapters that simply return text fallback.
- Performance target: on a representative mixed JavaScript/TypeScript/Python repository of roughly Property Partner OPS size, warm-cache bootstrap should complete in under 3 seconds and cold-cache native map build plus packet generation should complete in under 15 seconds on the maintained development machines. If the target cannot be met, release hardening must record the bottleneck and decide whether to cache, defer language work, or adjust the target.
- Production implementation starts with a tracer-bullet test through the public bootstrap or packet interface. Do not start by implementing internal parser classes and then writing tests around their shape.
- Triage state after these decisions are recorded: category `enhancement`; state `ready-for-human` until the issue is published and a maintainer confirms the first implementation slice. It can move to `ready-for-agent` once the issue tracker brief includes the MapBuilder Interface, first red test, no-change surfaces, and benchmark expectations.

## Testing Decisions

- Tests should verify external behavior through stable interfaces: bootstrap output, packet contents, map build results, map reader behavior, ranking outcomes, source checkout cleanliness, and GitNexus gating. Tests should not lock in private helper structure.
- Native map-builder tests should prove that a map can be produced when SoulForge and Bun are absent from PATH.
- First red test: native automatic map build works without SoulForge or Bun. Run bootstrap or packet generation against a temporary git repository with `PATH` restricted so `soulforge` and `bun` are unavailable. The expected behavior is a fresh packet backed by the native builder, target-head verification set to true, builder name set to native, and no SoulForge command recorded in native builder metadata. This test must fail today because the current build path depends on SoulForge.
- Builder selection tests should cover automatic native build, forced rebuild, never-build behavior, explicit external fallback, failed native build, failed external fallback, and stale existing map handling.
- Map metadata tests should prove target-head verification is true only when map metadata matches the analysis checkout head.
- Builder-version freshness tests should prove automatic mode rejects maps built by an older native builder version or incompatible schema version.
- Database schema tests should prove native-generated maps are readable through the same reader contract as legacy SoulForge-generated maps.
- Source cleanliness tests should run for PR, local, intent, and repo modes and prove that source checkouts are not left with map directories, generated cache files, or ignore-rule edits.
- Repository walking tests should prove generated, vendor, dependency, and tool-cache paths are excluded unless explicitly changed.
- JavaScript and TypeScript analyzer tests should cover imports, exports, re-exports, path aliases, index files, barrel files, classes, functions, hooks, tests, and route-like declarations where applicable.
- Python analyzer tests should cover modules, packages, imports, functions, classes, methods, decorators, and test files.
- Lean analyzer tests should cover the subset required by lean-code, beginning with declarations and imports.
- Graph builder tests should cover file edges, reverse dependents, centrality, co-change, and confidence labeling.
- Ranking tests should prove changed production files outrank broad tests unless the change is test-only.
- Ranking tests should prove direct graph neighbors outrank co-change partners, and co-change partners outrank broad ambient PageRank-only files.
- Intent-mode tests should prove task terms can surface likely files in a clean checkout without dirty files.
- Packet parity tests should run in repo, intent, local, and PR modes and compare required packet sections rather than exact incidental formatting.
- GitNexus-off and GitNexus-auto tests should prove native map changes do not alter the rule that verified blast-radius claims require fresh GitNexus evidence.
- Benchmark tests should use Property Partner OPS, RepoForge, and lean-code scenarios to measure whether native packets match or beat current SoulForge-backed packet usefulness.
- Release smoke tests should reinstall the plugin, start a fresh Codex session, and verify RepoForge injects a packet without SoulForge or Bun available.

## Draft Issue Tracker Text

> *This was generated by AI during triage.*

### Title

Native repo map engine for RepoForge

### Category And State

- Category: `enhancement`
- State: `ready-for-human` before publication; promote to `ready-for-agent` after maintainer confirms the first implementation slice and issue labels are available.

### Summary

RepoForge currently depends on an external SoulForge binary to produce the map database used for target selection and packet context. Build a native RepoForge map engine that becomes the default builder, writes a database readable through the current map reader contract, preserves clean checkout isolation, and keeps SoulForge only as an explicit compatibility adapter.

### Agent Brief

**Category:** enhancement
**Summary:** Add a native RepoForge map engine so RepoForge no longer requires external SoulForge or Bun in the default bootstrap path.

**Current behavior:**
RepoForge shells out to SoulForge to build the repository map. When SoulForge or Bun is missing, bootstrap can only fall back to lower-quality no-map behavior or emit warnings. This creates machine-specific setup drift and blocks standalone RepoForge use.

**Desired behavior:**
RepoForge should build a fresh native map from the cache-owned analysis checkout in automatic map build mode. The map should include enough file, symbol, import/reference, co-change, centrality, metadata, and confidence data to preserve or improve packet target ranking. Legacy SoulForge maps should remain readable, and SoulForge should remain available only as an explicit fallback adapter.

**Key interfaces:**
- Map builder Interface: accepts analysis checkout, build mode, timeout, optional intent, and head metadata; returns a map build result with builder identity, version, metadata, warnings, command/native invocation details, and map database path.
- Map reader Interface: continues to read existing map database tables while native writer compatibility is proven.
- Bootstrap and packet Interface: continue to expose the same production workflow behavior for PR, local, intent, and repo modes.
- GitNexus bridge: remains the verified blast-radius authority and must not be weakened by native map work.

**Acceptance criteria:**
- [ ] Automatic map build can produce a fresh native map when SoulForge and Bun are absent from PATH.
- [ ] The generated packet records native builder metadata and verifies that the map matches the analysis checkout head.
- [ ] Builder-version or schema-version mismatch makes an existing map stale under automatic mode.
- [ ] Legacy SoulForge-generated maps remain readable through the compatibility reader.
- [ ] Source checkouts remain unchanged in PR, local, intent, and repo modes.
- [ ] Generated, vendor, dependency, and tool-cache paths are excluded unless explicitly changed.
- [ ] JavaScript, TypeScript, and Python receive deterministic first-slice symbol/import extraction.
- [ ] Unsupported languages degrade to file/path/co-change signals without fake high-confidence symbol data.
- [ ] GitNexus freshness and required-check semantics remain unchanged.
- [ ] Property Partner OPS, RepoForge, and lean-code benchmark fixtures show native packet quality matching or beating the existing SoulForge-backed baseline.

**Out of scope:**
- Replacing GitNexus.
- Full SoulForge UI/runtime parity.
- Requiring Bun or vendored SoulForge dependencies.
- Perfect call graph extraction for dynamic language patterns in the first release.
- Broad prompt renderer, ranking, or GitNexus bridge rewrites not required by the native map-builder seam.

## Out of Scope

- Rewriting RepoForge as a separate service.
- Replacing GitNexus.
- Weakening GitNexus freshness or required-check behavior.
- Full SoulForge UI, chat, memory, provider, rendering, or interactive runtime parity.
- Requiring Bun or vendored SoulForge dependencies for production RepoForge.
- Live LLM semantic summary generation during routine bootstrap.
- Changing Codex plugin marketplace mechanics beyond what is needed to expose the standalone behavior.
- Broad transaction, lease, replay, finalize, or persistent mutation-system changes.
- Perfect call graph extraction for dynamic language patterns in the first release.
- Full language parity across every language SoulForge or GitNexus may support.

## Further Notes

- Zoomed-out module map: Codex loads the RepoForge skill, the bootstrap adapter selects the mode, the packet orchestrator resolves a cache-owned analysis checkout, the map builder produces or reuses a fresh map, the map reader exposes map facts, ranking selects targets and coverage, the GitNexus bridge checks exact-head freshness and required graph checks, and the prompt renderer emits the packet.
- Main deepening opportunity: replace the shallow external process map-builder adapter with a real map-builder seam and a native default adapter. This improves locality because map-generation complexity lives behind one interface, and it improves leverage because callers continue to ask for a packet rather than learning parsing, graph, and cache rules.
- Secondary deepening opportunity: keep the map reader as the compatibility seam until native maps are proven, then rename it toward neutral repo-map language.
- The issue tracker could not be published from this checkout because no repository remote is configured. When this PRD is published to the tracker, apply the `needs-triage` label so it enters the normal triage flow.
