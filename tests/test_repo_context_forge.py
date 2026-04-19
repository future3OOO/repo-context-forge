from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "repo_context_forge.py"
SPEC = importlib.util.spec_from_file_location("repo_context_forge", MODULE_PATH)
assert SPEC is not None
repo_context_forge = importlib.util.module_from_spec(SPEC)
sys.modules["repo_context_forge"] = repo_context_forge
assert SPEC.loader is not None
SPEC.loader.exec_module(repo_context_forge)


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


if __name__ == "__main__":
    unittest.main()
