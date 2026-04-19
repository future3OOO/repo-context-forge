from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "repo_context_forge.py"
ROOT = MODULE_PATH.parents[0]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


repo_context_forge = load_module("repo_context_forge", MODULE_PATH)
codex_context_bootstrap = load_module(
    "codex_context_bootstrap",
    ROOT / "scripts" / "codex_context_bootstrap.py",
)
install_local_plugin = load_module(
    "install_local_plugin",
    ROOT / "scripts" / "install_local_plugin.py",
)


class RepoContextForgeTests(unittest.TestCase):
    def test_unique_sorted_deduplicates_and_sorts(self) -> None:
        self.assertEqual(
            repo_context_forge.unique_sorted(["b.py", "a.py", "b.py", ""]),
            ["a.py", "b.py"],
        )

    def test_selected_files_scope_pr(self) -> None:
        state = repo_context_forge.GitState(
            branch="main",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=["src/a.py"],
            staged_files=["docs/readme.md"],
            unstaged_files=["src/b.py"],
            untracked_files=["notes.md"],
        )

        self.assertEqual(repo_context_forge.selected_files(state, "pr"), ["src/a.py"])

    def test_selected_files_scope_all_deduplicates(self) -> None:
        state = repo_context_forge.GitState(
            branch="main",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=["src/a.py"],
            staged_files=["src/a.py", "docs/readme.md"],
            unstaged_files=["src/b.py"],
            untracked_files=["notes.md"],
        )

        self.assertEqual(
            repo_context_forge.selected_files(state, "all"),
            ["docs/readme.md", "notes.md", "src/a.py", "src/b.py"],
        )

    def test_build_gitnexus_plan_prefers_callable_symbols(self) -> None:
        plan = repo_context_forge.build_gitnexus_plan(
            [
                {
                    "path": "src/a.py",
                    "symbols": [
                        {"name": "VALUE", "kind": "constant"},
                        {"name": "handle", "kind": "function"},
                    ],
                }
            ]
        )

        self.assertEqual(
            plan,
            [
                {"kind": "symbol_context", "target": "handle", "file": "src/a.py"},
                {"kind": "symbol_impact", "target": "handle", "direction": "upstream"},
            ],
        )

    def test_rank_target_entry_prioritizes_changed_production_over_broad_test(self) -> None:
        state = repo_context_forge.GitState(
            branch="feature",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=[
                "workers/browser-automation/browser_automation_worker/cli.py",
                "tests/python/test_browser_automation_worker.py",
            ],
            staged_files=[],
            unstaged_files=[],
            untracked_files=[],
        )
        production = repo_context_forge.rank_target_entry(
            {
                "path": "workers/browser-automation/browser_automation_worker/cli.py",
                "changed_symbols": [{"name": "_capture", "kind": "function", "line": 10, "end_line": 20}],
                "cochanges": [],
                "graph_neighbors": [],
                "pagerank": 0.01,
            },
            state,
            mode="pr",
        )
        broad_test = repo_context_forge.rank_target_entry(
            {
                "path": "tests/python/test_browser_automation_worker.py",
                "changed_symbols": [{"name": "BrowserAutomationWorkerTests", "kind": "class", "line": 1, "end_line": 5000}],
                "cochanges": [],
                "graph_neighbors": [],
                "pagerank": 0.8,
            },
            state,
            mode="pr",
        )

        self.assertGreater(production["priority_score"], broad_test["priority_score"])
        self.assertIn("production_file", production["rank_signals"])
        self.assertIn("broad_test_container", broad_test["rank_signals"])

    def test_pr_ranking_does_not_boost_source_dirty_overlap(self) -> None:
        state = repo_context_forge.GitState(
            branch="feature",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=[],
            staged_files=["src/dirty.py"],
            unstaged_files=[],
            untracked_files=[],
        )

        entry = repo_context_forge.rank_target_entry(
            {
                "path": "src/dirty.py",
                "changed_symbols": [],
                "cochanges": [],
                "graph_neighbors": [],
                "pagerank": 0.01,
            },
            state,
            mode="pr",
        )

        self.assertNotIn("dirty_file", entry["rank_signals"])

    def test_task_state_boosts_edited_files(self) -> None:
        entries = [
            {
                "path": "src/a.py",
                "surface_role": "production",
                "rank": 2,
                "priority_score": 10,
                "rank_signals": [],
                "why_selected": [],
            },
            {
                "path": "src/b.py",
                "surface_role": "production",
                "rank": 1,
                "priority_score": 20,
                "rank_signals": [],
                "why_selected": [],
            },
        ]
        state = {"schema_version": 1, "edited_files": ["src/a.py"]}

        ranked = repo_context_forge.apply_task_state_to_entries(entries, state)

        self.assertEqual(ranked[0]["path"], "src/a.py")
        self.assertIn("edited_file", ranked[0]["rank_signals"])

    def test_record_task_event_deduplicates_paths(self) -> None:
        state = {"schema_version": 1, "read_files": ["src/a.py"]}

        repo_context_forge.record_task_event(state, "read", ["src/a.py", "src/b.py"])

        self.assertEqual(state["read_files"], ["src/a.py", "src/b.py"])

    def test_gitnexus_findings_block_stale_blast_radius_claims(self) -> None:
        packet = {
            "warnings": [],
            "targets": [
                {
                    "path": "src/a.py",
                    "rank_signals": [],
                    "why_selected": [],
                }
            ],
            "gitnexus": {"status": "planned"},
        }

        merged = repo_context_forge.apply_gitnexus_findings(
            packet,
            {
                "stale_index": True,
                "confirmed_files": ["src/a.py"],
                "impacted_files": ["src/missing.py"],
            },
        )

        self.assertEqual(merged["gitnexus"]["status"], "blocked")
        self.assertIn("gitnexus_confirmed", merged["targets"][0]["rank_signals"])
        self.assertIn("GitNexus index is stale; blast-radius claims are blocked", merged["warnings"])
        self.assertEqual(merged["gitnexus"]["absent_impacted_files"], ["src/missing.py"])

    def test_parse_unified_diff_new_ranges(self) -> None:
        diff = "\n".join(
            [
                "@@ -10,0 +11,4 @@ def old():",
                "@@ -20 +30 @@ class Thing:",
                "@@ -40,2 +0,0 @@",
            ]
        )

        self.assertEqual(
            repo_context_forge.parse_unified_diff_new_ranges(diff),
            [(11, 14), (30, 30), (0, 0)],
        )

    def test_symbol_overlaps_changed_range(self) -> None:
        symbol = repo_context_forge.Symbol(
            name="handle",
            kind="function",
            line=10,
            end_line=20,
            signature=None,
            is_exported=True,
        )

        self.assertFalse(repo_context_forge.symbol_overlaps(symbol, [(1, 9)]))
        self.assertTrue(repo_context_forge.symbol_overlaps(symbol, [(15, 25)]))

    def test_remove_nested_symbols_prefers_outer_function(self) -> None:
        outer = repo_context_forge.Symbol(
            name="_extract",
            kind="function",
            line=10,
            end_line=30,
            signature=None,
            is_exported=False,
        )
        inner = repo_context_forge.Symbol(
            name="visit",
            kind="function",
            line=15,
            end_line=20,
            signature=None,
            is_exported=True,
        )

        self.assertEqual(repo_context_forge.remove_nested_symbols([inner, outer]), [outer])

    def test_compute_token_budget_respects_explicit_value(self) -> None:
        self.assertEqual(repo_context_forge.compute_token_budget(100_000, 1234), 1234)

    def test_compute_token_budget_decays_for_long_context(self) -> None:
        early = repo_context_forge.compute_token_budget(0, None)
        late = repo_context_forge.compute_token_budget(100_000, None)

        self.assertGreater(early, late)
        self.assertGreaterEqual(late, repo_context_forge.MIN_TOKEN_BUDGET)

    def test_tokenize_intent_filters_common_words(self) -> None:
        self.assertEqual(
            repo_context_forge.tokenize_intent("Update the Gmail draft lifecycle"),
            ["update", "gmail", "draft", "lifecycle"],
        )

    def test_dirty_paths_can_ignore_tool_cache(self) -> None:
        self.assertEqual(
            repo_context_forge.filter_tool_cache_dirty_paths(
                [".soulforge/repomap.db", ".soulforge", "src/app.py"]
            ),
            ["src/app.py"],
        )

    def test_parse_porcelain_paths_preserves_hidden_file_dot(self) -> None:
        self.assertEqual(
            repo_context_forge.parse_porcelain_paths(" M .gitignore\n?? .soulforge/repomap.db\n"),
            [".gitignore", ".soulforge/repomap.db"],
        )

    def test_blocker_prompt_renders_worktree_suggestions(self) -> None:
        packet = {
            "schema_version": 1,
            "blocked": True,
            "blocker": {
                "reason": "detached checkout has no target surface",
                "worktree_suggestions": [
                    {"path": "/repo/worktree", "branch": "feature", "head": "abc123"}
                ],
            },
            "mode": "blocked",
            "target_state": {
                "source_repo": "/repo",
                "analysis_repo": "/repo",
                "base_ref": "main",
                "head_ref": "HEAD",
                "head_sha": "abc123",
                "source_dirty": False,
                "target_dirty": False,
            },
            "targets": [],
            "warnings": ["detached checkout has no target surface"],
            "gitnexus": {"plan": []},
        }

        rendered = repo_context_forge.render_prompt(packet)

        self.assertIn("<blocker", rendered)
        self.assertIn("/repo/worktree", rendered)

    def test_bootstrap_blocks_detached_empty_checkout(self) -> None:
        state = repo_context_forge.GitState(
            branch="(detached)",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=[],
            staged_files=[],
            unstaged_files=[],
            untracked_files=[],
        )
        original_read_git_state = codex_context_bootstrap.forge.read_git_state
        original_is_detached = codex_context_bootstrap.forge.is_detached
        codex_context_bootstrap.forge.read_git_state = lambda *_args, **_kwargs: state
        codex_context_bootstrap.forge.is_detached = lambda _repo: True
        try:
            reason = codex_context_bootstrap.should_block_empty_checkout(
                Path("/repo"),
                "local",
                "origin/main",
                "HEAD",
                None,
            )
        finally:
            codex_context_bootstrap.forge.read_git_state = original_read_git_state
            codex_context_bootstrap.forge.is_detached = original_is_detached

        self.assertEqual(
            reason,
            "detached checkout has no target surface; select the active PR worktree",
        )

    def test_cache_key_is_stable(self) -> None:
        key = repo_context_forge.cache_key_for(Path("/tmp/example"), "abc123")

        self.assertEqual(key, repo_context_forge.cache_key_for(Path("/tmp/example"), "abc123"))
        self.assertNotEqual(key, repo_context_forge.cache_key_for(Path("/tmp/example"), "def456"))

    def test_render_prompt_includes_required_checks(self) -> None:
        packet = {
            "schema_version": 1,
            "mode": "pr",
            "warnings": ["source worktree is dirty"],
            "target_state": {
                "source_repo": "/repo",
                "analysis_repo": "/cache/repo",
                "base_ref": "main",
                "head_ref": "HEAD",
                "head_sha": "abc123",
                "source_dirty": True,
                "target_dirty": False,
            },
            "targets": [
                {
                    "path": "src/a.py",
                    "rank": 1,
                    "changed_symbols": [
                        {
                            "name": "handle",
                            "kind": "function",
                            "line": 10,
                            "end_line": 20,
                            "signature": "def handle() -> None",
                        }
                    ],
                }
            ],
            "gitnexus": {
                "plan": [
                    {"kind": "symbol_context", "target": "handle", "file": "src/a.py"}
                ]
            },
        }

        rendered = repo_context_forge.render_prompt(packet)

        self.assertIn("<repo_context_packet", rendered)
        self.assertIn('path="src/a.py"', rendered)
        self.assertIn('target="handle"', rendered)

    def test_wrapper_env_exposes_packet_and_target_metadata(self) -> None:
        packet = {
            "mode": "pr",
            "repo": "/repo",
            "target_state": {
                "source_repo": "/repo",
                "analysis_repo": "/cache/repo",
                "head_sha": "abc123",
            },
            "gitnexus": {"repo": "example"},
        }

        env = repo_context_forge.wrapper_env(packet, Path("/tmp/packet.xml"))

        self.assertEqual(env["REPO_CONTEXT_FORGE_PACKET_FILE"], "/tmp/packet.xml")
        self.assertEqual(env["REPO_CONTEXT_FORGE_ANALYSIS_REPO"], "/cache/repo")
        self.assertEqual(env["REPO_CONTEXT_FORGE_TARGET_SHA"], "abc123")
        self.assertEqual(env["REPO_CONTEXT_FORGE_GITNEXUS_REPO"], "example")

    def test_plugin_manifest_points_to_existing_skill_root(self) -> None:
        manifest_path = ROOT / ".codex-plugin" / "plugin.json"
        manifest = repo_context_forge.json.loads(manifest_path.read_text())

        self.assertEqual(manifest["name"], "repo-context-forge")
        self.assertTrue((ROOT / manifest["skills"]).resolve().is_dir())

    def test_bootstrap_auto_mode_prefers_intent_when_clean_without_base(self) -> None:
        original_is_dirty = codex_context_bootstrap.forge.is_dirty
        codex_context_bootstrap.forge.is_dirty = lambda _repo: False
        try:
            mode = codex_context_bootstrap.choose_mode(
                Path("/repo"),
                "auto",
                None,
                "HEAD",
                "add draft preservation",
            )
        finally:
            codex_context_bootstrap.forge.is_dirty = original_is_dirty

        self.assertEqual(mode, "intent")

    def test_install_plugin_entry_is_installed_by_default(self) -> None:
        entry = install_local_plugin.plugin_entry()

        self.assertEqual(entry["name"], "repo-context-forge")
        self.assertEqual(entry["source"]["path"], "./plugins/repo-context-forge")
        self.assertEqual(entry["policy"]["installation"], "INSTALLED_BY_DEFAULT")


if __name__ == "__main__":
    unittest.main()
