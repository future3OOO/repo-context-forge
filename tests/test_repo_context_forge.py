from __future__ import annotations

import fcntl
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from collections import namedtuple
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


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


class TrackedConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name: str):
        if name == "connection" and "connection" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.connection, name)

    def close(self) -> None:
        self.closed = True
        self.connection.close()


class RepoContextForgeTests(unittest.TestCase):
    def make_git_repo(self, root: Path) -> None:
        repo_context_forge.run_cmd(["git", "init"], cwd=root)
        repo_context_forge.run_cmd(["git", "config", "user.email", "test@example.com"], cwd=root)
        repo_context_forge.run_cmd(["git", "config", "user.name", "Test User"], cwd=root)
        (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("print('clean')\n", encoding="utf-8")
        repo_context_forge.run_cmd(["git", "add", ".gitignore", "src/a.py"], cwd=root)
        repo_context_forge.run_cmd(["git", "commit", "-m", "initial"], cwd=root)

    def ensure_gitnexus_index(
        self,
        state: repo_context_forge.TargetState,
        repo_name: str,
        mode: str,
        **kwargs,
    ) -> dict[str, object]:
        return repo_context_forge.gitnexus_analysis.ensure_index(
            state.analysis_repo,
            state.head_sha,
            repo_name,
            mode,
            run_command=repo_context_forge.run_cmd,
            **kwargs,
        )

    def execute_gitnexus_plan(
        self,
        plan: list[dict[str, str]],
        status: dict[str, object],
        **kwargs,
    ) -> dict[str, object]:
        return repo_context_forge.gitnexus_analysis.execute(
            plan,
            status,
            run_command=repo_context_forge.run_cmd,
            **kwargs,
        )

    def build_workflow_index(
        self, repo: Path, source: str | None = None
    ) -> repo_context_forge.workflow_index.WorkflowIndex:
        if source is not None:
            (repo / "src" / "a.py").write_text(source, encoding="utf-8")
        native_index = repo_context_forge.workflow_index.WorkflowIndex(
            repo, repo_context_forge.file_role
        )
        native_index.build(
            repo_context_forge.source_worktree_files(repo),
            head_sha=repo_context_forge.run_git(repo, ["rev-parse", "HEAD"]),
            summary_for_symbol=repo_context_forge.synthetic_symbol_summary,
        )
        return native_index

    def test_run_cmd_can_suppress_core_dump_payloads(self) -> None:
        result = repo_context_forge.run_cmd(
            ["cat", "/proc/self/coredump_filter"],
            suppress_core_dump=True,
        )

        self.assertEqual(result.stdout.strip(), "00000000")

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
        plan, omitted_checks = repo_context_forge.build_gitnexus_plan(
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
                {
                    "kind": "symbol_impact",
                    "target": "handle",
                    "file": "src/a.py",
                    "direction": "upstream",
                },
            ],
        )
        self.assertEqual(omitted_checks, 0)

    def test_build_gitnexus_plan_never_splits_context_impact_pair_at_cap(self) -> None:
        entries = [{"path": "README.md", "symbols": []}]
        entries.extend(
            {
                "path": f"src/module_{index}.py",
                "symbols": [{"name": f"handle_{index}", "kind": "function"}],
            }
            for index in range(10)
        )

        plan, omitted_checks = repo_context_forge.build_gitnexus_plan(entries)

        self.assertLessEqual(len(plan), repo_context_forge.MAX_GITNEXUS_CHECKS)
        symbol_checks = {
            (item["file"], item["target"]): item["kind"]
            for item in plan
            if item["kind"] == "symbol_context"
        }
        impact_checks = {
            (item["file"], item["target"]): item["kind"]
            for item in plan
            if item["kind"] == "symbol_impact"
        }
        self.assertEqual(symbol_checks.keys(), impact_checks.keys())
        self.assertEqual(omitted_checks, 2)

        file_plan, omitted_file_checks = repo_context_forge.build_gitnexus_plan(
            [{"path": f"docs/{index}.md", "symbols": []} for index in range(21)]
        )
        self.assertEqual(len(file_plan), repo_context_forge.MAX_GITNEXUS_CHECKS)
        self.assertEqual(omitted_file_checks, 1)

    def test_public_bootstrap_keeps_duplicate_symbol_names_file_scoped(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
            packet_path = Path(runtime_home) / "packet.json"
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text(
                "def connect():\n    return 'a'\n", encoding="utf-8"
            )
            (repo / "src" / "b.py").write_text(
                "def connect():\n    return 'b'\n", encoding="utf-8"
            )
            (repo / "src" / "use.py").write_text(
                "from a import connect as connect_a\n"
                "from b import connect as connect_b\n\n"
                "def use():\n    return connect_a(), connect_b()\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "duplicate symbols"])

            result = repo_context_forge.run_cmd(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "codex_context_bootstrap.py"),
                    "--repo",
                    str(repo),
                    "--mode",
                    "repo",
                    "--top",
                    "3",
                    "--cache-dir",
                    cache_dir,
                    "--map-build",
                    "never",
                    "--gitnexus-mode",
                    "auto",
                    "--packet-json-out",
                    str(packet_path),
                ],
                env={**os.environ, "HOME": runtime_home},
                allow_fail=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            self.assertTrue(packet_path.exists())
            packet = repo_context_forge.json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertNotIn("blocked", packet)
            analysis = packet["gitnexus"]["analysis"]
            self.assertEqual(analysis["graph_call_count"], 6)
            self.assertEqual(analysis["process_count"], 6)
            self.assertLessEqual(
                analysis["output_bytes"],
                analysis["graph_call_count"] * repo_context_forge.gitnexus_analysis.MAX_OUTPUT_BYTES,
            )
            connect_entries = [
                entry for entry in analysis["entries"] if entry.get("target") == "connect"
            ]
            self.assertEqual(len(connect_entries), 4)
            self.assertEqual(
                {(entry["kind"], entry["file"], entry.get("direction", "")) for entry in connect_entries},
                {
                    ("symbol_context", "src/a.py", ""),
                    ("symbol_impact", "src/a.py", "upstream"),
                    ("symbol_context", "src/b.py", ""),
                    ("symbol_impact", "src/b.py", "upstream"),
                },
            )
            self.assertEqual(
                {entry["resolved_identity"] for entry in connect_entries if entry["kind"] == "symbol_context"},
                {"Function:src/a.py:connect", "Function:src/b.py:connect"},
            )
            self.assertEqual(
                [entry["status"] for entry in connect_entries],
                ["resolved"] * 4,
            )
            self.assertEqual(
                {entry["resolved_identity"] for entry in connect_entries},
                {"Function:src/a.py:connect", "Function:src/b.py:connect"},
            )
            self.assertNotIn("<blocker", result.stdout)
            self.assertIn(
                '<check kind="symbol_context" target="connect" file="src/a.py"',
                result.stdout,
            )
            self.assertIn(
                '<check kind="symbol_context" target="connect" file="src/b.py"',
                result.stdout,
            )
            self.assertIn(
                '<check kind="symbol_impact" target="connect" file="src/a.py" direction="upstream"',
                result.stdout,
            )
            self.assertIn(
                '<check kind="symbol_impact" target="connect" file="src/b.py" direction="upstream"',
                result.stdout,
            )

    def test_public_bootstrap_resolves_nested_file_context_by_exact_identity(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
            packet_path = Path(runtime_home) / "packet.json"
            self.make_git_repo(repo)
            (repo / "ARCHITECTURE.md").write_text("# Root architecture\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "ARCHITECTURE.md"])
            repo_context_forge.run_git(repo, ["commit", "-m", "root architecture"])
            (repo / "docs").mkdir()
            (repo / "docs" / "ARCHITECTURE.md").write_text(
                "# Nested architecture\n", encoding="utf-8"
            )
            repo_context_forge.run_git(repo, ["add", "docs/ARCHITECTURE.md"])
            repo_context_forge.run_git(repo, ["commit", "-m", "nested architecture"])

            result = repo_context_forge.run_cmd(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "codex_context_bootstrap.py"),
                    "--repo",
                    str(repo),
                    "--mode",
                    "pr",
                    "--base",
                    "HEAD~1",
                    "--top",
                    "1",
                    "--cache-dir",
                    cache_dir,
                    "--map-build",
                    "never",
                    "--gitnexus-mode",
                    "auto",
                    "--allow-stale-pr-head",
                    "--packet-json-out",
                    str(packet_path),
                ],
                env={**os.environ, "HOME": runtime_home},
                allow_fail=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            packet = repo_context_forge.json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertNotIn("blocked", packet)
            self.assertTrue(packet["gitnexus"]["required_checks_resolved"])
            entries = packet["gitnexus"]["analysis"]["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["kind"], "file_context")
            self.assertEqual(entries[0]["file"], "docs/ARCHITECTURE.md")
            self.assertEqual(entries[0]["resolved_identity"], "File:docs/ARCHITECTURE.md")

    def test_public_bootstrap_reports_exact_omitted_check_count_non_blocking(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
            packet_path = Path(runtime_home) / "packet.json"
            self.make_git_repo(repo)
            (repo / "src" / "many.py").write_text(
                "".join(
                    f"def handle_{index}():\n    return {index}\n\n" for index in range(12)
                ),
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "many callables"])

            result = repo_context_forge.run_cmd(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "codex_context_bootstrap.py"),
                    "--repo",
                    str(repo),
                    "--mode",
                    "repo",
                    "--top",
                    "1",
                    "--cache-dir",
                    cache_dir,
                    "--map-build",
                    "never",
                    "--gitnexus-mode",
                    "auto",
                    "--enforce-intake",
                    "--packet-json-out",
                    str(packet_path),
                ],
                env={**os.environ, "HOME": runtime_home},
                allow_fail=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            packet = repo_context_forge.json.loads(packet_path.read_text(encoding="utf-8"))
            analysis = packet["gitnexus"]["analysis"]
            self.assertEqual(len(analysis["entries"]), repo_context_forge.MAX_GITNEXUS_CHECKS)
            self.assertEqual(analysis["omitted_check_count"], 4)
            self.assertNotIn("plan_capacity_reached", analysis)
            self.assertTrue(packet["gitnexus"]["required_checks_resolved"])
            self.assertNotIn("blocked", packet)
            self.assertIn("omitted_checks=4; omissions_blocking=false", result.stdout)
            self.assertIn('omitted_checks="4" omissions_blocking="false"', result.stdout)

    def test_public_bootstrap_emits_resolved_graph_analysis(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
            packet_path = Path(runtime_home) / "packet.json"
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text(
                "def handle():\n    return 1\n", encoding="utf-8"
            )
            (repo / "src" / "use.py").write_text(
                "from a import handle\n\ndef use():\n    return handle()\n", encoding="utf-8"
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "callable dependency"])

            result = repo_context_forge.run_cmd(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "codex_context_bootstrap.py"),
                    "--repo",
                    str(repo),
                    "--mode",
                    "repo",
                    "--top",
                    "2",
                    "--cache-dir",
                    cache_dir,
                    "--map-build",
                    "never",
                    "--gitnexus-mode",
                    "auto",
                    "--packet-json-out",
                    str(packet_path),
                ],
                env={**os.environ, "HOME": runtime_home},
                allow_fail=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            packet = repo_context_forge.json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertNotIn("blocked", packet)
            self.assertEqual(packet["gitnexus"]["status"], "reindexed")
            self.assertTrue(packet["gitnexus"]["required_checks_resolved"])
            self.assertTrue(
                repo_context_forge.gitnexus_analysis.result_is_resolved(
                    packet["gitnexus"]["analysis"]
                )
            )
            self.assertGreater(packet["gitnexus"]["analysis"]["graph_call_count"], 0)
            self.assertIn('<gitnexus_analysis status="resolved"', result.stdout)

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

    def test_coverage_plan_groups_changed_production_and_verification(self) -> None:
        plan = repo_context_forge.build_coverage_plan(
            [
                {
                    "path": "src/service.py",
                    "surface_role": "production",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
                {
                    "path": "tests/test_service.py",
                    "surface_role": "test",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
            ]
        )

        area_ids = [area["id"] for area in plan["areas"]]
        self.assertTrue(plan["required"])
        self.assertFalse(plan["delegation_required"])
        self.assertIn("production_contract", area_ids)
        self.assertIn("verification_contract", area_ids)

    def test_coverage_plan_keeps_required_areas_in_parent_session(self) -> None:
        plan = repo_context_forge.build_coverage_plan(
            [
                {
                    "path": "src/service.py",
                    "surface_role": "production",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {
                        "risk": "medium",
                        "direct_dependents": 2,
                        "total_affected_scope": 5,
                    },
                },
                {
                    "path": "tests/test_service.py",
                    "surface_role": "test",
                    "rank_signals": ["changed_file", "broad_test_container"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
                {
                    "path": "README.md",
                    "surface_role": "production",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
                {
                    "path": "src/client.py",
                    "surface_role": "production",
                    "rank_signals": ["graph_neighbor", "cochange_partner"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
            ]
        )

        area_ids = [area["id"] for area in plan["areas"]]

        self.assertFalse(plan["delegation_required"])
        self.assertEqual(
            [
                "production_contract",
                "verification_contract",
                "operator_contract",
                "blast_radius",
                "related_map_surface",
            ],
            area_ids,
        )

    def test_coverage_plan_includes_all_matching_files_without_truncation(self) -> None:
        entries = [
            {
                "path": f"src/service_{index}.py",
                "surface_role": "production",
                "rank_signals": ["changed_file"],
                "soulforge_impact": {"risk": "low", "direct_dependents": 0},
            }
            for index in range(8)
        ]
        plan = repo_context_forge.build_coverage_plan(entries)

        production = next(area for area in plan["areas"] if area["id"] == "production_contract")
        self.assertEqual([entry["path"] for entry in entries], production["files"])

    def test_coverage_plan_includes_blast_radius_and_related_surfaces(self) -> None:
        plan = repo_context_forge.build_coverage_plan(
            [
                {
                    "path": "src/service.py",
                    "surface_role": "production",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {
                        "risk": "medium",
                        "direct_dependents": 4,
                        "total_affected_scope": 12,
                    },
                },
                {
                    "path": "src/client.py",
                    "surface_role": "production",
                    "rank_signals": ["graph_neighbor", "cochange_partner"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
            ]
        )

        area_ids = [area["id"] for area in plan["areas"]]
        self.assertIn("blast_radius", area_ids)
        self.assertIn("related_map_surface", area_ids)

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

    def test_repo_mode_selects_top_soulforge_files(self) -> None:
        state = repo_context_forge.GitState(
            branch="main",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=[],
            staged_files=[],
            unstaged_files=[],
            untracked_files=[],
        )

        class FakeMap:
            def top_files(self, _limit: int):
                return [
                    repo_context_forge.MapFile("src/app.py", 0.5, 3, 20, 1),
                    repo_context_forge.MapFile(".soulforge/repomap.db", 0.4, 0, 1, 2),
                    repo_context_forge.MapFile("src/worker.py", 0.3, 2, 10, 3),
                ]

        self.assertEqual(
            repo_context_forge.target_files_for_mode("repo", state, FakeMap(), None, 3),
            ["src/app.py", "src/worker.py"],
        )

    def test_repo_mode_excludes_reference_only_agent_paths(self) -> None:
        state = repo_context_forge.GitState(
            branch="main",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=[],
            staged_files=[],
            unstaged_files=[],
            untracked_files=[],
        )

        class FakeMap:
            def top_files(self, _limit: int):
                return [
                    repo_context_forge.MapFile("plugin-baseline/a.py", 0.9, 1, 10, 1),
                    repo_context_forge.MapFile("runtime/a.py", 0.8, 1, 10, 2),
                    repo_context_forge.MapFile("src/app.py", 0.7, 1, 10, 3),
                ]

        self.assertEqual(
            repo_context_forge.target_files_for_mode(
                "repo",
                state,
                FakeMap(),
                None,
                3,
                ["plugin-baseline/"],
            ),
            ["runtime/a.py", "src/app.py"],
        )

    def test_pr_mode_keeps_changed_reference_only_files_but_filters_related(self) -> None:
        state = repo_context_forge.GitState(
            branch="feature",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=["plugin-baseline/a.py"],
            staged_files=[],
            unstaged_files=[],
            untracked_files=[],
        )

        class FakeMap:
            def related_files_for_paths(self, _paths, _limit: int):
                return ["plugin-baseline/b.py", "runtime/a.py"]

        self.assertEqual(
            repo_context_forge.target_files_for_mode(
                "pr",
                state,
                FakeMap(),
                None,
                3,
                ["plugin-baseline/"],
            ),
            ["plugin-baseline/a.py", "runtime/a.py"],
        )

    def test_reference_only_prefixes_are_read_from_agents(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            (repo / "AGENTS.md").write_text(
                "- `plugin-baseline/` is reference-only unless approved.\n"
                "- `runtime/` is active.\n",
                encoding="utf-8",
            )

            self.assertEqual(
                repo_context_forge.reference_only_prefixes(repo),
                ["plugin-baseline/"],
            )

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

    def test_compute_token_budget_expands_for_long_context(self) -> None:
        early = repo_context_forge.compute_token_budget(0, None)
        late = repo_context_forge.compute_token_budget(100_000, None)

        self.assertEqual(early, repo_context_forge.DEFAULT_TOKEN_BUDGET)
        self.assertGreater(late, early)
        self.assertGreaterEqual(late, repo_context_forge.MIN_TOKEN_BUDGET)
        self.assertLessEqual(late, repo_context_forge.MAX_TOKEN_BUDGET)

    def test_tokenize_intent_filters_common_words(self) -> None:
        self.assertEqual(
            repo_context_forge.tokenize_intent("Update the Gmail draft lifecycle"),
            ["update", "gmail", "draft", "lifecycle"],
        )

    def test_dirty_paths_can_ignore_tool_cache(self) -> None:
        self.assertEqual(
            repo_context_forge.filter_tool_cache_dirty_paths(
                [
                    ".soulforge/repomap.db",
                    ".soulforge",
                    ".codex",
                    ".gitnexus/meta.json",
                    ".repo-context-forge/workflow-index.sqlite3",
                    "src/app.py",
                ]
            ),
            ["src/app.py"],
        )

    def test_parse_porcelain_paths_preserves_hidden_file_dot(self) -> None:
        self.assertEqual(
            repo_context_forge.parse_porcelain_paths(" M .gitignore\n?? .soulforge/repomap.db\n"),
            [".gitignore", ".soulforge/repomap.db"],
        )

    def test_local_analysis_worktree_overlays_dirty_files_without_tool_cache(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text("print('dirty')\n", encoding="utf-8")
            (repo / "src" / "b.py").write_text("print('untracked')\n", encoding="utf-8")
            (repo / ".soulforge").mkdir()
            (repo / ".soulforge" / "repomap.db").write_text("cache", encoding="utf-8")

            state = repo_context_forge.ensure_local_analysis_worktree(
                repo,
                "HEAD",
                Path(cache_dir),
            )

            self.assertNotEqual(state.analysis_repo, repo)
            self.assertEqual(
                (state.analysis_repo / "src" / "a.py").read_text(encoding="utf-8"),
                "print('dirty')\n",
            )
            self.assertEqual(
                (state.analysis_repo / "src" / "b.py").read_text(encoding="utf-8"),
                "print('untracked')\n",
            )
            self.assertFalse((state.analysis_repo / ".soulforge" / "repomap.db").exists())
            self.assertTrue(state.source_dirty)
            self.assertTrue(state.target_dirty)
            worktrees = repo_context_forge.run_git(repo, ["worktree", "list", "--porcelain"])
            self.assertNotIn(str(state.analysis_repo), worktrees)
            common_dir = repo_context_forge.run_git(
                state.analysis_repo,
                ["rev-parse", "--git-common-dir"],
            )
            self.assertEqual(common_dir, ".git")

    def test_pr_analysis_reuses_cache_after_generated_agent_files(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            cache = Path(cache_dir)
            first = repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)
            generated = first.analysis_repo / ".claude" / "skills" / "gitnexus"
            generated.mkdir(parents=True)
            (generated / "SKILL.md").write_text("generated\n", encoding="utf-8")

            second = repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)

            self.assertEqual(second.analysis_repo, first.analysis_repo)
            self.assertFalse((second.analysis_repo / ".claude").exists())
            self.assertFalse(second.target_dirty)
            self.assertFalse(repo_context_forge.is_dirty(repo, ignore_tool_cache=True))

    def test_pr_analysis_recovers_when_reused_cache_preclean_fails(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            cache = Path(cache_dir)
            first = repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)
            real_run_git = repo_context_forge.run_git
            reset_attempts = 0

            def fail_first_reset(target, args, *, allow_fail=False):
                nonlocal reset_attempts
                if target == first.analysis_repo and args == ["reset", "--hard", "HEAD"]:
                    reset_attempts += 1
                    if reset_attempts == 1:
                        if allow_fail:
                            return ""
                        raise RuntimeError("interrupted reset")
                return real_run_git(target, args, allow_fail=allow_fail)

            with patch.object(repo_context_forge, "run_git", side_effect=fail_first_reset):
                second = repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)

            self.assertEqual(second.analysis_repo, first.analysis_repo)
            self.assertEqual(reset_attempts, 2)
            self.assertFalse(second.target_dirty)

    def test_make_packet_reuses_clean_exact_head_workflow_index(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            arguments = {
                "mode": "repo",
                "base_ref": "HEAD",
                "head_ref": "HEAD",
                "intent": None,
                "top": 5,
                "token_budget": repo_context_forge.DEFAULT_TOKEN_BUDGET,
                "cache_dir": Path(cache_dir),
                "soulforge_bin": None,
                "map_build": "never",
                "map_timeout_ms": 1,
                "allow_missing_map": True,
                "gitnexus_repo": None,
            }

            original_build = repo_context_forge.workflow_index.WorkflowIndex.build
            builds = 0

            def count_builds(index, *args, **kwargs):
                nonlocal builds
                builds += 1
                return original_build(index, *args, **kwargs)

            with patch.object(
                repo_context_forge.workflow_index.WorkflowIndex,
                "build",
                new=count_builds,
            ):
                first = repo_context_forge.make_packet(repo, **arguments)
                second = repo_context_forge.make_packet(repo, **arguments)
            index_path = Path(str(first["workflow_index"]["db_path"]))

            self.assertEqual(Path(str(second["workflow_index"]["db_path"])), index_path)
            self.assertEqual(builds, 1)

    def test_make_packet_rebuilds_invalid_cached_workflow_index(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            arguments = {
                "mode": "repo", "base_ref": "HEAD", "head_ref": "HEAD",
                "intent": None, "top": 5,
                "token_budget": repo_context_forge.DEFAULT_TOKEN_BUDGET,
                "cache_dir": Path(cache_dir), "soulforge_bin": None,
                "map_build": "never", "map_timeout_ms": 1,
                "allow_missing_map": True, "gitnexus_repo": None,
            }
            first = repo_context_forge.make_packet(repo, **arguments)
            index_path = Path(str(first["workflow_index"]["db_path"]))
            with closing(sqlite3.connect(index_path)) as connection, connection:
                connection.execute(
                    "UPDATE metadata SET value = 'stale' WHERE key = 'head_sha'"
                )

            second = repo_context_forge.make_packet(repo, **arguments)

            self.assertEqual(
                second["workflow_index"]["head_sha"],
                repo_context_forge.run_git(repo, ["rev-parse", "HEAD"]),
            )

    def test_make_packet_rebuilds_repeated_dirty_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text("print('dirty one')\n", encoding="utf-8")
            arguments = {
                "mode": "local", "base_ref": "HEAD", "head_ref": "HEAD",
                "intent": None, "top": 5,
                "token_budget": repo_context_forge.DEFAULT_TOKEN_BUDGET,
                "cache_dir": Path(cache_dir), "soulforge_bin": None,
                "map_build": "never", "map_timeout_ms": 1,
                "allow_missing_map": True, "gitnexus_repo": None,
            }
            repo_context_forge.make_packet(repo, **arguments)
            (repo / "src" / "a.py").write_text("print('dirty two')\n", encoding="utf-8")

            second = repo_context_forge.make_packet(repo, **arguments)

            self.assertTrue(second["workflow_index"]["dirty_overlay"])

    def test_make_packet_rejects_non_cache_owned_analysis_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            state = repo_context_forge.TargetState(
                mode="repo", source_repo=repo, analysis_repo=repo,
                base_ref="HEAD", head_ref="HEAD",
                head_sha=repo_context_forge.run_git(repo, ["rev-parse", "HEAD"]),
                source_dirty=False, target_dirty=False, cache_key=None,
            )
            with patch.object(repo_context_forge, "resolve_target_state", return_value=state):
                with self.assertRaisesRegex(RuntimeError, "cache-owned"):
                    repo_context_forge.make_packet(
                        repo, mode="repo", base_ref="HEAD", head_ref="HEAD",
                        intent=None, top=5,
                        token_budget=repo_context_forge.DEFAULT_TOKEN_BUDGET,
                        cache_dir=Path(cache_dir), soulforge_bin=None,
                        map_build="never", map_timeout_ms=1,
                        allow_missing_map=True, gitnexus_repo=None,
                    )

            self.assertFalse((repo / repo_context_forge.workflow_index.INDEX_DIR).exists())

    def test_make_packet_runs_soulforge_build_outside_source_repo(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text("print('dirty')\n", encoding="utf-8")
            original_builder = repo_context_forge.build_soulforge_map

            def fake_builder(
                analysis_repo: Path,
                _soulforge_bin: str | None,
                _build_mode: repo_context_forge.MapBuildMode,
                _timeout_ms: int,
            ) -> repo_context_forge.MapBuildResult:
                with (analysis_repo / ".gitignore").open("a", encoding="utf-8") as gitignore:
                    gitignore.write(".soulforge\n")
                return repo_context_forge.MapBuildResult(True, [], 0, "", "", None)

            repo_context_forge.build_soulforge_map = fake_builder
            try:
                packet = repo_context_forge.make_packet(
                    repo,
                    mode="local",
                    base_ref="HEAD",
                    head_ref="HEAD",
                    intent=None,
                    top=5,
                    token_budget=repo_context_forge.DEFAULT_TOKEN_BUDGET,
                    cache_dir=Path(cache_dir),
                    soulforge_bin=None,
                    map_build="always",
                    map_timeout_ms=1,
                    allow_missing_map=True,
                    gitnexus_repo=None,
                )
            finally:
                repo_context_forge.build_soulforge_map = original_builder

            self.assertEqual((repo / ".gitignore").read_text(encoding="utf-8"), "*.log\n")
            self.assertNotEqual(packet["target_state"]["analysis_repo"], str(repo))

    def test_make_packet_reports_preexisting_tool_cache_without_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / ".codex").write_text("cache\n", encoding="utf-8")
            (repo / ".soulforge").mkdir()
            (repo / ".soulforge" / "repomap.db").write_text("cache\n", encoding="utf-8")

            packet = repo_context_forge.make_packet(
                repo,
                mode="repo",
                base_ref="HEAD",
                head_ref="HEAD",
                intent=None,
                top=5,
                token_budget=repo_context_forge.DEFAULT_TOKEN_BUDGET,
                cache_dir=Path(cache_dir),
                soulforge_bin=None,
                map_build="never",
                map_timeout_ms=1,
                allow_missing_map=True,
                gitnexus_repo=None,
            )

            source_status = packet["source_status"]
            self.assertTrue(source_status["unchanged"])
            self.assertEqual(source_status["added_paths"], [])
            self.assertEqual(source_status["tool_cache_paths_created"], [])
            self.assertEqual(
                source_status["tool_cache_paths_before"],
                [".codex", ".soulforge/repomap.db"],
            )
            self.assertFalse(packet["target_state"]["source_dirty"])
            self.assertTrue(packet["target_state"]["source_status_unchanged"])

    def test_make_packet_builds_workflow_index_without_soulforge(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            server = repo / "apps" / "ops-dashboard" / "server"
            server.mkdir(parents=True)
            (server / "agentGateway.mjs").write_text(
                "export function createAgentGateway() {\n"
                "  return { health() { return { ok: true }; } };\n"
                "}\n",
                encoding="utf-8",
            )
            repo_context_forge.run_cmd(["git", "add", "apps/ops-dashboard/server"], cwd=repo)
            repo_context_forge.run_cmd(["git", "commit", "-m", "agent gateway"], cwd=repo)

            packet = repo_context_forge.make_packet(
                repo,
                mode="intent",
                base_ref="HEAD",
                head_ref="HEAD",
                intent="Improve agent gateway robustness",
                top=5,
                token_budget=repo_context_forge.DEFAULT_TOKEN_BUDGET,
                cache_dir=Path(cache_dir),
                soulforge_bin=None,
                map_build="never",
                map_timeout_ms=1,
                allow_missing_map=True,
                gitnexus_repo=None,
            )

            workflow_index = packet["workflow_index"]
            self.assertTrue(workflow_index["available"])
            self.assertTrue(Path(str(workflow_index["db_path"])).exists())
            self.assertGreater(workflow_index["files"], 0)
            self.assertGreater(workflow_index["symbols"], 0)
            self.assertEqual(workflow_index["head_sha"], packet["target_state"]["head_sha"])
            self.assertEqual(
                packet["targets"][0]["path"],
                "apps/ops-dashboard/server/agentGateway.mjs",
            )
            self.assertFalse(packet["soulforge"]["stats"]["available"])
            self.assertTrue(packet["source_status"]["unchanged"])
            rendered = repo_context_forge.render_prompt(packet)
            self.assertIn("<workflow_index available=\"true\"", rendered)
            self.assertIn(
                f"head_sha=\"{workflow_index['head_sha']}\"", rendered
            )

    def test_intent_mode_selects_targets_without_soulforge_map(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            server = repo / "apps" / "ops-dashboard" / "server"
            server.mkdir(parents=True)
            (server / "agentGateway.mjs").write_text(
                "export function createAgentGateway() { return {}; }\n",
                encoding="utf-8",
            )
            (server / "agentRoutes.mjs").write_text(
                "export function createAgentRoutes() { return {}; }\n",
                encoding="utf-8",
            )
            (server / "agentGateway.test.mjs").write_text(
                "test('agent gateway', () => {});\n",
                encoding="utf-8",
            )
            paths = repo_context_forge.source_worktree_files(repo)
            native_index = repo_context_forge.workflow_index.WorkflowIndex(
                repo, repo_context_forge.file_role
            )
            native_index.build(
                paths,
                head_sha=repo_context_forge.run_git(repo, ["rev-parse", "HEAD"]),
                summary_for_symbol=repo_context_forge.synthetic_symbol_summary,
            )
            soul_map = repo_context_forge.SoulForgeMap(repo, native_index)

            targets = repo_context_forge.target_files_for_mode(
                "intent",
                repo_context_forge.read_git_state(repo, "HEAD", "HEAD"),
                soul_map,
                "Improve agent gateway robustness and tests",
                5,
            )

            self.assertEqual(targets[0], "apps/ops-dashboard/server/agentGateway.mjs")
            self.assertIn("apps/ops-dashboard/server/agentRoutes.mjs", targets)
            self.assertIn("apps/ops-dashboard/server/agentGateway.test.mjs", targets)
            self.assertIn(
                "createAgentGateway",
                [symbol.name for symbol in soul_map.symbols_for_file(targets[0])],
            )

    def test_soulforge_adapter_converts_native_records_by_interface(self) -> None:
        file_entry = namedtuple(
            "FileEntry", "path pagerank symbol_count line_count rank"
        )("src/a.py", 0.5, 1, 2, 1)
        symbol_entry = namedtuple(
            "SymbolEntry",
            "name kind line end_line signature is_exported summary summary_source",
        )("handle", "function", 1, 1, "def handle():", True, "summary", "workflow_index")

        class NativeIndex:
            is_available = True

            def ranked_files(self, _limit):
                return [file_entry]

            def lookup_files(self, _paths):
                return {file_entry.path: file_entry}

            def file_symbols(self, _path, _limit):
                return [symbol_entry]

        soul_map = repo_context_forge.SoulForgeMap(Path("/repo"), NativeIndex())

        self.assertEqual(soul_map.top_files(1)[0].path, "src/a.py")
        self.assertEqual(soul_map.files_by_path(["src/a.py"])["src/a.py"].rank, 1)
        self.assertEqual(soul_map.symbols_for_file("src/a.py")[0].name, "handle")

    def test_workflow_index_intent_matches_symbol_names(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "processor.py").write_text(
                "def reconcile_tenant_ledger():\n    return None\n",
                encoding="utf-8",
            )
            native_index = repo_context_forge.workflow_index.WorkflowIndex(
                repo, repo_context_forge.file_role
            )
            native_index.build(
                repo_context_forge.source_worktree_files(repo),
                head_sha=repo_context_forge.run_git(repo, ["rev-parse", "HEAD"]),
                summary_for_symbol=repo_context_forge.synthetic_symbol_summary,
            )

            self.assertEqual(
                native_index.rank_intent(["tenant", "ledger"], 5)[0],
                "src/processor.py",
            )

    def test_workflow_index_intent_uses_whole_terms_and_safe_plurals(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            paths = [
                "src/valid.py",
                "src/classifier.py",
                "src/statue.py",
                "src/gateway.py",
                "src/ledger.py",
            ]
            for path in paths:
                (repo / path).write_text("pass\n", encoding="utf-8")
            native_index = repo_context_forge.workflow_index.WorkflowIndex(
                repo, repo_context_forge.file_role
            )
            native_index.build(
                paths,
                head_sha="abc123",
                summary_for_symbol=repo_context_forge.synthetic_symbol_summary,
            )

            self.assertEqual(native_index.rank_intent(["id"], 5), [])
            self.assertEqual(native_index.rank_intent(["class"], 5), [])
            self.assertEqual(native_index.rank_intent(["status"], 5), [])
            self.assertEqual(native_index.rank_intent(["gateways"], 5), ["src/gateway.py"])
            self.assertEqual(native_index.rank_intent(["GATEWAYS"], 5), ["src/gateway.py"])
            self.assertEqual(native_index.rank_intent(["ledgers"], 5), ["src/ledger.py"])

    def test_workflow_index_negative_symbol_limit_returns_no_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(
                repo, "def indexed_symbol():\n    return 1\n"
            )

            self.assertEqual(native_index.file_symbols("src/a.py", -1), [])

    def test_workflow_index_reads_database_from_uri_special_path(self) -> None:
        with tempfile.TemporaryDirectory() as parent_dir:
            repo = Path(parent_dir) / "repo?#special"
            repo.mkdir()
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(
                repo, "def indexed_symbol():\n    return 1\n"
            )

            self.assertTrue(native_index.status()["available"])
            self.assertEqual(
                [symbol.name for symbol in native_index.file_symbols("src/a.py", 5)],
                ["indexed_symbol"],
            )

    def test_workflow_index_status_rejects_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(repo)
            with closing(sqlite3.connect(native_index.db_path)) as conn, conn:
                conn.execute("DELETE FROM metadata WHERE key = 'head_sha'")

            status = native_index.status()

            self.assertFalse(status["available"])
            self.assertIn("head_sha", str(status["warning"]))

    def test_workflow_index_rebuilds_previous_span_schema(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(
                repo, "def indexed_symbol():\n    return 1\n"
            )
            with closing(sqlite3.connect(native_index.db_path)) as conn, conn:
                conn.execute("UPDATE symbols SET end_line = line")
                conn.execute(
                    "UPDATE metadata SET value = '4' WHERE key = 'schema_version'"
                )

            head_sha = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
            native_index.ensure_current(
                repo_context_forge.source_worktree_files(repo),
                head_sha=head_sha,
                dirty_overlay=False,
                summary_for_symbol=repo_context_forge.synthetic_symbol_summary,
                reuse=True,
            )

            symbol = native_index.file_symbols("src/a.py", 1)[0]
            self.assertEqual((symbol.line, symbol.end_line), (1, 2))

    def test_workflow_index_corrupt_database_fails_closed_for_all_readers(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            native_index = repo_context_forge.workflow_index.WorkflowIndex(
                repo, repo_context_forge.file_role
            )
            native_index.db_path.parent.mkdir(parents=True)
            corrupt = b"not a sqlite database"
            native_index.db_path.write_bytes(corrupt)

            self.assertFalse(native_index.status()["available"])
            calls = {
                "ranked_files": (lambda: native_index.ranked_files(5), []),
                "lookup_files": (lambda: native_index.lookup_files(["src/a.py"]), {}),
                "file_symbols": (lambda: native_index.file_symbols("src/a.py", 5), []),
                "rank_intent": (lambda: native_index.rank_intent(["agent"], 5), []),
                "related_paths": (lambda: native_index.related_paths(["src/a.py"], 5), []),
            }
            for name, (call, expected) in calls.items():
                with self.subTest(name=name):
                    self.assertEqual(call(), expected)
            self.assertEqual(native_index.db_path.read_bytes(), corrupt)
            self.assertIn("read failed", str(native_index.status()["warning"]))

            native_index.build(
                [], head_sha="recovered", summary_for_symbol=lambda *_args: "summary"
            )
            self.assertEqual(native_index.ranked_files(5), [])
            self.assertNotIn("warning", native_index.status())

    def test_workflow_index_closes_build_and_read_connections(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            opened: list[TrackedConnection] = []
            real_connect = sqlite3.connect

            def connect(*args, **kwargs):
                connection = TrackedConnection(real_connect(*args, **kwargs))
                opened.append(connection)
                return connection

            with patch.object(
                repo_context_forge.workflow_index.sqlite3,
                "connect",
                side_effect=connect,
            ):
                native_index = self.build_workflow_index(
                    repo, "def indexed_symbol():\n    return 1\n"
                )
                native_index.status()
                native_index.ranked_files(5)
                native_index.lookup_files(["src/a.py"])
                native_index.file_symbols("src/a.py", 5)
                native_index.rank_intent(["indexed"], 5)
                native_index.related_paths(["src/a.py"], 5)

            self.assertTrue(opened)
            self.assertTrue(all(connection.closed for connection in opened))

    def test_workflow_index_failed_build_closes_and_preserves_index(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(repo)
            original_database = native_index.db_path.read_bytes()
            (repo / "src" / "a.py").write_text(
                "def replacement():\n    return 1\n", encoding="utf-8"
            )
            opened: list[TrackedConnection] = []
            real_connect = sqlite3.connect

            def connect(*args, **kwargs):
                connection = TrackedConnection(real_connect(*args, **kwargs))
                opened.append(connection)
                return connection

            def fail_summary(_path: str, _name: str, _kind: str) -> str:
                raise RuntimeError("summary failed")

            with patch.object(
                repo_context_forge.workflow_index.sqlite3,
                "connect",
                side_effect=connect,
            ):
                with self.assertRaisesRegex(RuntimeError, "summary failed"):
                    native_index.build(
                        ["src/a.py"],
                        head_sha="replacement",
                        summary_for_symbol=fail_summary,
                    )

            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)
            self.assertEqual(native_index.db_path.read_bytes(), original_database)
            self.assertEqual(
                list(native_index.db_path.parent.glob("workflow-index.sqlite3.*.tmp")),
                [],
            )

    def test_workflow_index_exports_only_top_level_public_python_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(
                repo,
                "def public():\n"
                "    def inner():\n"
                "        return 1\n"
                "    return inner()\n\n"
                "class Example:\n"
                "    def method(self):\n"
                "        return 1\n\n"
                "def _private():\n"
                "    return 1\n",
            )

            exports = {
                symbol.name: symbol.is_exported
                for symbol in native_index.file_symbols("src/a.py", 10)
            }

            self.assertEqual(
                exports,
                {
                    "public": True,
                    "inner": False,
                    "Example": True,
                    "method": False,
                    "_private": False,
                },
            )

    def test_workflow_index_exports_only_explicit_javascript_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.ts").write_text(
                "export function publicFunction() {}\n"
                "function privateFunction() {}\n"
                "export class PublicClass {}\n"
                "class PrivateClass {}\n"
                "export const publicArrow = () => {};\n"
                "const privateArrow = () => {};\n",
                encoding="utf-8",
            )
            native_index = self.build_workflow_index(repo)

            exports = {
                symbol.name: symbol.is_exported
                for symbol in native_index.file_symbols("src/a.ts", 10)
            }

            self.assertEqual(
                exports,
                {
                    "publicFunction": True,
                    "PublicClass": True,
                    "publicArrow": True,
                    "privateFunction": False,
                    "PrivateClass": False,
                    "privateArrow": False,
                },
            )

    def test_workflow_index_rejects_parenthesized_non_callable_constants(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.ts").write_text(
                "const config = (defaults);\n"
                "const sum = (left: number, right: number) => left + right;\n"
                "const typed = (value: string): string => value;\n"
                "const callback = (run: () => void): void => run();\n"
                "const deep = (run: (next: () => void) => void): void => run(() => {});\n"
                "const multiline = (\n"
                "  value: string,\n"
                "): string => value;\n"
                "const dialog = (\n"
                "  <Dialog />\n"
                ");\n",
                encoding="utf-8",
            )
            native_index = self.build_workflow_index(repo)

            self.assertEqual(
                [symbol.name for symbol in native_index.file_symbols("src/a.ts", 10)],
                ["sum", "typed", "callback", "deep", "multiline"],
            )

    def test_workflow_index_includes_named_default_exports(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.ts").write_text(
                "export default async function load() {}\n"
                "export default class Service {}\n",
                encoding="utf-8",
            )
            native_index = self.build_workflow_index(repo)

            self.assertEqual(
                {
                    symbol.name: (symbol.kind, symbol.is_exported)
                    for symbol in native_index.file_symbols("src/a.ts", 10)
                },
                {"load": ("function", True), "Service": ("class", True)},
            )

    def test_packet_selects_owning_symbols_for_body_only_changes(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "tests").mkdir()
            python_path = repo / "tests" / "test_service.py"
            python_source = (
                "class ServiceTests:\n"
                "    def test_body(self):\n"
                "        return 1\n"
                + "".join(f"    filler_{index} = {index}\n" for index in range(401))
                + "\ndef after():  # body follows\n"
                "    return 0\n"
            )
            python_path.write_text(python_source, encoding="utf-8")
            typescript_path = repo / "src" / "transform.ts"
            typescript_path.write_text(
                "const transform: (value: string) => string = (\n"
                "  value: string,\n"
                "): string => value;\n\n"
                "const single = value => value.trim();\n\n"
                "const after = () => 0;\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(
                repo, ["add", "tests/test_service.py", "src/transform.ts"]
            )
            repo_context_forge.run_git(repo, ["commit", "-m", "symbol bodies"])

            python_path.write_text(
                python_source.replace("return 1", "return 2").replace(
                    "return 0", "return 9"
                ),
                encoding="utf-8",
            )
            typescript_path.write_text(
                typescript_path.read_text(encoding="utf-8").replace(
                    "value: string", "value: number"
                ).replace("value.trim()", "value.toUpperCase()"),
                encoding="utf-8",
            )
            state = repo_context_forge.read_git_state(repo, "HEAD", "HEAD")
            native_index = self.build_workflow_index(repo)
            entries = repo_context_forge.make_target_entries(
                mode="local",
                source_repo=repo,
                analysis_repo=repo,
                base_ref="HEAD",
                head_ref="HEAD",
                source_git_state=state,
                soul_map=repo_context_forge.SoulForgeMap(repo, native_index),
                targets=["tests/test_service.py", "src/transform.ts"],
            )
            by_path = {str(entry["path"]): entry for entry in entries}

            self.assertEqual(
                [
                    symbol["name"]
                    for symbol in by_path["tests/test_service.py"]["changed_symbols"]
                ],
                ["ServiceTests", "after"],
            )
            self.assertIn(
                "broad_test_container",
                by_path["tests/test_service.py"]["rank_signals"],
            )
            self.assertEqual(
                [
                    symbol["name"]
                    for symbol in by_path["src/transform.ts"]["changed_symbols"]
                ],
                ["transform", "single"],
            )

    def test_packet_does_not_attribute_trailing_module_code(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            python_path = repo / "src" / "service.py"
            python_path.write_text(
                'def handle(sep="("):\n    return sep\n\nSETTING = 1\n',
                encoding="utf-8",
            )
            typescript_path = repo / "src" / "service.ts"
            typescript_path.write_text(
                "function handle(value: string) {\n"
                "  return `'${String(value).replace(/'/g, `'\\\"'\\\"'`)}'`;\n"
                "}\n\n"
                "const setting = 1;\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "src/service.py", "src/service.ts"])
            repo_context_forge.run_git(repo, ["commit", "-m", "module code"])

            python_path.write_text(
                python_path.read_text(encoding="utf-8").replace(
                    "SETTING = 1", "SETTING = 2"
                ),
                encoding="utf-8",
            )
            typescript_path.write_text(
                typescript_path.read_text(encoding="utf-8").replace(
                    "setting = 1", "setting = 2"
                ),
                encoding="utf-8",
            )
            state = repo_context_forge.read_git_state(repo, "HEAD", "HEAD")
            native_index = self.build_workflow_index(repo)
            entries = repo_context_forge.make_target_entries(
                mode="local",
                source_repo=repo,
                analysis_repo=repo,
                base_ref="HEAD",
                head_ref="HEAD",
                source_git_state=state,
                soul_map=repo_context_forge.SoulForgeMap(repo, native_index),
                targets=["src/service.py", "src/service.ts"],
            )

            self.assertEqual(
                {str(entry["path"]): entry["changed_symbols"] for entry in entries},
                {"src/service.py": [], "src/service.ts": []},
            )

    def test_packet_attributes_python_decorator_changes(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            source = repo / "src" / "decorated.py"
            source.write_text(
                "def marker(value):\n"
                "    return value\n\n"
                "@marker\n"
                "def handle():\n"
                "    return 1\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "src/decorated.py"])
            repo_context_forge.run_git(repo, ["commit", "-m", "decorated symbol"])
            source.write_text(
                source.read_text(encoding="utf-8").replace("@marker", "@marker()"),
                encoding="utf-8",
            )
            state = repo_context_forge.read_git_state(repo, "HEAD", "HEAD")
            native_index = self.build_workflow_index(repo)
            entries = repo_context_forge.make_target_entries(
                mode="local",
                source_repo=repo,
                analysis_repo=repo,
                base_ref="HEAD",
                head_ref="HEAD",
                source_git_state=state,
                soul_map=repo_context_forge.SoulForgeMap(repo, native_index),
                targets=["src/decorated.py"],
            )

            self.assertEqual(
                [symbol["name"] for symbol in entries[0]["changed_symbols"]],
                ["handle"],
            )

    def test_workflow_index_relates_python_test_companions(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "service.py").write_text(
                "def handle():\n    return 1\n", encoding="utf-8"
            )
            (repo / "tests").mkdir()
            (repo / "tests" / "test_service.py").write_text(
                "def test_handle():\n    assert True\n", encoding="utf-8"
            )
            native_index = self.build_workflow_index(repo)

            self.assertEqual(
                native_index.related_paths(["src/service.py"], 5)[0],
                "tests/test_service.py",
            )

    def test_typescript_test_files_are_verification_surface(self) -> None:
        self.assertEqual(repo_context_forge.file_role("src/App.test.ts"), "test")
        self.assertEqual(repo_context_forge.file_role("src/App.test.tsx"), "test")
        self.assertEqual(repo_context_forge.file_role("src/App.spec.tsx"), "test")

    def test_required_intake_includes_architecture_and_gitnexus_authority(self) -> None:
        packet = {
            "schema_version": 1,
            "mode": "pr",
            "token_budget": 16000,
            "target_state": {"head_sha": "abc123"},
            "semantic_summaries": {"mode": "full_cached", "source_counts": {"llm": 1}},
            "gitnexus": {
                "status": "reindexed",
                "repo": "analysis-repo",
                "expected_head_sha": "abc123",
                "indexed_head_sha": "abc123",
                "required_checks_resolved": True,
            },
            "targets": [
                {
                    "path": "src/service.py",
                    "surface_role": "production",
                    "priority_score": 900,
                    "rank_signals": ["changed_file"],
                    "symbols": [{"name": "handle", "kind": "function"}],
                    "soulforge_impact": {
                        "risk": "medium",
                        "direct_dependents": 2,
                        "total_affected_scope": 4,
                    },
                },
                {
                    "path": "tests/test_service.py",
                    "surface_role": "test",
                    "priority_score": 800,
                    "rank_signals": ["broad_test_container"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
            ],
            "coverage_plan": {"required": False, "delegation_required": False, "areas": []},
        }

        rendered = repo_context_forge.render_required_intake(packet)

        self.assertIn("gitnexus: authority=packet", rendered)
        self.assertIn("repo=analysis-repo", rendered)
        self.assertIn("expected_head_sha=abc123", rendered)
        self.assertIn("indexed_head_sha=abc123", rendered)
        self.assertIn("gitnexus_tool_discovery:", rendered)
        self.assertIn("architecture_summary:", rendered)
        self.assertIn("- roles | production=1,test=1", rendered)
        self.assertIn(
            "- hotspots | src/service.py risk=medium direct_dependents=2 total_affected=4",
            rendered,
        )
        self.assertIn("- entry_points | handle", rendered)

    def test_context_digest_rejects_malformed_packet_sections(self) -> None:
        packet = {
            "mode": "pr",
            "target_state": {"head_sha": "abc123"},
            "gitnexus": {},
            "targets": "not-a-list",
        }

        with self.assertRaisesRegex(ValueError, "targets must be a list"):
            repo_context_forge.render_context_digest_lines(packet)

    def test_make_packet_populates_architecture_and_gitnexus_expected_head(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text(
                "def handle():\n    return 1\n", encoding="utf-8"
            )
            packet = repo_context_forge.make_packet(
                repo,
                mode="local",
                base_ref="HEAD",
                head_ref="HEAD",
                intent=None,
                top=5,
                token_budget=repo_context_forge.DEFAULT_TOKEN_BUDGET,
                cache_dir=Path(cache_dir),
                soulforge_bin=None,
                map_build="never",
                map_timeout_ms=1,
                allow_missing_map=True,
                gitnexus_repo=None,
            )

            self.assertEqual(
                packet["gitnexus"]["expected_head_sha"],
                packet["target_state"]["head_sha"],
            )
            self.assertEqual(packet["gitnexus"]["status"], "disabled")
            self.assertTrue(packet["workflow_index"]["dirty_overlay"])
            self.assertEqual(packet["architecture_summary"]["roles"], {"production": 1})

    def test_soulforge_impact_summary_uses_native_map_tables(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            db_dir = repo / ".soulforge"
            db_dir.mkdir()
            db_path = db_dir / "repomap.db"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.executescript(
                    """
                    CREATE TABLE files (
                      id INTEGER PRIMARY KEY,
                      path TEXT,
                      pagerank REAL,
                      symbol_count INTEGER,
                      line_count INTEGER
                    );
                    CREATE TABLE edges (
                      source_file_id INTEGER,
                      target_file_id INTEGER,
                      weight REAL,
                      confidence INTEGER
                    );
                    CREATE TABLE cochanges (
                      file_id_a INTEGER,
                      file_id_b INTEGER,
                      count INTEGER
                    );
                    CREATE TABLE symbols (
                      id INTEGER PRIMARY KEY,
                      file_id INTEGER,
                      name TEXT,
                      kind TEXT,
                      line INTEGER,
                      end_line INTEGER,
                      is_exported INTEGER,
                      signature TEXT
                    );
                    CREATE TABLE refs (
                      file_id INTEGER,
                      name TEXT,
                      source_file_id INTEGER,
                      import_source TEXT
                    );
                    CREATE TABLE calls (
                      caller_symbol_id INTEGER,
                      callee_name TEXT,
                      callee_symbol_id INTEGER,
                      callee_file_id INTEGER,
                      line INTEGER
                    );
                    """
                )
                conn.executemany(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?)",
                    [
                        (1, "src/core.py", 0.5, 1, 20),
                        (2, "src/app.py", 0.4, 1, 20),
                        (3, "tests/test_core.py", 0.3, 1, 20),
                        (4, "src/config.py", 0.2, 1, 20),
                        (5, "src/worker.py", 0.1, 1, 20),
                    ]
                    + [
                        (index, f"src/client_{index}.py", 0.1, 1, 20)
                        for index in range(6, 15)
                    ],
                )
                conn.executemany(
                    "INSERT INTO edges VALUES (?, ?, ?, ?)",
                    [(2, 1, 1.0, 1), (3, 2, 1.0, 1), (1, 4, 0.5, 1)]
                    + [(index, 1, 1.0, 1) for index in range(6, 15)],
                )
                conn.execute("INSERT INTO cochanges VALUES (1, 3, 4)")
                conn.executemany(
                    "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (10, 1, "Handle", "function", 3, 8, 1, "def Handle()"),
                        (20, 2, "caller", "function", 2, 4, 1, "def caller()"),
                    ],
                )
                conn.executemany(
                    "INSERT INTO refs VALUES (?, ?, ?, ?)",
                    [(2, "Handle", 1, None), (3, "Handle", 1, None), (5, "Handle", 1, None)],
                )
                conn.execute("INSERT INTO calls VALUES (20, 'Handle', 10, 1, 3)")

            soul_map = repo_context_forge.SoulForgeMap(repo)
            impact = soul_map.impact_summary_for_file("src/core.py")
            target = repo_context_forge.make_target_entries(
                mode="repo",
                source_repo=repo,
                analysis_repo=repo,
                base_ref="HEAD",
                head_ref="HEAD",
                source_git_state=repo_context_forge.read_git_state(repo, "HEAD", "HEAD"),
                soul_map=soul_map,
                targets=["src/core.py"],
            )[0]

            self.assertEqual(target["dependent_count"], 10)
            self.assertEqual(impact["direct_dependents"], 10)
            self.assertEqual(impact["dependencies"], 1)
            self.assertEqual(impact["cochange_partners"], 1)
            self.assertEqual(impact["total_affected_scope"], 11)
            self.assertEqual(impact["risk"], "high")
            self.assertEqual(len(impact["dependents"]), 8)
            self.assertEqual(impact["dependents"][0]["path"], "src/app.py")
            self.assertEqual(impact["exported_symbols_at_risk"][0]["name"], "Handle")
            self.assertEqual(impact["exported_symbols_at_risk"][0]["usage_files"], 3)

    def test_symbols_prefer_cached_llm_summary_and_fill_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            db_dir = repo / ".soulforge"
            db_dir.mkdir()
            with closing(sqlite3.connect(db_dir / "repomap.db")) as conn, conn:
                conn.executescript(
                    """
                    CREATE TABLE files (
                      id INTEGER PRIMARY KEY,
                      path TEXT,
                      pagerank REAL,
                      symbol_count INTEGER,
                      line_count INTEGER
                    );
                    CREATE TABLE symbols (
                      id INTEGER PRIMARY KEY,
                      file_id INTEGER,
                      name TEXT,
                      kind TEXT,
                      line INTEGER,
                      end_line INTEGER,
                      is_exported INTEGER,
                      signature TEXT
                    );
                    CREATE TABLE semantic_summaries (
                      symbol_id INTEGER,
                      source TEXT,
                      summary TEXT
                    );
                    """
                )
                conn.execute("INSERT INTO files VALUES (1, 'src/core.py', 1.0, 2, 20)")
                conn.executemany(
                    "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (10, 1, "HandleDraft", "function", 2, 8, 1, "def HandleDraft()"),
                        (20, 1, "missingSummary", "method", 10, 12, 0, None),
                    ],
                )
                conn.executemany(
                    "INSERT INTO semantic_summaries VALUES (?, ?, ?)",
                    [
                        (10, "synthetic", "synthetic cached"),
                        (10, "ast", "docstring cached"),
                        (10, "llm", "LLM cached behavior"),
                    ],
                )

            symbols = repo_context_forge.SoulForgeMap(repo).symbols_for_file("src/core.py")

            self.assertEqual(symbols[0].summary, "LLM cached behavior")
            self.assertEqual(symbols[0].summary_source, "llm")
            self.assertEqual(symbols[1].summary, "method in src: missing summary")
            self.assertEqual(symbols[1].summary_source, "synthetic_fallback")

    def test_semantic_summary_section_counts_sources(self) -> None:
        section = repo_context_forge.semantic_summary_section(
            [
                {
                    "symbols": [
                        {"summary_source": "llm"},
                        {"summary_source": "synthetic_fallback"},
                    ]
                },
                {"symbols": [{"summary_source": "llm"}]},
            ]
        )

        self.assertEqual(section["mode"], "full_cached")
        self.assertFalse(section["live_llm_generation"])
        self.assertEqual(section["source_counts"], {"llm": 2, "synthetic_fallback": 1})

    def test_soulforge_target_metadata_verifies_analysis_head(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            state = repo_context_forge.ensure_repo_analysis_checkout(
                repo,
                "HEAD",
                Path(cache_dir),
            )
            db_path = state.analysis_repo / ".soulforge" / "repomap.db"
            db_path.parent.mkdir()
            db_path.write_text("db", encoding="utf-8")

            metadata = repo_context_forge.soulforge_target_metadata(
                state,
                repo_context_forge.MapBuildResult(False, [], None, "", "", None),
                repo_context_forge.SoulForgeMap(state.analysis_repo),
                Path(cache_dir),
            )

            self.assertEqual(metadata["status"], "fresh")
            self.assertTrue(metadata["target_head_verified"])
            self.assertEqual(metadata["analysis_head_sha"], state.head_sha)
            self.assertTrue(metadata["analysis_repo_is_cache_owned"])

    def test_gitnexus_auto_blocks_when_analysis_checkout_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as registry_dir:
            analysis_repo = Path(repo_dir) / "analysis"
            analysis_repo.mkdir()
            registry_path = Path(registry_dir) / "registry.json"
            registry_path.write_text("[]", encoding="utf-8")
            state = repo_context_forge.TargetState(
                mode="pr",
                source_repo=analysis_repo,
                analysis_repo=analysis_repo,
                base_ref="main",
                head_ref="HEAD",
                head_sha="new-head",
                source_dirty=False,
                target_dirty=False,
                cache_key="key",
            )
            lock_path = analysis_repo.parent / f".{analysis_repo.name}.gitnexus.lock"

            with lock_path.open("a") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                status = self.ensure_gitnexus_index(
                    state,
                    "analysis",
                    "auto",
                    registry_path=registry_path,
                    gitnexus_bin="/bin/true",
                )

            self.assertEqual(status["status"], "blocked")
            self.assertFalse(status["reindex_attempted"])
            self.assertIn("already running", status["warning"])

    def test_gitnexus_blocks_when_registered_index_storage_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as registry_dir:
            analysis_repo = Path(repo_dir)
            registry_path = Path(registry_dir) / "registry.json"
            state = repo_context_forge.TargetState(
                mode="pr",
                source_repo=analysis_repo,
                analysis_repo=analysis_repo,
                base_ref="main",
                head_ref="HEAD",
                head_sha="new-head",
                source_dirty=False,
                target_dirty=False,
                cache_key="key",
            )
            registry_path.write_text(
                repo_context_forge.json.dumps(
                    [
                        {
                            "name": "analysis",
                            "path": str(analysis_repo.resolve()),
                            "lastCommit": "new-head",
                            "indexedAt": "now",
                            "storagePath": str(analysis_repo / ".gitnexus"),
                        }
                    ]
                ),
                encoding="utf-8",
            )

            status = self.ensure_gitnexus_index(
                state,
                "analysis",
                "check",
                registry_path=registry_path,
                gitnexus_bin="gitnexus",
            )

            self.assertEqual(status["status"], "blocked")
            self.assertFalse(status["index_fresh"])
            self.assertFalse(status["index_present"])
            self.assertIn("index storage is missing", status["warning"])

    def test_gitnexus_command_failure_blocks_required_check(self) -> None:
        status = self.execute_gitnexus_plan(
            [{"kind": "symbol_context", "target": "missing_symbol", "file": "src/a.py"}],
            {"status": "fresh", "repo": "analysis"},
            gitnexus_bin="/bin/false",
        )

        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["missing_required_symbols"], ["missing_symbol"])
        self.assertFalse(status["required_checks_resolved"])

    def test_gitnexus_placeholder_is_not_a_resolved_result(self) -> None:
        self.assertFalse(repo_context_forge.gitnexus_analysis.result_is_resolved({}))
        self.assertFalse(
            repo_context_forge.gitnexus_analysis.result_is_resolved({"status": "resolved"})
        )

    def test_empty_gitnexus_plan_executes_no_semantic_calls(self) -> None:
        status = self.execute_gitnexus_plan(
            [],
            {"status": "fresh", "repo": "unused"},
            gitnexus_bin="/bin/false",
        )

        self.assertTrue(status["required_checks_resolved"])
        self.assertEqual(status["analysis"]["status"], "resolved")
        self.assertEqual(status["analysis"]["graph_call_count"], 0)
        self.assertEqual(status["analysis"]["process_count"], 0)

    def test_duplicate_plan_entries_execute_once_with_real_gitnexus(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as runtime_home:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text(
                "def connect():\n    return 1\n", encoding="utf-8"
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "add connect"])
            env = {**os.environ, "HOME": runtime_home}
            indexed = repo_context_forge.run_cmd(
                ["gitnexus", "analyze", "--force", "--skip-agents-md", str(repo)],
                env=env,
                suppress_core_dump=True,
                allow_fail=True,
            )
            self.assertEqual(indexed.returncode, 0, indexed.stderr)
            registry = repo_context_forge.json.loads(
                (Path(runtime_home) / ".gitnexus" / "registry.json").read_text(encoding="utf-8")
            )
            repo_name = next(
                entry["name"] for entry in registry if Path(entry["path"]).resolve() == repo.resolve()
            )
            check = {"kind": "symbol_context", "target": "connect", "file": "src/a.py"}

            with patch.dict(os.environ, {"HOME": runtime_home}):
                status = self.execute_gitnexus_plan(
                    [check, dict(check)],
                    {"status": "fresh", "repo": repo_name},
                    gitnexus_bin="gitnexus",
                )

            self.assertTrue(status["required_checks_resolved"])
            self.assertEqual(status["analysis"]["graph_call_count"], 1)
            self.assertEqual(len(status["analysis"]["entries"]), 1)

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

    def test_public_bootstrap_writes_machine_packet_for_early_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            packet_path = repo / "blocker.json"
            self.make_git_repo(repo)
            repo_context_forge.run_git(repo, ["checkout", "--detach"])

            result = repo_context_forge.run_cmd(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "codex_context_bootstrap.py"),
                    "--repo",
                    str(repo),
                    "--mode",
                    "local",
                    "--base",
                    "HEAD",
                    "--packet-json-out",
                    str(packet_path),
                ],
                allow_fail=True,
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            packet = repo_context_forge.json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertTrue(packet["blocked"])
            self.assertIn("detached checkout", packet["blocker"]["reason"])

    def test_bootstrap_ignores_tool_cache_as_context_surface(self) -> None:
        state = repo_context_forge.GitState(
            branch="(detached)",
            head="abc123",
            base_ref="origin/main",
            merge_base="base",
            pr_files=[],
            staged_files=[],
            unstaged_files=[],
            untracked_files=[".soulforge/repomap.db", ".codex", ".gitnexus/meta.json"],
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

    def test_bootstrap_filters_cache_worktree_suggestions(self) -> None:
        cache_dir = Path("/home/user/.cache/repo-context-forge")

        self.assertFalse(
            codex_context_bootstrap.is_user_worktree(
                "/home/user/.cache/repo-context-forge/worktrees/repo-head",
                cache_dir,
            )
        )
        self.assertTrue(
            codex_context_bootstrap.is_user_worktree(
                "/home/user/worktrees/repo-feature",
                cache_dir,
            )
        )

    def test_bootstrap_prefers_environment_pr_base_over_main(self) -> None:
        with patch.dict(
            os.environ, {"GITHUB_BASE_REF": "codex/native-startup-checkout"}
        ), patch.object(
            codex_context_bootstrap,
            "git_output",
            side_effect=lambda _repo, args: "sha"
            if args[-1] in {"origin/codex/native-startup-checkout", "origin/main"}
            else "",
        ):
            base = codex_context_bootstrap.first_existing_base(Path("/repo"), None)

        self.assertEqual(base, "origin/codex/native-startup-checkout")

    def test_bootstrap_prefers_live_pr_base_over_main(self) -> None:
        def fake_run_cmd(args, **_kwargs):
            if args[:2] == ["gh", "pr"]:
                return repo_context_forge.subprocess.CompletedProcess(
                    args, 0, "codex/native-startup-checkout\n", ""
                )
            raise AssertionError(f"unexpected command: {args}")

        with patch.dict(os.environ, {"GITHUB_BASE_REF": ""}), patch.object(
            codex_context_bootstrap.forge,
            "run_cmd",
            side_effect=fake_run_cmd,
        ), patch.object(
            codex_context_bootstrap.shutil,
            "which",
            return_value="/usr/bin/gh",
        ), patch.object(
            codex_context_bootstrap,
            "git_output",
            side_effect=lambda _repo, args: "origin-sha"
            if args[-1] == "origin/codex/native-startup-checkout"
            else "upstream-sha"
            if args[-1] == "upstream/codex/native-startup-checkout"
            else "main-sha"
            if args[-1] == "origin/main"
            else "",
        ):
            base = codex_context_bootstrap.first_existing_base(Path("/repo"), None)

        self.assertEqual(base, "origin/codex/native-startup-checkout")

    def test_bootstrap_falls_back_to_main_without_github_cli(self) -> None:
        with patch.dict(os.environ, {"GITHUB_BASE_REF": ""}), patch.object(
            codex_context_bootstrap.shutil,
            "which",
            return_value=None,
        ), patch.object(
            codex_context_bootstrap,
            "git_output",
            side_effect=lambda _repo, args: "sha"
            if args[-1] == "origin/main"
            else "",
        ):
            base = codex_context_bootstrap.first_existing_base(Path("/repo"), None)

        self.assertEqual(base, "origin/main")

    def test_bootstrap_blocks_detached_pr_checkout_before_mapping(self) -> None:
        original_current_branch = codex_context_bootstrap.current_branch
        codex_context_bootstrap.current_branch = lambda _repo: ""
        try:
            reason = codex_context_bootstrap.should_block_stale_pr_checkout(
                Path("/repo"),
                "pr",
                "HEAD",
                False,
            )
        finally:
            codex_context_bootstrap.current_branch = original_current_branch

        self.assertEqual(
            reason,
            "detached PR checkout cannot be verified as latest; select the active PR branch/worktree",
        )

    def test_bootstrap_allows_explicit_non_head_pr_checkout(self) -> None:
        reason = codex_context_bootstrap.should_block_stale_pr_checkout(
            Path("/repo"),
            "pr",
            "origin/feature",
            False,
        )

        self.assertIsNone(reason)

    def test_bootstrap_blocks_branch_behind_upstream_before_mapping(self) -> None:
        original_current_branch = codex_context_bootstrap.current_branch
        original_branch_is_behind = codex_context_bootstrap.branch_is_behind_upstream
        codex_context_bootstrap.current_branch = lambda _repo: "feature"
        codex_context_bootstrap.branch_is_behind_upstream = (
            lambda _repo, _branch: ("feature", "a" * 40, "b" * 40)
        )
        try:
            reason = codex_context_bootstrap.should_block_stale_pr_checkout(
                Path("/repo"),
                "pr",
                "HEAD",
                False,
            )
        finally:
            codex_context_bootstrap.current_branch = original_current_branch
            codex_context_bootstrap.branch_is_behind_upstream = original_branch_is_behind

        self.assertEqual(
            reason,
            "branch feature is not at upstream head (aaaaaaaaaaaa != bbbbbbbbbbbb); update the PR worktree before mapping",
        )

    def test_bootstrap_allows_current_branch_matching_upstream(self) -> None:
        original_current_branch = codex_context_bootstrap.current_branch
        original_branch_is_behind = codex_context_bootstrap.branch_is_behind_upstream
        codex_context_bootstrap.current_branch = lambda _repo: "feature"
        codex_context_bootstrap.branch_is_behind_upstream = lambda _repo, _branch: None
        try:
            reason = codex_context_bootstrap.should_block_stale_pr_checkout(
                Path("/repo"),
                "pr",
                "HEAD",
                False,
            )
        finally:
            codex_context_bootstrap.current_branch = original_current_branch
            codex_context_bootstrap.branch_is_behind_upstream = original_branch_is_behind

        self.assertIsNone(reason)

    def test_bootstrap_upstream_check_allows_local_ahead_branch(self) -> None:
        original_upstream_ref = codex_context_bootstrap.upstream_ref
        original_refresh = codex_context_bootstrap.refresh_upstream_ref
        original_git_output = codex_context_bootstrap.git_output
        original_is_ancestor = codex_context_bootstrap.is_ancestor
        codex_context_bootstrap.upstream_ref = lambda _repo, _branch: "origin/feature"
        codex_context_bootstrap.refresh_upstream_ref = lambda _repo, _upstream: None
        codex_context_bootstrap.git_output = (
            lambda _repo, args: "localsha" if args[-1] == "feature" else "upstreamsha"
        )
        codex_context_bootstrap.is_ancestor = (
            lambda _repo, ancestor, descendant: ancestor == "upstreamsha"
            and descendant == "localsha"
        )
        try:
            stale = codex_context_bootstrap.branch_is_behind_upstream(
                Path("/repo"),
                "feature",
            )
        finally:
            codex_context_bootstrap.upstream_ref = original_upstream_ref
            codex_context_bootstrap.refresh_upstream_ref = original_refresh
            codex_context_bootstrap.git_output = original_git_output
            codex_context_bootstrap.is_ancestor = original_is_ancestor

        self.assertIsNone(stale)

    def test_bootstrap_upstream_check_blocks_local_behind_branch(self) -> None:
        original_upstream_ref = codex_context_bootstrap.upstream_ref
        original_refresh = codex_context_bootstrap.refresh_upstream_ref
        original_git_output = codex_context_bootstrap.git_output
        original_is_ancestor = codex_context_bootstrap.is_ancestor
        codex_context_bootstrap.upstream_ref = lambda _repo, _branch: "origin/feature"
        codex_context_bootstrap.refresh_upstream_ref = lambda _repo, _upstream: None
        codex_context_bootstrap.git_output = (
            lambda _repo, args: "localsha" if args[-1] == "feature" else "upstreamsha"
        )
        codex_context_bootstrap.is_ancestor = (
            lambda _repo, ancestor, descendant: ancestor == "localsha"
            and descendant == "upstreamsha"
        )
        try:
            stale = codex_context_bootstrap.branch_is_behind_upstream(
                Path("/repo"),
                "feature",
            )
        finally:
            codex_context_bootstrap.upstream_ref = original_upstream_ref
            codex_context_bootstrap.refresh_upstream_ref = original_refresh
            codex_context_bootstrap.git_output = original_git_output
            codex_context_bootstrap.is_ancestor = original_is_ancestor

        self.assertEqual(stale, ("feature", "localsha", "upstreamsha"))

    def test_bootstrap_allows_stale_pr_when_explicitly_requested(self) -> None:
        reason = codex_context_bootstrap.should_block_stale_pr_checkout(
            Path("/repo"),
            "pr",
            "HEAD",
            True,
        )

        self.assertIsNone(reason)

    def test_bootstrap_defaults_to_analysis_gitnexus_repo_and_large_budget(self) -> None:
        captured: dict[str, object] = {}
        originals = {
            "is_git_repo": codex_context_bootstrap.forge.is_git_repo,
            "repo_root": codex_context_bootstrap.forge.repo_root,
            "first_existing_base": codex_context_bootstrap.first_existing_base,
            "find_soulforge_binary": codex_context_bootstrap.forge.find_soulforge_binary,
            "make_packet": codex_context_bootstrap.forge.make_packet,
            "render_prompt": codex_context_bootstrap.forge.render_prompt,
            "output_text": codex_context_bootstrap.forge.output_text,
        }

        def fake_make_packet(*_args, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        codex_context_bootstrap.forge.is_git_repo = lambda _repo: True
        codex_context_bootstrap.forge.repo_root = lambda _repo: Path("/repo")
        codex_context_bootstrap.first_existing_base = lambda *_args, **_kwargs: "main"
        codex_context_bootstrap.forge.find_soulforge_binary = lambda _bin: None
        codex_context_bootstrap.forge.make_packet = fake_make_packet
        codex_context_bootstrap.forge.render_prompt = lambda _packet: "packet"
        codex_context_bootstrap.forge.output_text = lambda _text, _out: None
        try:
            result = codex_context_bootstrap.main(["--repo", "/repo", "--mode", "repo"])
        finally:
            codex_context_bootstrap.forge.is_git_repo = originals["is_git_repo"]
            codex_context_bootstrap.forge.repo_root = originals["repo_root"]
            codex_context_bootstrap.first_existing_base = originals["first_existing_base"]
            codex_context_bootstrap.forge.find_soulforge_binary = originals["find_soulforge_binary"]
            codex_context_bootstrap.forge.make_packet = originals["make_packet"]
            codex_context_bootstrap.forge.render_prompt = originals["render_prompt"]
            codex_context_bootstrap.forge.output_text = originals["output_text"]

        self.assertEqual(result, 0)
        self.assertIsNone(captured["gitnexus_repo"])
        self.assertEqual(captured["token_budget"], repo_context_forge.DEFAULT_TOKEN_BUDGET)
        self.assertGreaterEqual(captured["token_budget"], 16_000)

    def test_bootstrap_can_emit_required_intake_before_packet(self) -> None:
        captured: dict[str, str] = {}
        originals = {
            "is_git_repo": codex_context_bootstrap.forge.is_git_repo,
            "repo_root": codex_context_bootstrap.forge.repo_root,
            "first_existing_base": codex_context_bootstrap.first_existing_base,
            "find_soulforge_binary": codex_context_bootstrap.forge.find_soulforge_binary,
            "make_packet": codex_context_bootstrap.forge.make_packet,
            "render_prompt": codex_context_bootstrap.forge.render_prompt,
            "output_text": codex_context_bootstrap.forge.output_text,
        }
        packet = {
            "schema_version": 1,
            "mode": "pr",
            "token_budget": 16000,
            "target_state": {"head_sha": "abc123"},
            "semantic_summaries": {
                "mode": "full_cached",
                "live_llm_generation": False,
                "source_counts": {"synthetic": 2},
            },
            "gitnexus": {
                "repo": "example-index",
                "status": "fresh",
                "required_checks_resolved": True,
            },
            "coverage_plan": {
                "required": True,
                "delegation_required": False,
                "areas": [
                    {
                        "id": "production_contract",
                        "kind": "production",
                        "required": True,
                        "files": ["src/a.py"],
                        "why": "changed production surface from the packet",
                        "must_answer": "What changed?",
                    }
                ],
            },
            "targets": [
                {
                    "path": "src/a.py",
                    "priority_score": 10,
                    "surface_role": "production",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {"risk": "medium", "direct_dependents": 1},
                }
            ],
        }

        codex_context_bootstrap.forge.is_git_repo = lambda _repo: True
        codex_context_bootstrap.forge.repo_root = lambda _repo: Path("/repo")
        codex_context_bootstrap.first_existing_base = lambda *_args, **_kwargs: "main"
        codex_context_bootstrap.forge.find_soulforge_binary = lambda _bin: None
        codex_context_bootstrap.forge.make_packet = lambda *_args, **_kwargs: packet
        codex_context_bootstrap.forge.render_prompt = lambda _packet: "<repo_context_packet/>\n"
        codex_context_bootstrap.forge.output_text = (
            lambda text, _out: captured.__setitem__("text", text)
        )
        try:
            result = codex_context_bootstrap.main(
                ["--repo", "/repo", "--mode", "repo", "--enforce-intake"]
            )
        finally:
            codex_context_bootstrap.forge.is_git_repo = originals["is_git_repo"]
            codex_context_bootstrap.forge.repo_root = originals["repo_root"]
            codex_context_bootstrap.first_existing_base = originals["first_existing_base"]
            codex_context_bootstrap.forge.find_soulforge_binary = originals["find_soulforge_binary"]
            codex_context_bootstrap.forge.make_packet = originals["make_packet"]
            codex_context_bootstrap.forge.render_prompt = originals["render_prompt"]
            codex_context_bootstrap.forge.output_text = originals["output_text"]

        self.assertEqual(result, 0)
        self.assertTrue(captured["text"].startswith("REPO_CONTEXT_FORGE_REQUIRED_INTAKE"))
        self.assertIn("mode: pr", captured["text"])
        self.assertIn("token_budget: 16000", captured["text"])
        self.assertIn("sources=synthetic=2", captured["text"])
        self.assertIn(
            "gitnexus: authority=packet; repo=example-index; status=fresh",
            captured["text"],
        )
        self.assertIn("src/a.py", captured["text"])
        self.assertIn(
            "Consume gitnexus_analysis first; gitnexus_required_checks records the already-executed packet plan",
            captured["text"],
        )
        self.assertIn("coverage_plan: required=true delegation_required=false", captured["text"])
        self.assertIn("production_contract", captured["text"])
        self.assertIn("parent_coverage_tasks:", captured["text"])
        self.assertNotIn("spawn_agent |", captured["text"])
        self.assertIn("cover_serially | covers=production_contract", captured["text"])
        self.assertIn("covers=production_contract", captured["text"])
        self.assertIn("Do not spawn sub-agents from Repo Context Forge output alone.", captured["text"])
        self.assertIn(
            "Cover every required coverage area in the parent session",
            captured["text"],
        )
        self.assertIn("Do not let unscoped gitnexus_detect_changes(compare)", captured["text"])
        self.assertIn("END_REPO_CONTEXT_FORGE_REQUIRED_INTAKE\n<repo_context_packet/>", captured["text"])

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
                            "summary": "Handles the selected packet target",
                            "summary_source": "llm",
                        }
                    ],
                    "soulforge_impact": {
                        "risk": "medium",
                        "direct_dependents": 2,
                        "dependencies": 1,
                        "cochange_partners": 1,
                        "total_affected_scope": 4,
                        "dependents": [{"path": "src/caller.py", "weight": 1.0}],
                        "cochanges": [{"path": "tests/test_a.py", "count": 3}],
                        "exported_symbols_at_risk": [
                            {"name": "handle", "kind": "function", "usage_files": 2}
                        ],
                    },
                }
            ],
            "semantic_summaries": {
                "mode": "full_cached",
                "synthetic_fill": True,
                "live_llm_generation": False,
                "source_counts": {"llm": 1},
            },
            "gitnexus": {
                "plan": [
                    {"kind": "symbol_context", "target": "handle", "file": "src/a.py"}
                ]
            },
            "coverage_plan": {
                "required": True,
                "delegation_required": False,
                "areas": [
                    {
                        "id": "production_contract",
                        "kind": "production",
                        "required": True,
                        "files": ["src/a.py"],
                        "why": "changed production surface from the packet",
                        "must_answer": "What changed?",
                    }
                ],
            },
        }

        rendered = repo_context_forge.render_prompt(packet)

        self.assertIn("<repo_context_packet", rendered)
        self.assertIn("<token_budget>16000</token_budget>", rendered)
        self.assertIn("<context_digest>", rendered)
        self.assertIn("<required_agent_intake>", rendered)
        self.assertIn("<coverage_plan required=\"true\" delegation_required=\"false\">", rendered)
        self.assertIn('id="production_contract"', rendered)
        self.assertNotIn('<delegate_task action="spawn_agent"', rendered)
        self.assertNotIn('area="surface_impact_specialist"', rendered)
        self.assertIn("Do not spawn sub-agents from Repo Context Forge output alone.", rendered)
        self.assertIn(
            "Cover every required coverage area in the parent session",
            rendered,
        )
        self.assertIn(
            "Consume <gitnexus_analysis> first; <gitnexus_required_checks> records the already-executed packet plan",
            rendered,
        )
        self.assertIn("<semantic_sources>", rendered)
        self.assertIn("<semantic_summaries>", rendered)
        self.assertIn('<summary source="llm">Handles the selected packet target</summary>', rendered)
        self.assertIn('<source name="llm" count="1"/>', rendered)
        self.assertIn("<soulforge_impact>", rendered)
        self.assertIn("<risk>medium</risk>", rendered)
        self.assertIn("src/caller.py", rendered)
        self.assertIn('path="src/a.py"', rendered)
        self.assertIn('target="handle"', rendered)

    def test_render_prompt_omits_delegate_task_when_delegation_not_required(self) -> None:
        packet = {
            "mode": "local",
            "token_budget": 16000,
            "target_state": {
                "source_repo": "/repo",
                "analysis_repo": "/cache/repo",
                "base_ref": "HEAD",
                "head_ref": "HEAD",
                "head_sha": "abc123",
                "analysis_head_sha": "abc123",
                "analysis_repo_is_cache_owned": True,
                "analysis_head_matches_source_head": True,
                "source_dirty": True,
                "target_dirty": False,
            },
            "source_status": {"unchanged": True},
            "policy": {"reference_only_prefixes": []},
            "soulforge": {"target": {"status": "fresh", "target_head_verified": True}},
            "semantic_summaries": {"mode": "full_cached", "source_counts": {}},
            "gitnexus": {"status": "fresh", "repo": "repo", "plan": []},
            "targets": [],
            "warnings": [],
            "coverage_plan": {
                "required": True,
                "delegation_required": False,
                "areas": [
                    {
                        "id": "production_contract",
                        "kind": "production",
                        "required": True,
                        "files": ["src/a.py"],
                        "why": "changed production surface from the packet",
                        "must_answer": "What changed?",
                    }
                ],
            },
        }

        rendered = repo_context_forge.render_prompt(packet)

        self.assertIn("<coverage_plan required=\"true\" delegation_required=\"false\">", rendered)
        self.assertNotIn('<delegate_task action="spawn_agent"', rendered)

    def test_render_prompt_keeps_multiple_areas_in_parent_session(self) -> None:
        coverage_plan = repo_context_forge.build_coverage_plan(
            [
                {
                    "path": "src/service.py",
                    "surface_role": "production",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {
                        "risk": "medium",
                        "direct_dependents": 1,
                        "total_affected_scope": 2,
                    },
                },
                {
                    "path": "tests/test_service.py",
                    "surface_role": "test",
                    "rank_signals": ["changed_file", "broad_test_container"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
                {
                    "path": "README.md",
                    "surface_role": "production",
                    "rank_signals": ["changed_file"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
                {
                    "path": "src/client.py",
                    "surface_role": "production",
                    "rank_signals": ["graph_neighbor"],
                    "soulforge_impact": {"risk": "low", "direct_dependents": 0},
                },
            ]
        )
        packet = {
            "mode": "intent",
            "token_budget": 16000,
            "target_state": {
                "source_repo": "/repo",
                "analysis_repo": "/cache/repo",
                "base_ref": "main",
                "head_ref": "HEAD",
                "head_sha": "abc123",
                "analysis_head_sha": "abc123",
                "analysis_repo_is_cache_owned": True,
                "analysis_head_matches_source_head": True,
                "source_dirty": True,
                "target_dirty": True,
            },
            "source_status": {"unchanged": True},
            "policy": {"reference_only_prefixes": []},
            "soulforge": {"target": {"status": "fresh", "target_head_verified": True}},
            "semantic_summaries": {"mode": "full_cached", "source_counts": {}},
            "gitnexus": {"status": "fresh", "repo": "repo", "plan": []},
            "targets": [],
            "warnings": [],
            "coverage_plan": coverage_plan,
        }

        rendered = repo_context_forge.render_prompt(packet)

        self.assertNotIn('<delegate_task action="spawn_agent"', rendered)
        self.assertNotIn('area="surface_impact_specialist"', rendered)
        for area_id in [
            "production_contract",
            "verification_contract",
            "operator_contract",
            "blast_radius",
            "related_map_surface",
        ]:
            self.assertIn(f'id="{area_id}"', rendered)

    def test_render_prompt_compacts_to_budget_and_preserves_gitnexus_checks(self) -> None:
        long_summary = "x" * 5000
        packet = {
            "schema_version": 1,
            "mode": "pr",
            "token_budget": 1000,
            "warnings": [],
            "target_state": {
                "source_repo": "/repo",
                "analysis_repo": "/cache/repo",
                "base_ref": "main",
                "head_ref": "HEAD",
                "head_sha": "abc123",
                "analysis_head_sha": "abc123",
                "analysis_repo_is_cache_owned": True,
                "analysis_head_matches_source_head": True,
                "source_dirty": False,
                "target_dirty": False,
            },
            "source_status": {"unchanged": True},
            "soulforge": {
                "target": {"status": "fresh", "target_head_verified": True, "db_exists": True}
            },
            "semantic_summaries": {
                "mode": "full_cached",
                "synthetic_fill": True,
                "live_llm_generation": False,
                "source_counts": {"llm": 1},
            },
            "targets": [
                {
                    "path": "src/a.py",
                    "rank": 1,
                    "surface_role": "production",
                    "priority_score": 1000,
                    "rank_signals": ["changed_file"],
                    "why_selected": ["changed"],
                    "symbols": [
                        {
                            "name": "handle",
                            "kind": "function",
                            "line": 1,
                            "end_line": 3,
                            "signature": "def handle()",
                            "summary": long_summary,
                            "summary_source": "llm",
                        }
                    ],
                }
            ],
            "gitnexus": {
                "status": "fresh",
                "repo": "example",
                "expected_head_sha": "abc123",
                "indexed_head_sha": "abc123",
                "required_checks_resolved": True,
                "plan": [{"kind": "symbol_context", "target": "handle", "file": "src/a.py"}],
            },
            "coverage_plan": {
                "required": True,
                "delegation_required": False,
                "areas": [
                    {
                        "id": "production_contract",
                        "kind": "production",
                        "required": True,
                        "files": ["src/a.py"],
                        "why": "changed production surface from the packet",
                        "must_answer": "What changed?",
                    }
                ],
            },
        }

        rendered = repo_context_forge.render_prompt(packet)

        self.assertLessEqual(repo_context_forge.estimate_tokens(rendered), 1000)
        self.assertIn("<context_digest>", rendered)
        self.assertIn("<required_agent_intake>", rendered)
        self.assertIn("<coverage_plan required=\"true\" delegation_required=\"false\">", rendered)
        self.assertIn('id="production_contract"', rendered)
        self.assertIn('target="handle"', rendered)
        self.assertIn('path="src/a.py"', rendered)
        self.assertNotIn(long_summary, rendered)

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
        codex_context_bootstrap.forge.is_dirty = lambda _repo, **_kwargs: False
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

    def test_bootstrap_auto_mode_defaults_to_repo_when_clean_without_intent(self) -> None:
        original_is_dirty = codex_context_bootstrap.forge.is_dirty
        codex_context_bootstrap.forge.is_dirty = lambda _repo, **_kwargs: False
        try:
            mode = codex_context_bootstrap.choose_mode(
                Path("/repo"),
                "auto",
                None,
                "HEAD",
                None,
            )
        finally:
            codex_context_bootstrap.forge.is_dirty = original_is_dirty

        self.assertEqual(mode, "repo")

    def test_bootstrap_does_not_block_repo_mode(self) -> None:
        reason = codex_context_bootstrap.should_block_empty_checkout(
            Path("/repo"),
            "repo",
            None,
            "HEAD",
            None,
        )

        self.assertIsNone(reason)

    def test_bootstrap_auto_mode_ignores_tool_cache_dirty_for_intent(self) -> None:
        original_is_dirty = codex_context_bootstrap.forge.is_dirty

        def fake_is_dirty(_repo: Path, *, ignore_tool_cache: bool = False) -> bool:
            return not ignore_tool_cache

        codex_context_bootstrap.forge.is_dirty = fake_is_dirty
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
