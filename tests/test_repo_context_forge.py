from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from contextlib import closing, contextmanager
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
    def detect_changes(self, selector: str, worktree: Path, runtime_home: str):
        return repo_context_forge.run_cmd(
            ["gitnexus", "detect-changes", "-r", selector, "--worktree", str(worktree)],
            env={**os.environ, "HOME": runtime_home}, allow_fail=True)

    def test_the_candidate_slot_is_a_separate_checkout_and_selector(self) -> None:
        marker = "CANDIDATE_SLOT_NOT_A_SEPARATE_SLOT"
        with tempfile.TemporaryDirectory() as cache_dir:
            repo, head, cache = Path("/repo"), "0" * 40, Path(cache_dir)
            plain = repo_context_forge.analysis_checkout_path(repo, head, cache, "local")
            slotted = repo_context_forge.analysis_checkout_path(
                repo, head, cache, "local", candidate_slot=True)
            self.assertNotEqual(plain[1], slotted[1], marker)
            self.assertNotEqual(plain[0], slotted[0], marker)
            self.assertEqual(plain[0].parent, slotted[0].parent, marker)

    def test_an_unslotted_selector_is_unchanged_for_every_mode(self) -> None:
        """Every index already on disk was built under these selectors, so an unset
        slot must still resolve to them. The expected values are the measurement
        taken from the producer as it stood before the candidate slot existed
        (9e10481), frozen here: comparing against the installed copy instead would
        compare this code to itself once it ships."""
        marker = "UNSLOTTED_SELECTOR_DRIFTED"
        before = {
            "pr": ("/cache/worktrees/repo-000000000000-32c186c700e9b827", "32c186c700e9b827"),
            "repo": ("/cache/analysis-checkouts/repo-000000000000-d7570194426deb44",
                     "d7570194426deb44"),
            "local": ("/cache/analysis-worktrees/repo-000000000000-5facef2f95f5e691",
                      "5facef2f95f5e691"),
            "intent": ("/cache/analysis-worktrees/repo-000000000000-5facef2f95f5e691",
                       "5facef2f95f5e691"),
        }
        for mode, (expected_path, expected_key) in before.items():
            path, key = repo_context_forge.analysis_checkout_path(
                Path("/repo"), "0" * 40, Path("/cache"), mode)
            self.assertEqual((str(path), key), (expected_path, expected_key), marker + f": {mode}")

    def test_producer_exclusions_are_added_without_destroying_existing_ones(self) -> None:
        """Driven at ensure_cached_checkout, the owner that populates the checkout,
        because nothing here needs a packet or an index."""
        marker = "EXISTING_EXCLUDES_DESTROYED_OR_DUPLICATED"
        with tempfile.TemporaryDirectory() as root:
            repo, cache = Path(root) / "repo", Path(root) / "cache"
            repo.mkdir()
            self.make_git_repo(repo)
            head = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
            checkout, _key = repo_context_forge.analysis_checkout_path(
                repo, head, cache, "local")
            repo_context_forge.ensure_cached_checkout(repo, head, checkout, cache)

            exclude = checkout / ".git" / "info" / "exclude"
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "operator-owned-line\n",
                encoding="utf-8")
            repo_context_forge.ensure_cached_checkout(repo, head, checkout, cache)

            lines = exclude.read_text(encoding="utf-8").splitlines()
            self.assertIn("operator-owned-line", lines, marker)
            for path in repo_context_forge.TOOL_CACHE_DIRS:
                self.assertEqual(lines.count(path), 1, marker + f": {path} in {lines}")

            # The exclusion is only useful if the capture's own scan honours it.
            ledger = checkout / repo_context_forge.workflow_index.INDEX_DIR
            ledger.mkdir(parents=True, exist_ok=True)
            (ledger / "workflow-index.sqlite3").write_bytes(bytes(range(64)))
            self.assertEqual(
                repo_context_forge.run_git(
                    checkout, ["ls-files", "--others", "--exclude-standard"]).split(),
                [], marker + ": the ledger is still visible to git add -A")

    def test_a_pass_keeps_its_baseline_across_candidate_reruns(self) -> None:
        """The one integrated exercise, because only these obligations need a real
        index: the pass-start tree survives its own reruns, the producer's ledger is
        never a gap, the cache gains one slot rather than one per edit, a genuine
        deletion is still reported, and the source checkout is never written."""
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "a.py").write_text(
                "def compute():\n    return 0\n", encoding="utf-8")
            repo_context_forge.run_cmd(["git", "add", "src/a.py"], cwd=repo)
            repo_context_forge.run_cmd(["git", "commit", "-m", "a real symbol"], cwd=repo)
            head = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
            source_exclude = repo / ".git" / "info" / "exclude"
            exclude_before = source_exclude.read_bytes() if source_exclude.exists() else b""

            intake, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
            self.assertEqual(intake.returncode, 0, intake.stderr[-800:])
            self.assertIn(
                packet.get("gitnexus", {}).get("status"), {"fresh", "reindexed"},
                "ORDINARY_INTAKE_REGRESSED")
            pass_start, _key = repo_context_forge.analysis_checkout_path(
                repo, head, Path(cache_dir), "intent")
            recorded_tree = repo_context_forge.json.loads(
                (pass_start / ".gitnexus" / "meta.json").read_text(encoding="utf-8")
            )["indexedTree"]
            # The ledger is the producer's own, written into its own checkout; no
            # source repository contains or ignores it.
            self.assertTrue(
                (pass_start / repo_context_forge.workflow_index.INDEX_DIR).is_dir(),
                "LEDGER_NOT_WRITTEN_BY_THE_PRODUCER")

            for value in (1, 2, 3):
                (repo / "src" / "a.py").write_text(
                    f"def compute():\n    return {value}\n", encoding="utf-8")
                rerun, _packet = self.run_public_intent_bootstrap(
                    repo, cache_dir, runtime_home, candidate_slot=True)
                self.assertEqual(rerun.returncode, 0, rerun.stderr[-800:])

            after = repo_context_forge.json.loads(
                (pass_start / ".gitnexus" / "meta.json").read_text(encoding="utf-8")
            )["indexedTree"]
            self.assertEqual(after, recorded_tree, "PASS_START_BASELINE_DESTROYED")

            prefix = f"{repo.name}-{head[:12]}-"
            slots = sorted(
                path.name for path in (Path(cache_dir) / "analysis-worktrees").iterdir()
                if path.is_dir() and path.name.startswith(prefix))
            self.assertEqual(len(slots), 2, "SLOT_COUNT_UNBOUNDED: " + str(slots))

            probe = self.detect_changes(pass_start.name, repo, runtime_home)
            self.assertEqual(probe.returncode, 0, "LEDGER_CAPTURED_AS_A_GAP: " + probe.stderr[-800:])
            analysis = repo_context_forge.json.loads(probe.stdout)["analysis"]
            self.assertEqual(
                (analysis.get("status"),
                 [gap.get("path") for gap in analysis.get("gaps") or []]),
                ("complete", []),
                "LEDGER_CAPTURED_AS_A_GAP")

            changed = {
                line[3:] for line in
                repo_context_forge.porcelain_status(repo).splitlines() if line[3:]
            }
            self.assertEqual(changed, {"src/a.py"}, "SOURCE_CHECKOUT_WRITTEN: " + str(sorted(changed)))
            self.assertFalse(
                (repo / repo_context_forge.workflow_index.INDEX_DIR).exists(),
                "SOURCE_CHECKOUT_WRITTEN: the producer wrote its ledger into the source")
            self.assertEqual(
                source_exclude.read_bytes() if source_exclude.exists() else b"",
                exclude_before, "SOURCE_CHECKOUT_WRITTEN")

            # Excluding the producer's own artifacts must not swallow a real
            # deletion. Assert against changed_symbols, not the whole payload: the
            # path also appears in gaps, so a string search would pass on a gap.
            def changed_paths(result):
                payload = repo_context_forge.json.loads(result.stdout)
                return [
                    symbol.get("filePath")
                    for symbol in payload.get("changed_symbols") or []
                ]

            self.assertIn(
                "src/a.py", changed_paths(probe),
                "TRACKED_DELETION_SWALLOWED: the edited symbol was never attributed")
            self.assertIn(
                "compute",
                [symbol.get("name") for symbol in
                 repo_context_forge.json.loads(probe.stdout).get("changed_symbols") or []],
                "TRACKED_DELETION_SWALLOWED: compute was never attributed")

            (repo / "src" / "a.py").unlink()
            deletion = self.detect_changes(pass_start.name, repo, runtime_home)
            self.assertEqual(deletion.returncode, 0, deletion.stderr[-800:])
            self.assertIn(
                "src/a.py", changed_paths(deletion), "TRACKED_DELETION_SWALLOWED")

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

    def run_public_bootstrap(
        self, repo: Path, cache_dir: str, runtime_home: str, *,
        mode: str, intent: str | None = None, base: str | None = None,
        top: int = 1, gitnexus_mode: str = "auto", candidate_slot: bool = False,
    ):
        packet_path = Path(runtime_home) / "packet.json"
        packet_path.unlink(missing_ok=True)
        command = [
            sys.executable, str(ROOT / "scripts" / "codex_context_bootstrap.py"),
            "--repo", str(repo), "--mode", mode, "--top", str(top),
            "--cache-dir", cache_dir, "--map-build", "never",
            "--gitnexus-mode", gitnexus_mode, "--enforce-intake",
            "--packet-json-out", str(packet_path),
        ]
        if candidate_slot:
            command.append("--candidate-slot")
        if intent is not None:
            command.extend(("--intent", intent))
        if base is not None:
            command.extend(("--base", base, "--allow-stale-pr-head"))
        result = repo_context_forge.run_cmd(
            command, env={**os.environ, "HOME": runtime_home}, allow_fail=True)
        packet = repo_context_forge.json.loads(
            packet_path.read_text(encoding="utf-8")) if packet_path.exists() else {}
        return result, packet

    def run_public_intent_bootstrap(
        self, repo: Path, cache_dir: str, runtime_home: str, *,
        intent: str = "Update src/a.py", top: int = 1, gitnexus_mode: str = "auto",
        candidate_slot: bool = False,
    ):
        return self.run_public_bootstrap(
            repo, cache_dir, runtime_home, mode="intent", intent=intent, top=top,
            gitnexus_mode=gitnexus_mode, candidate_slot=candidate_slot)

    def run_public_intent_bootstrap_after(
        self, repo, cache_dir, runtime_home, ready, mutate, cleanup=None):
        stop = threading.Event()
        changed = threading.Event()
        def watch() -> None:
            deadline = time.monotonic() + 30
            while not stop.is_set() and time.monotonic() < deadline:
                if ready():
                    mutate()
                    changed.set()
                    return
                time.sleep(0.0005)
        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
        finally:
            stop.set()
            watcher.join()
            if cleanup is not None:
                cleanup()
        return changed.is_set(), result, packet

    def assert_public_intent_gap(self, intent: str, gap: dict[str, object], marker: str, *, blocks: bool = True) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home, intent=intent, top=1)
        self.assertEqual((result.returncode == 0, packet["coverage_gaps"], packet["gitnexus"]["required_checks_resolved"]), (not blocks, [gap], not blocks), marker)

    def run_off_mode_bootstrap(
        self,
        repo: Path,
        cache_dir: str,
        runtime_home: str,
    ):
        return self.run_public_intent_bootstrap(
            repo,
            cache_dir,
            runtime_home,
            gitnexus_mode="off",
        )

    @contextmanager
    def public_intent_repo(self):
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            yield repo, cache_dir, runtime_home

    def assert_public_intent_symbol_checked(
        self,
        *,
        file_name: str,
        helper_count: int,
        symbol_name: str,
        intent: str,
        failure_marker: str | None = None,
    ) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            relative_path = f"src/{file_name}"
            (repo / relative_path).write_text(
                "".join(
                    f"def helper_{index}():\n    return {index}\n\n"
                    for index in range(helper_count)
                )
                + f"class {symbol_name}:\n    pass\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "intent symbol target"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent=intent,
                top=1,
            )

            self.assertEqual(result.returncode, 0, failure_marker or result.stdout or result.stderr)
            checks = {
                (entry["kind"], entry["file"], entry["target"])
                for entry in packet["gitnexus"]["analysis"]["entries"]
            }
            expected_checks = {
                ("symbol_context", relative_path, symbol_name),
                ("symbol_impact", relative_path, symbol_name),
            }
            self.assertTrue(expected_checks <= checks, failure_marker)

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

    def test_run_cmd_file_capture_preserves_large_process_output(self) -> None:
        self.assertIsNotNone(shutil.which("node"), "GitNexus Node runtime is required")
        expected_bytes = 72_459

        result = repo_context_forge.run_cmd(
            [
                "node",
                "-e",
                f'process.stdout.write("x".repeat({expected_bytes})); process.exit(0)',
            ],
            capture_output_to_file=True,
        )

        self.assertEqual(len(result.stdout.encode()), expected_bytes)

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
        plan, omitted_checks, omitted_required = repo_context_forge.build_gitnexus_plan(
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
        self.assertEqual(omitted_required, [])

    def test_build_gitnexus_plan_never_splits_context_impact_pair_at_cap(self) -> None:
        entries = [{"path": "README.md", "symbols": []}]
        entries.extend(
            {
                "path": f"src/module_{index}.py",
                "symbols": [{"name": f"handle_{index}", "kind": "function"}],
            }
            for index in range(10)
        )

        plan, omitted_checks, omitted_required = repo_context_forge.build_gitnexus_plan(entries)

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
        self.assertEqual(omitted_required, [])

        file_plan, omitted_file_checks, omitted_required = repo_context_forge.build_gitnexus_plan(
            [{"path": f"docs/{index}.md", "symbols": []} for index in range(21)]
        )
        self.assertEqual(len(file_plan), repo_context_forge.MAX_GITNEXUS_CHECKS)
        self.assertEqual(omitted_file_checks, 1)
        self.assertEqual(omitted_required, [])

        required_names = [f"Anchor{index}" for index in range(13)]
        required_symbols = [
            {"name": name, "kind": "class"} for name in required_names[:12]
        ]
        _, omitted_checks, omitted_required = repo_context_forge.build_gitnexus_plan(
            [{"path": "src/deep.py", "symbols": required_symbols, "intent_required_symbols": required_names}],
            intent_mode=True,
        )
        self.assertEqual(omitted_checks, 0)
        self.assertEqual(
            {item["target"] for item in omitted_required},
            set(required_names[10:]),
        )

    def test_required_graph_groups_allocate_before_optional_breadth(self) -> None:
        required_names = [f"Required{index}" for index in range(10)]
        plan, omitted_count, omitted_required = repo_context_forge.build_gitnexus_plan(
            [
                {
                    "path": "src/required.py",
                    "symbols": [
                        {"name": name, "kind": "function"}
                        for name in [*required_names, "OptionalAnchor"]
                    ],
                    "intent_required_file": True,
                    "intent_required_symbols": required_names,
                }
            ],
            intent_mode=True,
        )

        self.assertEqual(
            (
                "OptionalAnchor" in {str(item["target"]) for item in plan},
                {str(item["target"]) for item in omitted_required},
                omitted_count,
            ),
            (False, {required_names[-1]}, 2),
            "OPTIONAL_CHECK_PREEMPTED_REQUIRED_COVERAGE",
        )

    def test_public_bootstrap_prioritizes_explicit_database_anchor(self) -> None:
        self.assert_public_intent_symbol_checked(
            file_name="db.py",
            helper_count=13,
            symbol_name="Database",
            intent="Update sqlite_utils.Database import checkpoints in src/db.py",
            failure_marker="QUALIFIED_SYMBOL_REPORTED_AS_ABSENT_FILE",
        )

    def test_public_bootstrap_prioritizes_explicit_symbol_after_eightieth_symbol(self) -> None:
        self.assert_public_intent_symbol_checked(
            file_name="deep.py",
            helper_count=81,
            symbol_name="DeepAnchor",
            intent="Update pkg.DeepAnchor in src/deep.py",
        )

    def test_public_bootstrap_prioritizes_unique_unqualified_symbol_beyond_display_slice(
        self,
    ) -> None:
        for symbol_name, intent, marker in (
            ("DeepAnchor", "Update DeepAnchor behavior", "REQUIRED_DEEP_SYMBOL_PAIR_OMITTED"),
            ("URL", "Update src/a.py and URL behavior", "UPPERCASE_SYMBOL_REPORTED_RESOLVED"),
        ):
            self.assert_public_intent_symbol_checked(
                file_name="deep.py", helper_count=13, symbol_name=symbol_name,
                intent=intent, failure_marker=marker,
            )

    def test_public_bootstrap_does_not_treat_prose_or_filenames_as_symbols(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "words.py").write_text("def item():\n    return None\n\ndef evidence():\n    return None\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "prose names"])
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home, intent="Update workflow_documents.py so each item retains evidence under CLAUDE.md for RED PR JSON output, premise/occurrence checks, and https://example.com/missing.py", top=1)
        references = {gap.get("reference") for gap in packet.get("coverage_gaps", [])}
        self.assertTrue(result.returncode == 0 and references.isdisjoint({"workflow_documents.py", "CLAUDE.md", "item", "evidence", "RED", "PR", "JSON", "example.com/missing.py"}), "ROOT_PROSE_REPORTED_ABSENT_FILE")
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "delete.py").write_text("def delete():\n    return 1\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "delete subject"])
            for subject_intent in ("Harden the delete flow", "Add support for delete behavior"):
                result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home, intent=subject_intent, top=1)
                self.assertEqual(
                    (result.returncode, {gap.get("kind") for gap in packet.get("coverage_gaps", [])} & {"no_relevant_seam"}, "src/delete.py" in {item["path"] for item in packet["targets"]}),
                    (0, set(), True),
                    "OPERATOR_SUBJECT_INTENT_FALSELY_BLOCKED",
                )

    def test_public_bootstrap_blocks_intent_without_relevant_seam(self) -> None:
        intent = "Update Frobnicator behavior"
        self.assert_public_intent_gap(intent, {"kind": "no_relevant_seam", "reference": intent, "candidates": []}, "BM_R2_NO_RELEVANT_SEAM_CONTRACT_FAILED")
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "widget.py").write_text("def add_widget():\n    return 1\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "widget.py"])
            repo_context_forge.run_git(repo, ["commit", "-m", "widget"])
            for creation_intent in ("Add a new FutureAnchor", "Create a new validation function"):
                result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home, intent=creation_intent, top=1)
                self.assertEqual(
                    (result.returncode == 0, packet["coverage_gaps"], "widget.py" in {item["path"] for item in packet["targets"]}),
                    (False, [{"kind": "no_relevant_seam", "reference": creation_intent, "candidates": []}], False),
                    "PROSE_TERM_RELEVANCE_RESOLVED_FALSE_SEAM",
                )

    def test_public_bootstrap_reports_absent_root_file(self) -> None:
        for path in ("workflow_documents.py", "CLAUDE.md"):
            self.assert_public_intent_gap(f"Update {path} behavior", {"kind": "absent_file", "reference": path, "candidates": []}, "ROOT_EXACT_FILE_NOT_ABSENT_FILE", blocks=False)

    def test_public_bootstrap_reports_backticked_absent_root_file(self) -> None:
        self.assert_public_intent_gap("Update `missing.py` behavior", {"kind": "absent_file", "reference": "missing.py", "candidates": []}, "BACKTICK_ROOT_FILE_NOT_ABSENT_FILE", blocks=False)

    def test_public_bootstrap_reports_hidden_absent_root_file(self) -> None:
        self.assert_public_intent_gap("Update `.env` behavior", {"kind": "absent_file", "reference": ".env", "candidates": []}, "HIDDEN_ROOT_FILE_NOT_ABSENT_FILE", blocks=False)

    def test_public_bootstrap_reports_punctuated_absent_root_file(self) -> None:
        self.assert_public_intent_gap("Update missing.py.", {"kind": "absent_file", "reference": "missing.py", "candidates": []}, "PUNCTUATED_ROOT_FILE_NOT_ABSENT_FILE", blocks=False)

    def test_public_bootstrap_does_not_treat_ambiguous_slash_as_file(self) -> None:
        for intent in ("Update premise/occurrence checks", "Update missing/tool behavior", "Update missing/Makefile behavior"):
            self.assert_public_intent_gap(intent, {"kind": "no_relevant_seam", "reference": intent, "candidates": []}, "AMBIGUOUS_UNQUOTED_SLASH_REPORTED_ABSENT_FILE")

    def test_public_bootstrap_does_not_reject_missing_creation_path(self) -> None:
        for intent in ("Add new.py", "Create new.py", "Add src/new.py", "Create src/new.py", "Add `new.py`", "Create `new.py`", "Add `.env`", "Create `.env`", "Add `src/new.py`", "Create `src/new.py`"):
            with self.subTest(intent=intent), self.public_intent_repo() as (repo, cache_dir, runtime_home):
                _result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home, intent=intent, top=1)
            self.assertFalse(any(gap.get("kind") == "absent_file" for gap in packet["coverage_gaps"]), "CREATION_PATH_REPORTED_ABSENT_FILE")

    def test_public_bootstrap_does_not_block_on_an_absent_intent_path(self) -> None:
        marker = "ABSENT_INTENT_PATH_BLOCKS"
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home, intent="Update src/missing.py behavior", top=1)
        planned = {item["path"] for item in packet["targets"]} | {item["file"] for item in packet["gitnexus_plan"]}
        self.assertEqual(
            (result.returncode, [gap["kind"] for gap in packet["coverage_gaps"]], packet["gitnexus"]["required_checks_resolved"], "src/missing.py" in planned),
            (0, ["absent_file"], True, False),
            marker + ": " + (result.stderr[-400:] if result.returncode else ""),
        )

    def test_public_bootstrap_reports_absent_exact_file(self) -> None:
        for reference, path, marker in (("src/missing.py", "src/missing.py", "ABSENT_EXACT_FILE_REPORTED_RESOLVED"), ("src/Makefile", "src/Makefile", "EXTENSIONLESS_ABSENT_FILE_REPORTED_RESOLVED"), ("missing/missing.py", "missing/missing.py", "MISSING_PARENT_ABSENT_FILE_REPORTED_OTHER_GAP"), ("missing/missing.py.", "missing/missing.py", "PUNCTUATED_MISSING_FILE_REPORTED_OTHER_GAP"), ("`missing/missing.py`", "missing/missing.py", "EXPLICIT_DOTTED_ABSENT_FILE_REPORTED_OTHER_GAP"), ("`missing/tool`", "missing/tool", "EXPLICIT_LOWERCASE_ABSENT_FILE_REPORTED_OTHER_GAP"), ("`missing/Makefile`", "missing/Makefile", "EXPLICIT_EXTENSIONLESS_ABSENT_FILE_REPORTED_OTHER_GAP")):
            self.assert_public_intent_gap(f"Update {reference} behavior", {"kind": "absent_file", "reference": path, "candidates": []}, marker, blocks=False)

    def test_public_bootstrap_requires_all_explicit_replay_symbols(self) -> None:
        required_names = {
            "_finding_dispositions",
            "advisor_disposition",
            "advisor_disposition_document",
            "_apply_finding_dispositions",
            "tree_manifest",
        }
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for index, name in enumerate(sorted(required_names)):
                (repo / "src" / f"owner_{index}.py").write_text(
                    f"def {name}():\n    return None\n",
                    encoding="utf-8",
                )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "replay seams"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent="Update " + " ".join(sorted(required_names)),
                top=1,
            )

        planned = {
            item["target"]
            for item in packet.get("gitnexus_plan", [])
            if item.get("required") is True
        }
        self.assertTrue(
            result.returncode == 0 and required_names <= planned,
            "PRESERVED_REPLAY_REQUIRED_SEAMS_OMITTED",
        )

    def test_public_bootstrap_planner_is_independent_of_display_slice(self) -> None:
        self.assert_public_intent_symbol_checked(
            file_name="display.py",
            helper_count=13,
            symbol_name="DatabaseAnchor",
            intent="Improve database persistence behavior",
            failure_marker="DISPLAY_LIMIT_CHANGED_GRAPH_PLAN",
        )

    def test_blocker_packet_emits_advisor_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            self.make_git_repo(repo)
            packet = repo_context_forge.make_blocker_packet(
                repo, reason="measured blocker", base_ref="HEAD", head_ref="HEAD")
        projection = packet.get("advisorProjection", {})
        self.assertTrue(
            projection.get("expectedCandidateTree")
            == {"gap": "expected_candidate_tree_unavailable"}
            and {"kind": "expected_candidate_tree_unavailable"}
            in projection.get("coverageGaps", []),
            "BLOCKER_EXPECTED_TREE_GAP_MISSING",
        )

    def test_public_bootstrap_emits_explicit_off_mode_projection(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_off_mode_bootstrap(repo, cache_dir, runtime_home)
        projection = packet["advisorProjection"]
        self.assertEqual(
            (result.returncode, projection.get("indexedCandidateTree"),
             projection.get("graph", {}).get("status"), "gitnexus" in packet),
            (0, {"gap": "indexed_candidate_tree_unavailable"}, "disabled", True),
            "OFF_MODE_PROJECTION_INVENTED_INDEXED_TREE",
        )

    def test_public_bootstrap_emits_canonical_projection_provenance(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            repo_context_forge.run_git(
                repo, ["remote", "add", "origin", "git@github.com:future3OOO/example.git"])
            result, packet = self.run_off_mode_bootstrap(repo, cache_dir, runtime_home)
            expected_base = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
        projection = packet["advisorProjection"]
        self.assertEqual(
            (result.returncode, projection.get("sourceRepo"),
             projection.get("sourceBaseOid")),
            (0, "github.com/future3OOO/example", expected_base),
            "PROJECTION_PROVENANCE_WAS_NONCANONICAL",
        )

    def test_public_bootstrap_provenance_preserves_remote_port(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            repo_context_forge.run_git(
                repo, ["remote", "add", "origin", "ssh://git@example.com:8443/org/repo.git"])
            first_result, first_packet = self.run_off_mode_bootstrap(
                repo, cache_dir, runtime_home)
            repo_context_forge.run_git(
                repo, ["remote", "set-url", "origin", "ssh://git@example.com:9443/org/repo.git"])
            second_result, second_packet = self.run_off_mode_bootstrap(
                repo, cache_dir, runtime_home)
            repo_context_forge.run_git(
                repo, ["remote", "set-url", "origin",
                       "https://user@example.com:8443/org/repo.git"])
            third_result, third_packet = self.run_off_mode_bootstrap(
                repo, cache_dir, runtime_home)
            repo_context_forge.run_git(
                repo, ["remote", "set-url", "origin",
                       "ssh://git@[2001:db8::1]:8443/org/repo.git"])
            ipv6_result, ipv6_packet = self.run_off_mode_bootstrap(
                repo, cache_dir, runtime_home)
            repo_context_forge.run_git(
                repo, ["remote", "set-url", "origin",
                       "https://example.com:notaport/org/repo.git"])
            bad_port_result, bad_port_packet = self.run_off_mode_bootstrap(
                repo, cache_dir, runtime_home)
        first_tree = first_packet["target_state"].get("candidate_tree")
        self.assertEqual(
            (
                first_result.returncode, second_result.returncode, third_result.returncode,
                ipv6_result.returncode, bad_port_result.returncode,
                first_packet["advisorProjection"].get("sourceRepo"),
                second_packet["advisorProjection"].get("sourceRepo"),
                third_packet["advisorProjection"].get("sourceRepo"),
                ipv6_packet["advisorProjection"].get("sourceRepo"),
                bad_port_packet["advisorProjection"].get("sourceRepo"),
                bool(first_tree),
                second_packet["target_state"].get("candidate_tree") == first_tree,
            ),
            (
                0, 0, 0,
                0, 0,
                "example.com:8443/org/repo",
                "example.com:9443/org/repo",
                "example.com:8443/org/repo",
                "[2001:db8::1]:8443/org/repo",
                {"gap": "source_repo_unavailable"},
                True,
                True,
            ),
            "PORT_DISTINGUISHED_REMOTES_COLLAPSED_TO_ONE_PROVENANCE",
        )

    def test_public_bootstrap_emits_bounded_advisor_projection_v1(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "projection.py").write_text(
                "class ProjectionAnchor:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "projection anchor"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home,
                intent="Update ProjectionAnchor behavior", top=1)
        projection = packet["advisorProjection"]
        expected_tree = packet["target_state"].get("candidate_tree")
        self.assertEqual(
            (result.returncode, projection.get("schemaVersion"),
             projection.get("expectedCandidateTree"),
             projection.get("indexedCandidateTree")),
            (0, 1, expected_tree, expected_tree),
            "ADVISOR_PROJECTION_V1_CONTRACT_BROKEN",
        )
        graph_references = projection.get("graph", {}).get("references", [])
        packet_references = {
            entry.get("reference")
            for entry in packet["gitnexus"]["analysis"]["entries"]
        }
        self.assertTrue(
            graph_references and set(graph_references) <= packet_references,
            "ADVISOR_PROJECTION_V1_CONTRACT_BROKEN",
        )

    def test_public_bootstrap_pr_mode_ignores_unrelated_source_dirt(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "src/a.py"])
            repo_context_forge.run_git(repo, ["commit", "-m", "pr change"])
            head_tree = repo_context_forge.run_git(repo, ["rev-parse", "HEAD^{tree}"])
            (repo / "local-only.txt").write_text("dirty\n", encoding="utf-8")
            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="pr", base="HEAD~1")
        self.assertTrue(
            result.returncode == 0
            and packet["advisorProjection"]["expectedCandidateTree"]
            == packet["advisorProjection"]["indexedCandidateTree"] == head_tree,
            "PR_DIRTY_SOURCE_CANDIDATE_REJECTED",
        )

    def test_public_bootstrap_repo_mode_ignores_unrelated_source_dirt(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            head_tree = repo_context_forge.run_git(repo, ["rev-parse", "HEAD^{tree}"])
            (repo / "local-only.txt").write_text("dirty\n", encoding="utf-8")
            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="repo")
            shutil.rmtree(repo)
            failed_result, failed_packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="repo")
        self.assertTrue(
            result.returncode == 0
            and packet["advisorProjection"]["expectedCandidateTree"]
            == packet["advisorProjection"]["indexedCandidateTree"] == head_tree,
            "REPO_DIRTY_SOURCE_CANDIDATE_REJECTED",
        )
        self.assertEqual(
            (failed_result.returncode != 0, failed_packet),
            (True, {}),
            "FAILED_BOOTSTRAP_RETURNED_STALE_PACKET",
        )

    def test_public_bootstrap_preserves_offsets_after_backtick_spans(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for name in ("one", "two"):
                (repo / "src" / f"{name}.py").write_text(
                    "class DeepAnchor:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "duplicate symbols"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home,
                intent="Review `not code` then update one.DeepAnchor behavior", top=2)
        required_checks = {
            (entry["kind"], entry["file"], entry["target"])
            for entry in packet["gitnexus"]["analysis"]["entries"]
        }
        self.assertTrue(
            result.returncode == 0
            and packet["gitnexus"]["required_checks_resolved"]
            and {
                ("symbol_context", "src/one.py", "DeepAnchor"),
                ("symbol_impact", "src/one.py", "DeepAnchor"),
            } <= required_checks
            and not any(
                gap.get("reference") == "DeepAnchor"
                for gap in packet["coverage_gaps"]
            ),
            "BACKTICK_OFFSET_AMBIGUITY_REPORTED",
        )

    def test_public_bootstrap_refreshes_second_dirty_candidate_without_manual_cleanup(
        self,
    ) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            source = repo / "src" / "a.py"
            trees = []
            for symbol_name in ("FirstDirtyAnchor", "SecondDirtyAnchor"):
                source.write_text(
                    f"class {symbol_name}:\n    pass\n",
                    encoding="utf-8",
                )
                result, packet = self.run_public_intent_bootstrap(
                    repo,
                    cache_dir,
                    runtime_home,
                    intent=f"Update {symbol_name} behavior",
                    top=1,
                )
                expected_tree = packet["target_state"].get("candidate_tree")
                checks = {
                    (entry["kind"], entry["target"])
                    for entry in packet["gitnexus"]["analysis"]["entries"]
                }
                expected_checks = {
                    ("symbol_context", symbol_name),
                    ("symbol_impact", symbol_name),
                }
                self.assertEqual(
                    (
                        result.returncode,
                        packet["gitnexus"].get("indexed_candidate_tree"),
                        expected_checks <= checks,
                    ),
                    (0, expected_tree, True),
                    "SECOND_DIRTY_EDIT_REUSED_STALE_GRAPH",
                )
                trees.append(expected_tree)

            self.assertNotEqual(trees[0], trees[1], "SECOND_DIRTY_EDIT_REUSED_STALE_GRAPH")
            unchanged_result, unchanged_packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update SecondDirtyAnchor behavior", top=1)
            self.assertEqual(
                (
                    unchanged_result.returncode,
                    unchanged_packet["gitnexus"].get("status"),
                ),
                (0, "fresh"),
                "SECOND_DIRTY_EDIT_REUSED_STALE_GRAPH",
            )

    def test_public_bootstrap_binds_index_generation_to_candidate_tree(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "indexed.py").write_text(
                "class IndexedAnchor:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "indexed anchor"])

            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update IndexedAnchor behavior", top=1)
            expected_tree = packet["target_state"].get("candidate_tree")
            self.assertEqual(
                (
                    result.returncode,
                    packet["gitnexus"].get("indexed_candidate_tree"),
                    bool(packet["gitnexus"].get("index_generation")),
                ),
                (0, expected_tree, True),
                "PACKET_NESTED_REFRESH_REGRESSED\n"
                f"gitnexus={packet.get('gitnexus')}\n"
                f"stdout={result.stdout}\nstderr={result.stderr}",
            )

    def test_public_bootstrap_blocks_mismatched_index_generation(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home)
            registry_path = Path(runtime_home) / ".gitnexus" / "registry.json"
            registry = repo_context_forge.json.loads(
                registry_path.read_text(encoding="utf-8"))
            analysis_repo = packet["target_state"]["analysis_repo"]
            entry = next(item for item in registry if item["path"] == analysis_repo)
            entry["indexedAt"] = "mismatched-generation"
            registry_path.write_text(
                repo_context_forge.json.dumps(registry), encoding="utf-8")

            blocked_result, blocked_packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, gitnexus_mode="check")

            self.assertTrue(
                result.returncode == 0
                and packet["gitnexus"].get("index_generation")
                and blocked_result.returncode != 0
                and blocked_packet["gitnexus"].get("status") == "blocked"
                and not blocked_packet["gitnexus"].get("index_generation")
                and not blocked_packet["gitnexus"].get("indexed_candidate_tree")
                and not blocked_packet["gitnexus"]["required_checks_resolved"],
                "INDEX_GENERATION_BINDING_REGRESSED",
            )

        for file_name in ("meta.json", "repo-context-forge-receipt.json"):
            with self.subTest(file_name=file_name), self.public_intent_repo() as (repo, cache_dir, runtime_home):
                result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
                index_file = Path(packet["target_state"]["analysis_repo"]) / ".gitnexus" / file_name
                index_file.write_bytes(b"\xff")
                packet_path = Path(runtime_home) / "packet.json"
                packet_path.unlink()
                check_result, check_packet = self.run_public_intent_bootstrap(
                    repo, cache_dir, runtime_home, gitnexus_mode="check")
                packet_path.unlink(missing_ok=True)
                auto_result, auto_packet = self.run_public_intent_bootstrap(
                    repo, cache_dir, runtime_home)
                self.assertEqual(
                    (result.returncode, check_result.returncode,
                     check_packet.get("gitnexus", {}).get("status"),
                     check_packet.get("gitnexus", {}).get("reindex_attempted"),
                     check_packet.get("gitnexus", {}).get("required_checks_resolved"),
                     auto_result.returncode, auto_packet.get("gitnexus", {}).get("status"),
                     auto_packet.get("gitnexus", {}).get("reindex_attempted"),
                     auto_packet.get("gitnexus", {}).get("required_checks_resolved"),
                     "UnicodeDecodeError" in check_result.stderr + auto_result.stderr),
                    (0, 1, "blocked", False, False, 0, "reindexed", True, True, False),
                    "INVALID_UTF8_GRAPH_STATE_CRASHED_BOOTSTRAP")

    def test_public_bootstrap_preserves_registry_failure_diagnostic_during_reindex(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            registry_path = Path(runtime_home) / ".gitnexus" / "registry.json"
            def registry_ready() -> bool:
                try:
                    return bool(repo_context_forge.json.loads(
                        registry_path.read_text(encoding="utf-8")))
                except (OSError, repo_context_forge.json.JSONDecodeError):
                    return False
            changed, result, packet = self.run_public_intent_bootstrap_after(
                repo, cache_dir, runtime_home, registry_ready,
                lambda: registry_path.write_text("{invalid-json", encoding="utf-8"))
            warning = str(packet.get("gitnexus", {}).get("warning") or "")
            self.assertEqual(
                (changed, result.returncode != 0, packet.get("gitnexus", {}).get("status"),
                 "registry is not valid JSON" in warning, "already running" in warning),
                (True, True, "blocked", True, False),
                "REGISTRY_ERROR_MISLABELED_AS_LOCK_CONTENTION")

    def test_public_bootstrap_blocks_receipt_write_failure(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            analysis_repo, _key = repo_context_forge.analysis_checkout_path(
                repo, repo_context_forge.run_git(repo, ["rev-parse", "HEAD"]),
                Path(cache_dir), "intent")
            index_dir = analysis_repo / ".gitnexus"
            receipt_path = index_dir / "repo-context-forge-receipt.json"
            changed, result, packet = self.run_public_intent_bootstrap_after(
                repo, cache_dir, runtime_home,
                lambda: (index_dir / "meta.json").is_file() and not receipt_path.exists(),
                lambda: index_dir.chmod(0o555),
                lambda: index_dir.chmod(0o755) if index_dir.exists() else None)
            warning = str(packet.get("gitnexus", {}).get("warning") or "")
            self.assertEqual(
                (changed, result.returncode != 0, packet.get("gitnexus", {}).get("status"),
                 packet.get("gitnexus", {}).get("required_checks_resolved"),
                 "receipt publication failed" in warning, receipt_path.exists()),
                (True, True, "blocked", False, True, False),
                "RECEIPT_WRITE_FAILURE_ESCAPED_WITHOUT_BLOCKER_PACKET")

        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            _result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
            receipt_path = Path(packet["target_state"]["analysis_repo"]) / ".gitnexus" / "repo-context-forge-receipt.json"
            receipt_path.unlink()
            receipt_path.mkdir()
            (Path(runtime_home) / "packet.json").unlink()
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
            self.assertEqual(
                (result.returncode != 0, packet.get("gitnexus", {}).get("status"),
                 packet.get("gitnexus", {}).get("required_checks_resolved"),
                 "receipt publication failed" in str(packet.get("gitnexus", {}).get("warning") or ""),
                 "Traceback" in result.stderr),
                (True, "blocked", False, True, False),
                "RECEIPT_REPLACE_FAILURE_ESCAPED_WITHOUT_BLOCKER_PACKET")

        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            victim = Path(runtime_home) / "victim.txt"
            victim.write_text("PRESERVE_ME\n", encoding="utf-8")
            (repo / ".gitnexus").mkdir()
            (repo / ".gitnexus" / "repo-context-forge-receipt.tmp").symlink_to(victim)
            repo_context_forge.run_git(
                repo, ["add", "-f", ".gitnexus/repo-context-forge-receipt.tmp"])
            repo_context_forge.run_git(repo, ["commit", "-m", "tracked receipt symlink"])
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
            self.assertEqual(
                (result.returncode, packet["gitnexus"]["required_checks_resolved"],
                 victim.read_text(encoding="utf-8")),
                (0, True, "PRESERVE_ME\n"),
                "RECEIPT_TEMP_SYMLINK_OVERWROTE_EXTERNAL_TARGET")

        with self.subTest("tracked index dir symlink"), self.public_intent_repo() as (
                repo, cache_dir, runtime_home):
            victim_dir = Path(runtime_home) / "victim-dir"
            victim_dir.mkdir()
            (repo / ".gitnexus").symlink_to(victim_dir)
            repo_context_forge.run_git(repo, ["add", "-f", ".gitnexus"])
            repo_context_forge.run_git(repo, ["commit", "-m", "tracked index dir symlink"])
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
            self.assertEqual(
                (result.returncode, packet["gitnexus"]["required_checks_resolved"],
                 sorted(path.name for path in victim_dir.iterdir())),
                (0, True, []),
                "INDEX_DIR_SYMLINK_ESCAPED_CACHE_CONFINEMENT")

    def test_public_bootstrap_blocks_candidate_transaction_contention(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            head = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
            analysis_repo, _key = repo_context_forge.analysis_checkout_path(
                repo, head, Path(cache_dir), "local")
            lock_path = repo_context_forge.gitnexus_analysis.analysis_transaction_path(
                analysis_repo)
            lock_path.parent.mkdir(parents=True)
            with lock_path.open("a") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result, _packet = self.run_off_mode_bootstrap(
                    repo, cache_dir, runtime_home
                )

            self.assertNotEqual(result.returncode, 0, "PACKET_CONTENTION_REGRESSED")
            self.assertIn("candidate analysis transaction is already running", result.stderr,
                          "PACKET_CONTENTION_REGRESSED")

    def candidate_lock_run(self, slot_for_lock: bool, repo, cache_dir, runtime_home):
        """Hold the real lock for one slot, then run a candidate bootstrap."""
        head = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
        analysis_repo, _key = repo_context_forge.analysis_checkout_path(
            repo, head, Path(cache_dir), "local", candidate_slot=slot_for_lock)
        lock_path = repo_context_forge.gitnexus_analysis.analysis_transaction_path(
            analysis_repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, candidate_slot=True)

    def test_a_held_candidate_lock_blocks_a_candidate_run(self) -> None:
        marker = "CANDIDATE_WORK_UNPROTECTED_BY_ITS_OWN_LOCK"
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, _packet = self.candidate_lock_run(True, repo, cache_dir, runtime_home)
            self.assertNotEqual(result.returncode, 0, marker)
            self.assertIn("candidate analysis transaction is already running",
                          result.stderr, marker)

    def test_a_held_ordinary_lock_does_not_block_a_candidate_run(self) -> None:
        marker = "ORDINARY_LOCK_SERIALIZES_THE_CANDIDATE_SLOT"
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, _packet = self.candidate_lock_run(False, repo, cache_dir, runtime_home)
            self.assertEqual(result.returncode, 0, marker + result.stderr[-400:])

    def test_one_candidate_slot_serves_every_mode_a_pass_reruns_in(self) -> None:
        """A pass reruns in a different mode as its tree goes clean to dirty. The
        candidate must be a different physical checkout from the one the pass
        started with, each mode must keep its own file selection, and switching
        mode must not mint a second candidate."""
        marker = "CANDIDATE_SLOT_NOT_SHARED_ACROSS_MODES"
        with tempfile.TemporaryDirectory() as root:
            repo, cache = Path(root) / "repo", Path(root) / "cache"
            repo.mkdir()
            self.make_git_repo(repo)
            head = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])

            pr_plain = repo_context_forge.resolve_target_state(
                repo, "pr", "HEAD", "HEAD", cache, False)
            pr_candidate = repo_context_forge.resolve_target_state(
                repo, "pr", "HEAD", "HEAD", cache, True)
            self.assertNotEqual(
                pr_candidate.analysis_repo, pr_plain.analysis_repo, marker)
            self.assertEqual(pr_candidate.mode, "pr", marker)

            local_candidate = repo_context_forge.resolve_target_state(
                repo, "local", "HEAD", "HEAD", cache, True)
            self.assertEqual(
                local_candidate.analysis_repo, pr_candidate.analysis_repo,
                marker + ": switching mode minted a second candidate")
            self.assertEqual(local_candidate.mode, "local", marker)

            candidates = [
                path for directory in repo_context_forge.CACHE_CHECKOUT_DIRS
                for path in (cache / directory).glob(f"{repo.name}-{head[:12]}-*")
                if path.resolve() == pr_candidate.analysis_repo
            ]
            self.assertEqual(len(candidates), 1, marker + f": {candidates}")

            # The lock the decorator would take names that one candidate checkout.
            self.assertEqual(
                repo_context_forge.gitnexus_analysis.analysis_transaction_path(
                    pr_candidate.analysis_repo),
                repo_context_forge.gitnexus_analysis.analysis_transaction_path(
                    local_candidate.analysis_repo),
                marker + ": the two modes would lock different candidate paths")

    def test_standalone_refresh_contends_with_packet_transaction(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home)
            state = packet["target_state"]
            analysis_repo = Path(state["analysis_repo"])
            (analysis_repo / ".gitnexus" / "meta.json").unlink()
            lock_path = (
                repo_context_forge.gitnexus_analysis.analysis_transaction_path(
                    analysis_repo))
            with lock_path.open("a") as lock_file, patch.dict(
                os.environ, {"HOME": runtime_home}
            ):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                status = repo_context_forge.gitnexus_analysis.ensure_index(
                    analysis_repo, state["head_sha"], packet["gitnexus"]["repo"],
                    "auto", run_command=repo_context_forge.run_cmd,
                    expected_candidate_tree=state["candidate_tree"],
                    registry_path=Path(runtime_home) / ".gitnexus" / "registry.json",
                )
            self.assertTrue(
                result.returncode == 0
                and status.get("status") == "blocked"
                and status.get("reindex_attempted") is False,
                "STANDALONE_REINDEX_BYPASSED_PACKET_LOCK",
            )
            self.assertNotIn("transaction_held", repo_context_forge.gitnexus_analysis.ensure_index.__code__.co_varnames, "CALLER_ASSERTED_TRANSACTION_OWNERSHIP")

    def test_transaction_context_expires_in_inherited_task(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
            state = packet["target_state"]
            analysis_repo = Path(state["analysis_repo"])
            lock_path = repo_context_forge.gitnexus_analysis.analysis_transaction_path(analysis_repo)
            async def scenario():
                ready, proceed = asyncio.Event(), asyncio.Event()
                async def refresh():
                    ready.set()
                    await proceed.wait()
                    return repo_context_forge.gitnexus_analysis.ensure_index(
                        analysis_repo, state["head_sha"], packet["gitnexus"]["repo"], "auto",
                        run_command=lambda args, **kwargs: repo_context_forge.run_cmd(
                            args, env={**os.environ, "HOME": runtime_home}, **kwargs),
                        expected_candidate_tree=state["candidate_tree"],
                        registry_path=Path(runtime_home) / ".gitnexus" / "registry.json")
                with repo_context_forge.gitnexus_analysis.analysis_transaction(lock_path):
                    task = asyncio.create_task(refresh())
                    await ready.wait()
                (analysis_repo / ".gitnexus" / "meta.json").unlink()
                with lock_path.open("a") as lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    proceed.set()
                    return await task
            status = asyncio.run(scenario())
        self.assertTrue(
            result.returncode == 0 and status.get("status") == "blocked"
            and status.get("reindex_attempted") is False,
            "CONTEXTVAR_TRANSACTION_OWNERSHIP_ESCAPED")

    def test_public_bootstrap_does_not_block_on_a_symbolless_intent_named_file(self) -> None:
        marker = "LOCKFILE_BLOCKS_GRAPH"
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            lockfile = repo / "package-lock.json"
            lockfile.write_text('{"name": "app", "lockfileVersion": 3, "packages": {}}\n', encoding="utf-8")
            repo_context_forge.run_cmd(["git", "add", "package-lock.json"], cwd=repo)
            repo_context_forge.run_cmd(["git", "commit", "-q", "-m", "lockfile"], cwd=repo)
            lockfile.write_text('{"name": "app", "lockfileVersion": 3, "packages": {"": {}}}\n', encoding="utf-8")

            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="local",
                intent="Bump the dependency recorded in package-lock.json", gitnexus_mode="auto")

            status = packet.get("gitnexus", {})
            self.assertEqual(
                (result.returncode, status.get("required_checks_resolved"), status.get("missing_required_symbols")),
                (0, True, []),
                marker + ": " + (result.stderr[-400:] if result.returncode else repo_context_forge.json.dumps(status)[:400]))

    def test_public_bootstrap_blocks_graph_time_candidate_mutation(self) -> None:
        marker = "GRAPH_TIME_CANDIDATE_MUTATION_VALIDATED_FOR_WRONG_REASON"
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            names = [f"MutationAnchor{index}" for index in range(8)]
            (repo / "src" / "a.py").write_text("".join(
                f"class {name}:\n    pass\n" for name in names), encoding="utf-8")
            head = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
            analysis_repo, _key = repo_context_forge.analysis_checkout_path(repo, head, Path(cache_dir), "intent")
            packet_path = Path(runtime_home) / "packet.json"
            command = [
                sys.executable, str(ROOT / "scripts" / "codex_context_bootstrap.py"),
                "--repo", str(repo), "--mode", "intent", "--intent", "Update " + " ".join(names),
                "--cache-dir", cache_dir, "--map-build", "never", "--gitnexus-mode", "auto",
                "--enforce-intake", "--packet-json-out", str(packet_path)]
            process = repo_context_forge.subprocess.Popen(
                command, env={**os.environ, "HOME": runtime_home}, stdout=repo_context_forge.subprocess.PIPE,
                stderr=repo_context_forge.subprocess.PIPE, text=True)
            deadline = time.monotonic() + 30

            def stop_graph_child() -> int | None:
                try:
                    children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
                except OSError:
                    return None
                for child in children:
                    try:
                        command = Path(f"/proc/{child}/cmdline").read_bytes().replace(b"\0", b" ")
                        if b"gitnexus" not in command or not (
                            b" context " in command or b" impact " in command):
                            continue
                        os.kill(int(child), signal.SIGSTOP)
                        if "State:\tT" in Path(f"/proc/{child}/status").read_text():
                            return int(child)
                        os.kill(int(child), signal.SIGCONT)
                    except OSError:
                        continue
                return None

            graph_child = None
            while graph_child is None:
                self.assertIsNone(process.poll(), marker)
                self.assertLess(time.monotonic(), deadline, marker)
                graph_child = stop_graph_child()
                if graph_child is None:
                    time.sleep(0.001)
            try:
                with (analysis_repo / "src" / "a.py").open("a", encoding="utf-8") as source:
                    source.write("MUTATED = True\n")
            finally:
                os.kill(graph_child, signal.SIGCONT)
            process.communicate(timeout=60)
            packet = repo_context_forge.json.loads(packet_path.read_text(encoding="utf-8"))
            analysis = packet["gitnexus"]["analysis"]
            self.assertEqual(
                (process.returncode != 0, packet["gitnexus"]["required_checks_resolved"],
                 analysis["graph_call_count"], [item["kind"] for item in analysis["unresolved_checks"]],
                 (analysis_repo / ".gitnexus" / "repo-context-forge-receipt.json").exists()),
                (True, False, 16, ["candidate_receipt"], False),
                marker)

    def test_public_bootstrap_candidate_tree_tracks_supported_git_shapes(self) -> None:
        def untracked(repo):
            (repo / "src" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
        def hidden(repo):
            (repo / ".runtime").mkdir()
            (repo / ".runtime" / "config.json").write_text("{}\n", encoding="utf-8")
        cases = {
            "untracked": untracked,
            "hidden": hidden,
            "deletion": lambda repo: (repo / "src" / "a.py").unlink(),
            "rename": lambda repo: (repo / "src" / "a.py").rename(repo / "src" / "b.py"),
            "executable": lambda repo: (repo / "src" / "a.py").chmod(0o755),
            "symlink": lambda repo: (repo / "src" / "link.py").symlink_to("a.py"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), self.public_intent_repo() as (repo, cache_dir, runtime_home):
                mutate(repo)
                status = repo_context_forge.porcelain_status(repo)
                index_tree = repo_context_forge.run_git(repo, ["write-tree"])
                expected_tree = repo_context_forge.candidate_tree(repo)
                result, packet = self.run_public_bootstrap(
                    repo, cache_dir, runtime_home, mode="local")
                projection = packet["advisorProjection"]
                self.assertEqual(
                    (result.returncode, projection["expectedCandidateTree"],
                     projection["indexedCandidateTree"],
                     packet["gitnexus"]["analysis"]["omitted_check_count"],
                     repo_context_forge.porcelain_status(repo),
                     repo_context_forge.run_git(repo, ["write-tree"])),
                    (0, expected_tree, expected_tree,
                     int(name in {"hidden", "deletion", "rename"}), status, index_tree),
                    "CANDIDATE_GRAPH_OMISSION_ERASED")

    def test_public_local_bootstrap_aligns_generated_candidate_exclusions(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            magic_cache = repo / ":(exclude)owned" / ".gitnexus"
            magic_cache.mkdir(parents=True)
            payload = magic_cache / "payload"
            payload.write_text("cache\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "pathspec cache"])
            (repo / "src" / "a.py").write_text("print('dirty')\n", encoding="utf-8")
            cache = repo / "src" / "__pycache__"
            cache.mkdir()
            (cache / "a.cpython-311.pyc").write_bytes(b"bytes")
            payload.unlink()
            expected_tree = repo_context_forge.candidate_tree(repo)
            payload.write_text("cache\n", encoding="utf-8")
            source_status = repo_context_forge.porcelain_status(repo)
            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="local")
            self.assertEqual(result.returncode, 0, "GENERATED_CANDIDATE_EXCLUSIONS_DIVERGED")
            projection = packet.get("advisorProjection", {})
            self.assertEqual(
                (projection.get("expectedCandidateTree"),
                 projection.get("indexedCandidateTree"),
                 repo_context_forge.porcelain_status(repo)),
                (expected_tree, expected_tree, source_status),
                "PATHSPEC_MAGIC_CACHE_CERTIFIED",
            )
            for variable in ("GIT_GLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"):
                with self.subTest(variable=variable), patch.dict(os.environ, {variable: "1"}):
                    result, packet = self.run_public_bootstrap(
                        repo, cache_dir, runtime_home, mode="local")
                projection = packet.get("advisorProjection", {})
                self.assertTrue(
                    result.returncode == 0
                    and projection.get("expectedCandidateTree") == expected_tree
                    and projection.get("indexedCandidateTree") == expected_tree
                    and repo_context_forge.porcelain_status(repo) == source_status,
                    "GLOBAL_PATHSPEC_ENV_REJECTED",
                )
        for directory in (".codex", ".gitnexus", ".repo-context-forge"):
            with self.subTest(directory=directory), self.public_intent_repo() as (
                repo, cache_dir, runtime_home
            ):
                (repo / ".gitignore").write_text(
                    f"*.log\n{directory}/\n", encoding="utf-8")
                expected_tree = repo_context_forge.candidate_tree(repo)
                source_status = repo_context_forge.porcelain_status(repo)
                result, packet = self.run_public_bootstrap(
                    repo, cache_dir, runtime_home, mode="local")
                projection = packet.get("advisorProjection", {})
                self.assertEqual(
                    (result.returncode, projection.get("expectedCandidateTree"),
                     projection.get("indexedCandidateTree"),
                     repo_context_forge.porcelain_status(repo)),
                    (0, expected_tree, expected_tree, source_status),
                    "LEGITIMATE_TOOL_CACHE_GITIGNORE_EDIT_DROPPED",
                )
        with self.subTest("tracked nested index symlink"), self.public_intent_repo() as (
                repo, cache_dir, runtime_home):
            victim = Path(runtime_home) / "victim.txt"
            victim.write_text("PRESERVE_ME\n", encoding="utf-8")
            (repo / ".gitnexus").mkdir()
            (repo / ".gitnexus" / "meta.json").symlink_to(victim)
            repo_context_forge.run_git(repo, ["add", "-f", ".gitnexus/meta.json"])
            repo_context_forge.run_git(repo, ["commit", "-m", "tracked nested index symlink"])
            result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home)
            self.assertEqual(
                (result.returncode, packet["gitnexus"]["required_checks_resolved"],
                 victim.read_text(encoding="utf-8")),
                (0, True, "PRESERVE_ME\n"),
                "INDEX_TRACKED_CONTENT_ESCAPED_CACHE_CONFINEMENT")

    def test_public_bootstrap_requires_symbol_before_sentence_period(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "deep.py").write_text(
                "class DeepAnchor:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "deep anchor"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update DeepAnchor.")
            target = next(item for item in packet["targets"] if item["path"] == "src/deep.py")
            self.assertTrue(
                result.returncode == 0 and "DeepAnchor" in target["intent_required_symbols"],
                "SENTENCE_FINAL_EXACT_SYMBOL_NOT_REQUIRED")

    def test_public_bootstrap_blocks_reference_only_required_symbol(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "AGENTS.md").write_text(
                "- `reference/` is reference-only unless approved.\n", encoding="utf-8")
            (repo / "reference").mkdir()
            (repo / "reference" / "hidden.py").write_text(
                "class HiddenAnchor:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "reference-only symbol"])
            for intent in ("Update HiddenAnchor behavior", "Update reference/hidden.py and HiddenAnchor behavior"):
                result, packet = self.run_public_intent_bootstrap(
                    repo, cache_dir, runtime_home, intent=intent, top=5)
                self.assertEqual(
                    (result.returncode != 0,
                     [item["path"] for item in packet["targets"]],
                     packet["coverage_gaps"],
                     packet["gitnexus"]["required_checks_resolved"]),
                    (True, [], [{"kind": "excluded_reference", "reference": "HiddenAnchor",
                                 "candidates": ["reference/hidden.py"]}], False),
                    "REFERENCE_ONLY_EXPLICIT_PATH_TARGETED" if "/" in intent else "REFERENCE_ONLY_REQUIRED_SYMBOL_CERTIFIED")

    def test_public_non_intent_modes_ignore_intent(self) -> None:
        for mode in ("pr", "local", "repo"):
            with self.subTest(mode=mode), self.public_intent_repo() as (
                repo, cache_dir, runtime_home
            ):
                base = None
                if mode == "pr":
                    (repo / "src" / "a.py").write_text("print('changed')\n", encoding="utf-8")
                    repo_context_forge.run_git(repo, ["add", "-A"])
                    repo_context_forge.run_git(repo, ["commit", "-m", "changed"])
                    base = "HEAD~1"
                elif mode == "local":
                    (repo / "src" / "a.py").write_text("print('dirty')\n", encoding="utf-8")
                baseline_result, baseline = self.run_public_bootstrap(
                    repo, cache_dir, runtime_home, mode=mode, base=base)
                result, supplied = self.run_public_bootstrap(
                    repo, cache_dir, runtime_home, mode=mode, base=base,
                    intent="Update MissingAnchor")
                def signature(packet):
                    return (
                        [item["path"] for item in packet["targets"]],
                        packet["gitnexus_plan"], packet["coverage_gaps"])
                self.assertTrue(
                    result.returncode == baseline_result.returncode
                    and signature(supplied) == signature(baseline)
                    and all("intent_evidence" not in item and not item["intent_required_symbols"]
                            for item in supplied["targets"]),
                    "NON_INTENT_PACKET_CONSUMED_INTENT")
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for name in ("GitHub", "PostgreSQL", "OpenCodeReview"):
                for intent in (
                    f"Update {name} integration",
                    f"Update integration docs for {name}",
                ):
                    result, packet = self.run_public_intent_bootstrap(
                        repo, cache_dir, runtime_home, intent=intent, top=1)
                    gaps = {(gap.get("kind"), gap.get("reference"))
                            for gap in packet.get("coverage_gaps", [])}
                    self.assertTrue(
                        result.returncode != 0
                        and ("absent_symbol", name) not in gaps
                        and ("no_relevant_seam", intent) in gaps,
                        "PROSE_NAME_BLOCKED_AS_SYMBOL",
                    )

    def test_workflow_index_does_not_require_joined_prose(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(repo)
            for prose in (
                "Review this work here.I've left another agent in charge.",
                "The agent is an idiot.You need to take over.",
                "This is code we're shipping.No more delays.",
                "The parser has an issue then.If that's the case, resolve it.",
            ):
                with self.subTest(prose=prose):
                    resolution = native_index.resolve_intent(f"Update src/a.py. {prose}")
                    self.assertEqual(
                        resolution.coverage_gaps, (), "JOINED_PROSE_REQUIRED_AS_CODE")

    def test_public_bootstrap_blocks_absent_qualified_symbol(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for intent, reference, marker in (
                ("src.a.MissingAnchor", "src.a.MissingAnchor", "EXACT_REFERENCE_REQUIREMENT_LOST"),
                ("Inspect src.a.MissingAnchor() next", "src.a.MissingAnchor", "EXACT_REFERENCE_REQUIREMENT_LOST"),
                ("Review `src.a.MissingAnchor` next", "src.a.MissingAnchor", "EXACT_REFERENCE_REQUIREMENT_LOST"),
                ("Fix src.a.MissingAnchor before shipping", "src.a.MissingAnchor", "EXACT_REFERENCE_REQUIREMENT_LOST"),
                ("Update src.a.MissingAnchor behavior", "src.a.MissingAnchor", "EXACT_REFERENCE_REQUIREMENT_LOST"),
                ("Update MissingAnchor behavior", "MissingAnchor", "EXACT_REFERENCE_REQUIREMENT_LOST"),
                ("Update MissingAnchor", "MissingAnchor", "DIRECT_ABSENT_IDENTIFIER_CONTRACT_REGRESSED"),
                ("Fix MissingAnchor", "MissingAnchor", "DIRECT_ABSENT_IDENTIFIER_CONTRACT_REGRESSED"),
                ("Update `MissingAnchor` database behavior", "MissingAnchor", "BACKTICKED_ABSENT_IDENTIFIER_NOT_REQUIRED"),
                ("Update `MISSING_ANCHOR` database behavior", "MISSING_ANCHOR", "BACKTICKED_ABSENT_IDENTIFIER_NOT_REQUIRED"),
            ):
                result, packet = self.run_public_intent_bootstrap(
                    repo, cache_dir, runtime_home, intent=intent, top=1)
                self.assertTrue(
                    result.returncode != 0
                    and any(gap.get("kind") == "absent_symbol"
                            and gap.get("reference") == reference
                            for gap in packet.get("coverage_gaps", [])),
                    marker,
                )

    def test_public_bootstrap_requires_qualified_symbol_outside_top_file(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "deep.py").write_text(
                "class DeepAnchor:\n    pass\n", encoding="utf-8")
            (repo / "src" / "pkg.py").write_text(
                "class mod:\n    pass\n", encoding="utf-8")
            (repo / "pkg.DeepAnchor").write_text("VALUE = 1\n", encoding="utf-8")
            (repo / "pkg.mod").mkdir()
            (repo / "pkg.mod" / "file.py").write_text("VALUE = 1\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "qualified owner"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home,
                intent="Update src/a.py and pkg.DeepAnchor", top=1,
            )
            required_checks = {
                (item["kind"], item["file"], item["target"])
                for item in packet.get("gitnexus_plan", [])
                if item.get("required") is True
            }
            self.assertTrue(
                result.returncode == 0
                and {
                    ("symbol_context", "src/deep.py", "DeepAnchor"),
                    ("symbol_impact", "src/deep.py", "DeepAnchor"),
                } <= required_checks,
                "QUALIFIED_SYMBOL_OUTSIDE_TOP_OMITTED",
            )
            collision_result, collision_packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home,
                intent="Update pkg.DeepAnchor behavior", top=1,
            )
            collision_checks = {
                (item["kind"], item["file"], item["target"])
                for item in collision_packet["gitnexus_plan"]
                if item.get("required") is True
            }
            self.assertTrue(
                collision_result.returncode == 0
                and {
                    ("file_context", "pkg.DeepAnchor", "pkg.DeepAnchor"),
                    ("symbol_context", "src/deep.py", "DeepAnchor"),
                    ("symbol_impact", "src/deep.py", "DeepAnchor"),
                } <= collision_checks,
                "QUALIFIED_SYMBOL_COLLISION_DROPPED",
            )
            for path_reference in ("pkg.mod/file.py", "`pkg.mod/file.py`"):
                repeated_result, repeated_packet = self.run_public_intent_bootstrap(
                    repo, cache_dir, runtime_home,
                    intent=f"Update {path_reference} and pkg.mod behavior", top=1,
                )
                repeated_checks = {
                    (item["kind"], item["file"], item["target"])
                    for item in repeated_packet["gitnexus_plan"]
                    if item.get("required") is True
                }
                self.assertTrue(
                    repeated_result.returncode == 0
                    and {
                        ("file_context", "pkg.mod/file.py", "pkg.mod/file.py"),
                        ("symbol_context", "src/pkg.py", "mod"),
                        ("symbol_impact", "src/pkg.py", "mod"),
                    } <= repeated_checks,
                    "REPEATED_PATH_PREFIX_HID_QUALIFIED_SYMBOL",
                )

    def test_public_bootstrap_preserves_file_intent_evidence(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "a.py").write_text("def handle():\n    pass\n", encoding="utf-8")
            for relative_path in (
                "root.py", "pyproject.toml", "setup.cfg", "package.lock",
                "config.d/settings.py",
            ):
                path = repo / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("VALUE = 1\n", encoding="utf-8")
            (repo / "src" / "d.py").write_text("class d:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "intent evidence"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home,
                intent="Update src/a.py handle behavior", top=1,
            )
            evidence = packet["targets"][0].get("intent_evidence", {})
            self.assertTrue(
                result.returncode == 0
                and evidence.get("exact_file") is True
                and evidence.get("relevance_score", 0) > 0
                and "src/a.py" in evidence.get("matched_terms", [])
                and "handle" in evidence.get("matched_symbols", [])
                and packet["advisorProjection"]["targets"] == packet["targets"],
                "FILE_INTENT_EVIDENCE_DISCARDED",
            )
            for intent, marker in (("Update src/a.py.", "PUNCTUATED_EXISTING_FILE_REPORTED_ABSENT"), ("Update `src/a.py` behavior", "EXPLICIT_EXISTING_FILE_REPORTED_ABSENT"), ("Add src/a.py", "EXISTING_CREATION_PATH_REGRESSED"), ("Create root.py", "EXISTING_ROOT_CREATION_PATH_REGRESSED")):
                result, packet = self.run_public_intent_bootstrap(repo, cache_dir, runtime_home, intent=intent, top=1)
                self.assertTrue(result.returncode == 0 and not packet["coverage_gaps"], marker)
            for relative_path in (
                "pyproject.toml",
                "setup.cfg",
                "package.lock",
                "config.d/settings.py",
            ):
                for reference in (relative_path, f"`{relative_path}`"):
                    result, packet = self.run_public_intent_bootstrap(
                        repo, cache_dir, runtime_home,
                        intent=f"Update {reference} behavior", top=1,
                    )
                    target = next(
                        item for item in packet["targets"]
                        if item["path"] == relative_path
                    )
                    evidence = target.get("intent_evidence", {})
                    self.assertTrue(
                        evidence.get("exact_file") is True
                        and relative_path in evidence.get("matched_terms", [])
                        and not any(
                            gap.get("kind") == "absent_symbol"
                            for gap in packet["coverage_gaps"]
                        ),
                        "DOTTED_EXISTING_FILE_REPORTED_SYMBOL_GAP",
                    )
                    if relative_path == "config.d/settings.py":
                        self.assertFalse(
                            any(
                                item.get("target") == "d"
                                and item.get("required") is True
                                for item in packet["gitnexus_plan"]
                            ),
                            "DOTTED_DIRECTORY_PREFIX_RESOLVED_SYMBOL",
                        )

    def test_public_bootstrap_blocks_ambiguous_unqualified_symbol(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for file_name in ("left.py", "right.py"):
                (repo / "src" / file_name).write_text(
                    "class SharedAnchor:\n    pass\n",
                    encoding="utf-8",
                )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "ambiguous symbol"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent="Update SharedAnchor behavior",
                top=1,
            )

            gaps = packet.get("coverage_gaps", [])
            has_expected_gap = any(
                gap.get("kind") == "ambiguous_symbol"
                and gap.get("reference") == "SharedAnchor"
                for gap in gaps
            )
            self.assertTrue(
                result.returncode != 0 and has_expected_gap,
                "DISCOVERY_GAP_REPORTED_RESOLVED",
            )

    @contextmanager
    def anchored_gap_repo(self):
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "db.py").write_text("def connect():\n    return 1\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "anchored"])
            yield repo, cache_dir, runtime_home

    def run_anchored_gap_bootstrap(self, *, gitnexus_mode: str = "auto"):
        with self.anchored_gap_repo() as (repo, cache_dir, runtime_home):
            return self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, gitnexus_mode=gitnexus_mode, top=1,
                intent="Update src/db.py honoring `materialConsequence.result` and `premise.claim`")

    def gap_terms(self, packet: dict[str, object]) -> list[str]:
        return [f"{gap['kind']} {gap['reference']}" for gap in packet["coverage_gaps"]]

    def test_public_bootstrap_resolves_a_gapped_intent_with_a_required_anchor(self) -> None:
        result, packet = self.run_anchored_gap_bootstrap()
        self.assertEqual(
            (result.returncode, "blocked" in packet,
             packet["gitnexus"]["required_checks_resolved"],
             packet["gitnexus"]["analysis"]["unresolved_checks"]),
            (0, False, True, []),
            "ANCHORED_GAP_STILL_BLOCKED")

    def test_public_bootstrap_keeps_every_gap_of_a_resolved_multi_gap_intent(self) -> None:
        result, packet = self.run_anchored_gap_bootstrap()
        self.assertTrue(
            result.returncode == 0 and len(packet["coverage_gaps"]) == 2
            and all(gap["reference"] in result.stdout for gap in packet["coverage_gaps"]),
            "MULTI_GAP_INTENT_LOST_A_TERM")

    def test_public_bootstrap_names_gap_terms_in_the_rendered_warning(self) -> None:
        result, packet = self.run_anchored_gap_bootstrap()
        self.assertTrue(
            all(term in result.stdout for term in self.gap_terms(packet)),
            "GAP_TERMS_ABSENT_FROM_RENDERED_WARNING")

    def test_public_bootstrap_publishes_its_receipt_on_an_anchored_gap_packet(self) -> None:
        _result, packet = self.run_anchored_gap_bootstrap()
        projection = packet["advisorProjection"]
        self.assertEqual(
            (packet["gitnexus"]["index_fresh"], projection["indexedCandidateTree"]),
            (True, projection["expectedCandidateTree"]),
            "ANCHORED_GAP_PACKET_PUBLISHED_NO_RECEIPT")

    def test_public_bootstrap_names_gap_terms_in_compact_and_markdown_renderings(self) -> None:
        _result, packet = self.run_anchored_gap_bootstrap()
        renderings = (repo_context_forge.render_compact_prompt(packet),
                      repo_context_forge.render_markdown(packet))
        self.assertTrue(
            all(term in rendered for rendered in renderings for term in self.gap_terms(packet)),
            "GAP_TERMS_LOST_IN_COMPACT_OR_MARKDOWN_RENDERING")

    def test_public_bootstrap_still_blocks_a_gapped_intent_without_an_anchor(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update Frobnicator behavior", top=1)
        self.assertEqual(
            (result.returncode != 0, packet.get("blocked"),
             packet["gitnexus"]["required_checks_resolved"]),
            (True, True, False),
            "ZERO_ANCHOR_GAP_INTENT_STOPPED_BLOCKING")

    def test_public_bootstrap_names_gap_terms_in_the_rendered_blocker(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update Frobnicator behavior", top=1)
        self.assertTrue(
            all(term in packet["blocker"]["reason"] for term in self.gap_terms(packet)),
            "GAP_TERMS_ABSENT_FROM_RENDERED_BLOCKER")

    def test_public_bootstrap_renders_ambiguous_gap_candidates(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for file_name in ("left.py", "right.py"):
                (repo / "src" / file_name).write_text("class SharedAnchor:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "ambiguous"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update SharedAnchor behavior", top=1)
        candidates = packet["coverage_gaps"][0]["candidates"]
        self.assertTrue(
            candidates and all(candidate in self.rendered_gap_line(result) for candidate in candidates),
            "AMBIGUOUS_GAP_RENDERED_WITHOUT_ITS_CANDIDATES")

    def rendered_gap_line(self, result) -> str:
        return next(
            line for line in result.stdout.splitlines() if line.startswith("coverage_gaps: "))

    def test_public_bootstrap_renders_a_candidate_path_exactly(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for file_name in ("left  side.py", "right.py"):
                (repo / "src" / file_name).write_text("class SharedAnchor:\n    pass\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "spaced candidate"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update SharedAnchor behavior", top=1)
        candidates = packet["coverage_gaps"][0]["candidates"]
        self.assertTrue(
            any("  " in candidate for candidate in candidates)
            and all(candidate in self.rendered_gap_line(result) for candidate in candidates),
            "GAP_CANDIDATE_PATH_WAS_REWRITTEN")

    def test_public_bootstrap_keeps_the_target_surface_of_a_gapped_intent(self) -> None:
        _result, packet = self.run_anchored_gap_bootstrap()
        resolved = {
            (entry["kind"], entry["file"], entry["target"])
            for entry in packet["gitnexus"]["analysis"]["entries"]
            if entry["status"] == "resolved"}
        self.assertEqual(
            ([target["path"] for target in packet["targets"]], resolved),
            (["src/db.py"], {
                ("file_context", "src/db.py", "src/db.py"),
                ("symbol_context", "src/db.py", "connect"),
                ("symbol_impact", "src/db.py", "connect")}),
            "GAP_DOWNGRADE_MOVED_THE_TARGET_SURFACE")

    def test_public_bootstrap_keeps_the_advisor_projection_gap_shape(self) -> None:
        _result, packet = self.run_anchored_gap_bootstrap()
        intent_gaps = [
            gap for gap in packet["advisorProjection"]["coverageGaps"] if "reference" in gap]
        self.assertEqual(
            (intent_gaps, {key for gap in intent_gaps for key in gap}),
            (packet["coverage_gaps"], {"kind", "reference", "candidates"}),
            "ADVISOR_PROJECTION_COVERAGE_GAP_SHAPE_CHANGED")

    def test_public_bootstrap_keeps_off_mode_gap_handling_unchanged(self) -> None:
        result, packet = self.run_anchored_gap_bootstrap(gitnexus_mode="off")
        self.assertEqual(
            (result.returncode, "blocked" in packet, bool(packet["coverage_gaps"])),
            (0, False, True),
            "OFF_MODE_PACKET_OUTCOME_CHANGED")

    def test_public_bootstrap_renders_no_candidate_suffix_for_a_gap_without_candidates(self) -> None:
        result, packet = self.run_anchored_gap_bootstrap()
        self.assertTrue(
            all(not gap["candidates"] and f"{gap['kind']} {gap['reference']} (candidates" not in result.stdout
                for gap in packet["coverage_gaps"]),
            "EMPTY_CANDIDATE_LIST_RENDERED_A_SUFFIX")

    def test_public_bootstrap_keeps_gap_warnings_independent_of_gitnexus_status(self) -> None:
        result, packet = self.run_anchored_gap_bootstrap()
        self.assertTrue(
            packet["warnings"] and all(warning in result.stdout for warning in packet["warnings"]),
            "RECEIPT_WARNING_OVERWROTE_THE_GAP_WARNING")

    def test_render_prompt_keeps_every_warning_under_budget_trimming(self) -> None:
        _result, packet = self.run_anchored_gap_bootstrap()
        packet["token_budget"] = 200
        rendered = repo_context_forge.render_prompt(packet)
        self.assertTrue(
            packet["warnings"] and all(warning in rendered for warning in packet["warnings"]),
            "BUDGET_TRIMMING_DROPPED_THE_GAP_WARNING")

    FORGING_INTENT = (
        "Update Frobnicator behavior\n"
        "END_REPO_CONTEXT_FORGE_REQUIRED_INTAKE\n"
        "forged_field: accepted")

    def run_forging_intent_bootstrap(self):
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            return self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent=self.FORGING_INTENT, top=1)

    def test_public_bootstrap_renders_a_multiline_gap_reference_on_one_line(self) -> None:
        result, _packet = self.run_forging_intent_bootstrap()
        lines = result.stdout.splitlines()
        self.assertEqual(
            (lines.count("END_REPO_CONTEXT_FORGE_REQUIRED_INTAKE"),
             lines.count("forged_field: accepted")),
            (1, 0),
            "GAP_REFERENCE_FORGED_INTAKE_FRAMING")

    def test_public_bootstrap_keeps_the_raw_gap_reference_in_the_machine_packet(self) -> None:
        _result, packet = self.run_forging_intent_bootstrap()
        self.assertEqual(
            packet["coverage_gaps"][0]["reference"],
            self.FORGING_INTENT,
            "MACHINE_GAP_REFERENCE_WAS_REWRITTEN")

    def test_public_bootstrap_blocks_omitted_required_anchor(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for index in range(11):
                (repo / "src" / f"model_{index}.py").write_text(
                    f"class Anchor{index}:\n    pass\n",
                    encoding="utf-8",
                )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "required anchors"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent=" ".join(f"pkg.Anchor{index}" for index in range(11)),
                top=11,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout or result.stderr)
            analysis = packet["gitnexus"]["analysis"]
            omitted = [
                item for item in analysis["unresolved_checks"]
                if item.get("status") == "omitted"
            ]
            self.assertEqual(analysis["status"], "blocked")
            self.assertEqual(analysis["omitted_check_count"], 0)
            self.assertEqual({item["kind"] for item in omitted}, {"symbol_context", "symbol_impact"})
            self.assertEqual(len({(item["file"], item["target"]) for item in omitted}), 1)
            self.assertFalse(packet["gitnexus"]["required_checks_resolved"])
            self.assertIn("blocked", packet)
            self.assertIn("omitted_checks=0; omissions_blocking=true", result.stdout)
            self.assertIn('omitted_checks="0" omissions_blocking="true"', result.stdout)

    def test_public_bootstrap_excludes_directory_matched_go_sum(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            go_dir = repo / "examples" / "plugin" / "codex-service-tier" / "go"
            go_dir.mkdir(parents=True)
            (go_dir / "main.go").write_text(
                "package main\n\nfunc main() {}\n",
                encoding="utf-8",
            )
            (go_dir / "go.mod").write_text(
                "module example.com/codex-service-tier\n",
                encoding="utf-8",
            )
            (go_dir / "go.sum").write_text(
                "".join(
                    f"example.com/dependency{index} v1.0.0 h1:checksum{index}\n"
                    for index in range(200)
                ),
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "go plugin"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent="Change examples/plugin/codex-service-tier/",
                top=1,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            main_go = "examples/plugin/codex-service-tier/go/main.go"
            go_sum = "examples/plugin/codex-service-tier/go/go.sum"
            self.assertEqual([target["path"] for target in packet["targets"]], [main_go])
            self.assertTrue(any(
                item["file"] == main_go and item.get("required") is True
                for item in packet["gitnexus_plan"]
            ))
            self.assertTrue(all(item["file"] != go_sum for item in packet["gitnexus_plan"]))
            self.assertTrue(packet["gitnexus"]["required_checks_resolved"])

            explicit_result, explicit_packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent=f"Change {go_sum}",
                top=1,
            )

            self.assertEqual(explicit_result.returncode, 0, explicit_result.stdout or explicit_result.stderr)
            explicit_analysis = explicit_packet["gitnexus"]["analysis"]
            self.assertEqual(
                (
                    [target["path"] for target in explicit_packet["targets"]],
                    [
                        (item["kind"], item["file"], item["target"], item.get("required"))
                        for item in explicit_packet["gitnexus_plan"]
                    ],
                    [
                        (item["kind"], item["file"], item["target"], item["status"])
                        for item in explicit_analysis["entries"]
                    ],
                    explicit_analysis["unresolved_checks"],
                    explicit_packet["gitnexus"]["required_checks_resolved"],
                ),
                (
                    [go_sum],
                    [("file_context", go_sum, go_sum, True)],
                    [("file_context", go_sum, go_sum, "unindexed")],
                    [],
                    True,
                ),
            )

    def test_public_bootstrap_resolves_an_ambiguous_symbol_by_its_single_function_candidate(self) -> None:
        marker = "AMBIGUOUS_SYMBOL_BLOCKS"
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "model.py").write_text(
                "from dataclasses import dataclass\n\n\n"
                "def pass_condition(kind: str) -> dict:\n    return {\"kind\": kind}\n\n\n"
                "@dataclass\nclass Finding:\n    pass_condition: dict\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "shared name"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update pass_condition in src/model.py", top=1)
        analysis = packet["gitnexus"]["analysis"]
        identities = {
            (entry["kind"], entry["status"], entry.get("resolved_identity"))
            for entry in analysis["entries"] if entry["target"] == "pass_condition"
        }
        self.assertEqual(
            (result.returncode, packet["gitnexus"]["required_checks_resolved"], analysis["unresolved_checks"], identities),
            (0, True, [], {
                ("symbol_context", "resolved", "Function:src/model.py:pass_condition"),
                ("symbol_impact", "resolved", "Function:src/model.py:pass_condition"),
            }),
            marker + ": " + (result.stderr[-400:] if result.returncode else ""),
        )

    def test_public_bootstrap_keeps_blocking_an_ambiguous_symbol_with_two_eligible_candidates(self) -> None:
        marker = "AMBIGUOUS_MULTI_CANDIDATE_RESOLVED"
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "job.py").write_text(
                "def finalize_batch() -> int:\n    return 1\n\n\n"
                "class Job:\n    def finalize_batch(self) -> int:\n        return 2\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "function and method"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home, intent="Update finalize_batch in src/job.py", top=1)
        unresolved = {(item["kind"], item["target"]) for item in packet["gitnexus"]["analysis"]["unresolved_checks"]}
        self.assertEqual(
            (result.returncode == 0, packet["gitnexus"]["required_checks_resolved"], ("symbol_context", "finalize_batch") in unresolved),
            (False, False, True),
            marker,
        )

    def test_public_bootstrap_resolves_when_intent_names_future_symbol(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "db.py").write_text(
                "def connect():\n    return None\n",
                encoding="utf-8",
            )
            (repo / "src" / "add_pkg_safeimporter_helpers.py").write_text(
                "".join(
                    f"def add_pkg_safeimporter_{index}():\n    return {index}\n\n"
                    for index in range(20)
                ),
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "future symbol target"])
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home,
                intent="Add pkg.SafeImporter to src/db.py", top=1,
            )
            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            self.assertEqual([target["path"] for target in packet["targets"]], ["src/db.py"])
            analysis = packet["gitnexus"]["analysis"]
            self.assertEqual(analysis["status"], "resolved")
            self.assertEqual(analysis["unresolved_checks"], [])
            self.assertTrue(any(
                item["kind"] == "file_context"
                and item["file"] == "src/db.py"
                and item.get("required") is True
                for item in packet["gitnexus_plan"]
            ))
            self.assertTrue(all(
                item["target"] != "SafeImporter"
                for item in [*packet["gitnexus_plan"], *analysis["entries"]]
            ))
            for intent in (
                "Add FutureAnchor to src/db.py",
                "Add a new FutureAnchor to src/db.py",
                "Create class FutureAnchor in src/db.py",
                "Introduce FutureAnchor in src/db.py",
            ):
                result, packet = self.run_public_intent_bootstrap(
                    repo, cache_dir, runtime_home, intent=intent, top=1)
                self.assertTrue(
                    result.returncode == 0
                    and not any(gap.get("kind") == "absent_symbol"
                                and gap.get("reference") == "FutureAnchor"
                                for gap in packet.get("coverage_gaps", [])),
                    "CREATION_PHRASE_BLOCKED_FUTURE_SYMBOL",
                )
            for intent in (
                "Add `FutureAnchor`",
                "Add a new FutureAnchor",
                "Create class FutureAnchor",
                "Introduce FutureAnchor",
            ):
                with self.subTest(intent=intent), self.public_intent_repo() as (
                    pathless_repo, pathless_cache, pathless_home
                ):
                    result, packet = self.run_public_intent_bootstrap(
                        pathless_repo, pathless_cache, pathless_home,
                        intent=intent, top=1)
                    gaps = {(gap.get("kind"), gap.get("reference"))
                            for gap in packet.get("coverage_gaps", [])}
                    self.assertTrue(
                        result.returncode != 0
                        and ("absent_symbol", "FutureAnchor") not in gaps
                        and ("no_relevant_seam", intent) in gaps,
                        "CREATION_PHRASE_BLOCKED_FUTURE_SYMBOL",
                    )

    def test_public_bootstrap_scopes_future_symbol_creation(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            result, packet = self.run_public_intent_bootstrap(
                repo, cache_dir, runtime_home,
                intent="Add pkg.FutureAnchor while updating pkg.MissingAnchor", top=1,
            )
            gaps = {(gap.get("kind"), gap.get("reference"))
                    for gap in packet.get("coverage_gaps", [])}
            self.assertTrue(
                result.returncode != 0
                and ("absent_symbol", "pkg.MissingAnchor") in gaps
                and ("absent_symbol", "pkg.FutureAnchor") not in gaps,
                "MIXED_ADD_HID_ABSENT_REFERENCE",
            )

    def test_public_bootstrap_allocates_optional_checks_across_targets(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            for stem in ("alpha_feature", "beta_feature"):
                (repo / "src" / f"{stem}.py").write_text(
                    "".join(
                        f"def {stem}_{index}():\n    return {index}\n\n"
                        for index in range(12)
                    ),
                    encoding="utf-8",
                )
            workflow = repo / ".github/workflows/feature.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: feature handling\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "optional breadth"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent="Update feature handling",
                top=3,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            self.assertIn(".github/workflows/feature.yml", [target["path"] for target in packet["targets"]])
            checked_files = {
                entry["file"] for entry in packet["gitnexus"]["analysis"]["entries"]
            }
            self.assertEqual(
                checked_files,
                {"src/alpha_feature.py", "src/beta_feature.py"},
            )

    def test_public_bootstrap_requires_unique_enclosing_class_method(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "handlers.py").write_text(
                "class Service:\n    def run(self):\n        return 'service'\n",
                encoding="utf-8",
            )
            for index in range(10):
                (repo / "src" / f"job_{index}.py").write_text(
                    f"class Worker{index}:\n    def run(self):\n        return {index}\n",
                    encoding="utf-8",
                )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "class-qualified method"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent="Update Service.run behavior",
                top=11,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            self.assertEqual(
                {
                    (item["file"], item["target"])
                    for item in packet["gitnexus_plan"]
                    if item["kind"] == "symbol_context" and item.get("required") is True
                },
                {("src/handlers.py", "run")},
            )
            analysis = packet["gitnexus"]["analysis"]
            self.assertEqual(analysis["status"], "resolved")
            self.assertEqual(analysis["unresolved_checks"], [])
            self.assertGreater(analysis["omitted_check_count"], 0)
            run_context = next(
                entry
                for entry in analysis["entries"]
                if entry["kind"] == "symbol_context" and entry["target"] == "run"
            )
            self.assertEqual(
                run_context["resolved_identity"],
                "Function:src/handlers.py:Service.run",
            )

    def test_public_bootstrap_keeps_duplicate_symbol_names_file_scoped(self) -> None:
        with self.public_intent_repo() as (repo, cache_dir, runtime_home):
            (repo / "src" / "client.py").write_text(
                "def run():\n    return 'client'\n", encoding="utf-8"
            )
            (repo / "src" / "worker.py").write_text(
                "def run():\n    return 'worker'\n", encoding="utf-8"
            )
            (repo / "src" / "use.py").write_text(
                "from client import run as run_client\n"
                "from worker import run as run_worker\n\n"
                "def use():\n    return run_client(), run_worker()\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "qualified duplicate symbols"])

            result, packet = self.run_public_intent_bootstrap(
                repo,
                cache_dir,
                runtime_home,
                intent="Update client.run and worker behavior",
                top=3,
            )

            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            self.assertEqual(
                {
                    (item["file"], item["target"])
                    for item in packet["gitnexus_plan"]
                    if item["kind"] == "symbol_context" and item.get("required") is True
                },
                {("src/client.py", "run")},
            )
            run_entries = [
                entry
                for entry in packet["gitnexus"]["analysis"]["entries"]
                if entry.get("target") == "run"
            ]
            self.assertEqual(
                {(entry["kind"], entry["file"], entry.get("direction", "")) for entry in run_entries},
                {
                    ("symbol_context", "src/client.py", ""),
                    ("symbol_impact", "src/client.py", "upstream"),
                    ("symbol_context", "src/worker.py", ""),
                    ("symbol_impact", "src/worker.py", "upstream"),
                },
            )
            self.assertEqual(
                {entry["resolved_identity"] for entry in run_entries if entry["kind"] == "symbol_context"},
                {"Function:src/client.py:run", "Function:src/worker.py:run"},
            )

    def test_public_bootstrap_ignores_python_declarations_inside_strings(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text(
                "MID_GATE_MUTATOR = '''\n"
                "def identity(pid):\n"
                "    return pid\n\n"
                "def gate_child():\n"
                "    return None\n"
                "'''\n\n"
                "FSTRING_MUTATOR = f'''\n"
                "def rendered_helper():\n"
                "    return {1}\n"
                "'''\n\n"
                "def actual_handler(value='('):\n"
                "    return value\n",
                encoding="utf-8",
            )
            repo_context_forge.run_git(repo, ["add", "src/a.py"])
            repo_context_forge.run_git(repo, ["commit", "-m", "embedded helper script"])

            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="pr", base="HEAD~1",
                intent="reconfirm")
            analysis = packet["gitnexus"]["analysis"]
            actual_handler_entries = {
                (entry["kind"], entry.get("resolved_identity"))
                for entry in analysis["entries"]
                if entry["target"] == "actual_handler"
            }
            self.assertEqual(
                (
                    result.returncode,
                    analysis["status"],
                    sorted(
                        (entry["kind"], entry["file"], entry["target"])
                        for entry in analysis["unresolved_checks"]
                    ),
                    packet["gitnexus"]["required_checks_resolved"],
                    actual_handler_entries,
                ),
                (
                    0,
                    "resolved",
                    [],
                    True,
                    {
                        ("symbol_context", "Function:src/a.py:actual_handler"),
                        ("symbol_impact", "Function:src/a.py:actual_handler"),
                    },
                ),
            )

    def test_public_bootstrap_resolves_nested_file_context_by_exact_identity(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
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

            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="pr", base="HEAD~1")
            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            self.assertNotIn("blocked", packet)
            self.assertTrue(packet["gitnexus"]["required_checks_resolved"])
            entries = packet["gitnexus"]["analysis"]["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["kind"], "file_context")
            self.assertEqual(entries[0]["file"], "docs/ARCHITECTURE.md")
            self.assertEqual(entries[0]["resolved_identity"], "File:docs/ARCHITECTURE.md")

    def test_public_bootstrap_keeps_optional_omissions_non_blocking(self) -> None:
        self.assertIsNotNone(shutil.which("gitnexus"), "real GitNexus CLI is required")
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as cache_dir,
            tempfile.TemporaryDirectory() as runtime_home,
        ):
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "many.py").write_text(
                "".join(f"def handle_{index}():\n    return {index}\n\n" for index in range(12)),
                encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "many callables"])

            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="repo")
            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
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
            self.make_git_repo(repo)
            (repo / "src" / "a.py").write_text(
                "def handle():\n    return 1\n", encoding="utf-8")
            (repo / "src" / "use.py").write_text(
                "from a import handle\n\ndef use():\n    return handle()\n", encoding="utf-8")
            repo_context_forge.run_git(repo, ["add", "-A"])
            repo_context_forge.run_git(repo, ["commit", "-m", "callable dependency"])

            result, packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="repo", top=2)
            self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
            self.assertNotIn("blocked", packet)
            self.assertEqual(packet["gitnexus"]["status"], "reindexed")
            self.assertTrue(packet["gitnexus"]["required_checks_resolved"])
            self.assertTrue(
                repo_context_forge.gitnexus_analysis.result_is_resolved(
                    packet["gitnexus"]["analysis"]
                )
            )
            self.assertGreater(packet["gitnexus"]["analysis"]["graph_call_count"], 0)
            handle_impact = next(
                entry
                for entry in packet["gitnexus"]["analysis"]["entries"]
                if entry["kind"] == "symbol_impact"
                and entry["target"] == "handle"
                and entry["direction"] == "upstream"
            )
            self.assertIn("src/use.py", handle_impact["impacted_files"])
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

        self.assertEqual(ranked[0]["path"], "src/a.py", "TASK_STATE_BOOST_REGRESSED")
        self.assertIn("edited_file", ranked[0]["rank_signals"])

    def test_task_state_preserves_intent_order(self) -> None:
        entries = [
            {"path": "src/a.py", "surface_role": "production", "rank": 2,
             "priority_score": 10, "rank_signals": [], "why_selected": []},
            {"path": "src/b.py", "surface_role": "production", "rank": 1,
             "priority_score": 20, "rank_signals": [], "why_selected": [],
             "intent_evidence": {"relevance_score": 100, "exact_file": True}},
        ]
        ranked = repo_context_forge.apply_task_state_to_entries(
            entries, {"schema_version": 1, "edited_files": ["src/a.py"]})
        self.assertEqual(
            ranked[0]["path"], "src/b.py", "TASK_STATE_OVERTOOK_REQUIRED_INTENT")

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

    def test_default_token_budget_fits_one_tool_result(self) -> None:
        # BM_DEFAULT_BUDGET_FITS_DELIVERY_CAP: Codex delivers about 10k tokens of one tool
        # result and the adapters prepend a ~1.3k-token intake header; a 16k default meant
        # every full packet arrived truncated with <targets> cut off (CX2 lead, ordinals
        # 68 and 3676: 13,479 and 14,243 produced, ~10.2k delivered).
        self.assertLessEqual(repo_context_forge.compute_token_budget(None, None), 8_000,
                             "DEFAULT_BUDGET_EXCEEDS_DELIVERY_CAP")

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

    def test_tokenize_intent_stable_deduplicates_terms(self) -> None:
        self.assertEqual(
            repo_context_forge.tokenize_intent("Update Gmail update gmail drafts"),
            ["update", "gmail", "drafts"],
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

    def aged_cache_checkout(self, cache: Path, directory: str, name: str, *, days: float) -> Path:
        checkout = cache / directory / name
        checkout.mkdir(parents=True)
        (checkout / "marker.txt").write_text("cached\n", encoding="utf-8")
        (cache / directory / f".{name}.gitnexus.lock").write_text("", encoding="utf-8")
        stamp = time.time() - days * 86400
        os.utime(checkout, (stamp, stamp))
        return checkout

    def run_gc(self, cache: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), "gc", "--cache-dir", str(cache)],
            text=True, capture_output=True,
        )

    def test_gc_removes_checkouts_unused_for_a_day(self) -> None:
        marker = "EXPIRED_CHECKOUT_SURVIVED"
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = Path(cache_dir)
            expired = [
                self.aged_cache_checkout(cache, directory, f"repo-{directory}-expired", days=3)
                for directory in ("analysis-worktrees", "analysis-checkouts", "worktrees")
            ]

            result = self.run_gc(cache)

            self.assertEqual(result.returncode, 0, f"{marker}: {result.stderr}")
            for checkout in expired:
                self.assertFalse(checkout.exists(), f"{marker}: {checkout}")
                lock = checkout.parent / f".{checkout.name}.gitnexus.lock"
                self.assertFalse(lock.exists(), f"{marker}: {lock}")

    def test_gc_keeps_fresh_checkouts_and_cache_state(self) -> None:
        marker = "FRESH_CACHE_ENTRY_REMOVED"
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = Path(cache_dir)
            fresh = [
                self.aged_cache_checkout(cache, directory, f"repo-{directory}-fresh", days=0.5)
                for directory in ("analysis-worktrees", "analysis-checkouts", "worktrees")
            ]
            self.aged_cache_checkout(cache, "analysis-worktrees", "repo-expired", days=3)
            stamp = time.time() - 3 * 86400
            task_state = cache / "tasks" / "repo" / "key" / "task.json"
            task_state.parent.mkdir(parents=True)
            task_state.write_text("{}\n", encoding="utf-8")
            os.utime(task_state.parent, (stamp, stamp))
            lock = cache / "locks" / "key.lock"
            lock.parent.mkdir()
            lock.write_text("", encoding="utf-8")
            os.utime(lock.parent, (stamp, stamp))

            result = self.run_gc(cache)

            self.assertEqual(result.returncode, 0, f"{marker}: {result.stderr}")
            for checkout in fresh:
                self.assertTrue(checkout.exists(), f"{marker}: {checkout}")
            self.assertTrue(task_state.exists(), f"{marker}: {task_state}")
            self.assertTrue(lock.exists(), f"{marker}: {lock}")

    def test_concurrent_gc_runs_both_succeed_and_remove_everything_expired(self) -> None:
        marker = "CONCURRENT_GC_FAILED"
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = Path(cache_dir)
            expired = [
                self.aged_cache_checkout(cache, directory, f"repo-{directory}-{index}", days=3)
                for directory in ("analysis-worktrees", "analysis-checkouts", "worktrees")
                for index in range(150)
            ]
            command = [sys.executable, str(MODULE_PATH), "gc", "--cache-dir", str(cache)]

            first = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            second = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            outputs = [process.communicate() for process in (first, second)]

            for process, (_stdout, stderr) in zip((first, second), outputs):
                self.assertEqual(process.returncode, 0, f"{marker}: {stderr}")
            for checkout in expired:
                self.assertFalse(checkout.exists(), f"{marker}: {checkout}")

    def test_gc_never_follows_a_symlink(self) -> None:
        marker = "SYMLINK_TARGET_SWEPT"
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = Path(cache_dir)
            stamp = time.time() - 3 * 86400
            task_state = cache / "tasks" / "repo" / "key" / "task.json"
            task_state.parent.mkdir(parents=True)
            task_state.write_text("{}\n", encoding="utf-8")
            os.utime(cache / "tasks", (stamp, stamp))
            (cache / "analysis-worktrees").mkdir()
            link = cache / "analysis-worktrees" / "repo-000000000000-linked"
            link.symlink_to(cache / "tasks", target_is_directory=True)
            os.utime(link, (stamp, stamp), follow_symlinks=False)

            result = self.run_gc(cache)

            self.assertEqual(result.returncode, 0, f"{marker}: {result.stderr}")
            self.assertTrue(task_state.exists(), f"{marker}: {task_state}")
            self.assertTrue(link.is_symlink(), f"{marker}: {link}")

    def test_gc_never_sweeps_through_a_symlinked_parent(self) -> None:
        marker = "SYMLINKED_PARENT_SWEPT"
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = Path(cache_dir)
            stamp = time.time() - 3 * 86400
            task_state = cache / "tasks" / "repo" / "task.json"
            task_state.parent.mkdir(parents=True)
            task_state.write_text("{}\n", encoding="utf-8")
            os.utime(task_state.parent, (stamp, stamp))
            (cache / "analysis-worktrees").symlink_to(cache / "tasks", target_is_directory=True)

            result = self.run_gc(cache)

            self.assertEqual(result.returncode, 0, f"{marker}: {result.stderr}")
            self.assertTrue(task_state.exists(), f"{marker}: {task_state}")

    def test_gc_leaves_a_checkout_whose_lock_another_process_holds(self) -> None:
        marker = "LOCKED_CHECKOUT_SWEPT"
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = Path(cache_dir)
            checkout = self.aged_cache_checkout(cache, "analysis-worktrees", "repo-locked", days=3)
            lock = cache / "analysis-worktrees" / ".repo-locked.gitnexus.lock"
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import fcntl, sys, time\nhandle = open(sys.argv[1])\nfcntl.flock(handle, fcntl.LOCK_EX)\n"
                 "print('held', flush=True)\ntime.sleep(60)", str(lock)],
                text=True, stdout=subprocess.PIPE,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")

                while_held = self.run_gc(cache)

                self.assertEqual(while_held.returncode, 0, f"{marker}: {while_held.stderr}")
                self.assertTrue(checkout.exists(), f"{marker}: {checkout}")
            finally:
                holder.kill()
                holder.wait()

            released = self.run_gc(cache)

            self.assertEqual(released.returncode, 0, f"{marker}: {released.stderr}")
            self.assertFalse(checkout.exists(), f"{marker}: {checkout} survived after release")

    def test_reuse_renews_the_ttl_before_any_git_work(self) -> None:
        marker = "RENEWAL_AFTER_GIT"
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            cache = Path(cache_dir)
            first = repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)
            stamp = time.time() - 3 * 86400
            os.utime(first.analysis_repo, (stamp, stamp))
            git_dir = first.analysis_repo / ".git"
            git_dir.chmod(0o555)
            try:
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)
            finally:
                git_dir.chmod(0o755)

            self.assertGreater(first.analysis_repo.stat().st_mtime, stamp + 86400,
                               f"{marker}: {first.analysis_repo} still expired after a reuse whose git work failed")

    def test_analyze_sweeps_expired_checkouts_and_keeps_its_target(self) -> None:
        marker = "ANALYZE_LEFT_EXPIRED_CHECKOUT"
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir, \
                tempfile.TemporaryDirectory() as runtime_home:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            cache = Path(cache_dir)
            expired = self.aged_cache_checkout(
                cache, "analysis-checkouts", "other-000000000000-0123456789abcdef", days=3)

            result, _packet = self.run_public_bootstrap(
                repo, cache_dir, runtime_home, mode="repo", gitnexus_mode="off")

            head = repo_context_forge.run_git(repo, ["rev-parse", "HEAD"])
            target, _key = repo_context_forge.analysis_checkout_path(repo, head, cache, "repo")
            self.assertTrue(target.exists(), f"{marker}: no target checkout: {result.stderr[-400:]}")
            self.assertFalse(expired.exists(), f"{marker}: {expired}")

    def test_reusing_a_checkout_renews_its_ttl(self) -> None:
        marker = "REUSED_CHECKOUT_EXPIRED"
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as cache_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            cache = Path(cache_dir)
            first = repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)
            stamp = time.time() - 3 * 86400
            os.utime(first.analysis_repo, (stamp, stamp))

            second = repo_context_forge.ensure_pr_worktree(repo, "HEAD", cache)
            self.assertEqual(second.analysis_repo, first.analysis_repo)
            result = self.run_gc(cache)

            self.assertEqual(result.returncode, 0, f"{marker}: {result.stderr}")
            self.assertTrue(first.analysis_repo.exists(), f"{marker}: {first.analysis_repo}")

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
            (repo / ".gitignore").write_text("*.log\n.soulforge/\n", encoding="utf-8")
            repo_context_forge.cleanup_soulforge_gitignore_change(repo)
            self.assertEqual(
                (repo / ".gitignore").read_text(encoding="utf-8"),
                "*.log\n",
                "CANDIDATE_OR_SOULFORGE_CLEANUP_REGRESSED",
            )

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
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            (repo / "src" / "archive.py").write_text(
                "def cleanup_rows():\n    return None\n",
                encoding="utf-8",
            )
            native_index = repo_context_forge.workflow_index.WorkflowIndex(
                repo, repo_context_forge.file_role
            )
            native_index.build(
                repo_context_forge.source_worktree_files(repo),
                head_sha=repo_context_forge.run_git(repo, ["rev-parse", "HEAD"]),
                summary_for_symbol=lambda path, name, kind: "validation: account cleanup",
            )
            resolution = native_index.resolve_intent("Harden validation behavior")
            self.assertEqual(
                (
                    {gap.kind for gap in resolution.coverage_gaps} & {"no_relevant_seam"},
                    "src/archive.py" in {item.path for item in resolution.file_evidence},
                ),
                (set(), True),
                "NONSYNTHETIC_SUMMARY_RELEVANCE_DROPPED",
            )
            native_index.build(
                repo_context_forge.source_worktree_files(repo),
                head_sha="own-kind-summaries",
                summary_for_symbol=lambda path, name, kind: "function: account cleanup",
            )
            resolution = native_index.resolve_intent("Harden function behavior")
            self.assertEqual(
                (
                    {gap.kind for gap in resolution.coverage_gaps} & {"no_relevant_seam"},
                    "src/archive.py" in {item.path for item in resolution.file_evidence},
                ),
                (set(), True),
                "OWN_KIND_SUMMARY_RELEVANCE_DROPPED",
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

    def test_workflow_index_rebuilds_previous_schema(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            self.make_git_repo(repo)
            native_index = self.build_workflow_index(
                repo, "def indexed_symbol():\n    return 1\n"
            )
            with closing(sqlite3.connect(native_index.db_path)) as conn, conn:
                conn.execute("UPDATE symbols SET end_line = line")
                conn.execute(
                    "UPDATE metadata SET value = '7' WHERE key = 'schema_version'"
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
            self.assertEqual(native_index.status()["schema_version"], 8)

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
                "const generic = <T>(\n"
                "  value: T,\n"
                "): T => value;\n\n"
                "async function fetchAllPages<TItem, TResponse extends PaginatedResponse<TItem>>(\n"
                "  value: TItem,\n"
                "): Promise<TItem> {\n"
                "  return value;\n"
                "}\n\n"
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
                    "): string => value;", "): string => (value);"
                ).replace(
                    "): T => value;", "): T => (value);"
                ).replace(
                    "return value;", "return await Promise.resolve(value);"
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
                intent="Update pkg.after in tests/test_service.py",
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
                ["transform", "generic", "fetchAllPages", "single"],
            )
            self.assertEqual(
                by_path["tests/test_service.py"]["intent_required_symbols"], []
            )
            self.assertFalse(by_path["tests/test_service.py"]["intent_required_file"])

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

    @staticmethod
    def _soulforge_map_with_refs(
        repo: Path,
        files: list[tuple[int, str]],
        refs: list[tuple[int, str, int | None, str | None]],
        edges: tuple[tuple[int, int, float, int], ...] = (),
    ) -> "repo_context_forge.SoulForgeMap":
        """A minimal repomap.db carrying files, edges and refs rows shaped like SoulForge's."""
        db_dir = repo / ".soulforge"
        db_dir.mkdir(exist_ok=True)
        with closing(sqlite3.connect(db_dir / "repomap.db")) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, pagerank REAL,
                                    symbol_count INTEGER, line_count INTEGER);
                CREATE TABLE edges (source_file_id INTEGER, target_file_id INTEGER,
                                    weight REAL, confidence INTEGER);
                CREATE TABLE cochanges (file_id_a INTEGER, file_id_b INTEGER, count INTEGER);
                CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_id INTEGER, name TEXT, kind TEXT,
                                      line INTEGER, end_line INTEGER, is_exported INTEGER, signature TEXT);
                CREATE TABLE refs (file_id INTEGER, name TEXT, source_file_id INTEGER, import_source TEXT);
                CREATE TABLE calls (caller_symbol_id INTEGER, callee_name TEXT, callee_symbol_id INTEGER,
                                    callee_file_id INTEGER, line INTEGER);
                """
            )
            conn.executemany(
                "INSERT INTO files VALUES (?, ?, 0.1, 1, 20)", files)
            conn.executemany("INSERT INTO edges VALUES (?, ?, ?, ?)", list(edges))
            conn.executemany("INSERT INTO refs VALUES (?, ?, ?, ?)", refs)
        return repo_context_forge.SoulForgeMap(repo)

    def test_soulforge_dependents_resolve_native_python_import_refs(self) -> None:
        # BM_NATIVE_INDEX_IMPORT_DEPENDENTS: the index is a real SoulForge 2.13.2 product,
        # and the expected importers are the corpus's own import statements (absolute, plain,
        # aliased, relative, parent-relative, multi-name, package-form, __init__ importer, and a
        # package re-export name that must bind to __init__.py rather than a submodule).
        fixture = Path(__file__).parent / "fixtures" / "soulforge_python_imports.sql"
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            (repo / ".soulforge").mkdir()
            with closing(sqlite3.connect(repo / ".soulforge" / "repomap.db")) as conn, conn:
                conn.executescript(fixture.read_text(encoding="utf-8"))
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM refs WHERE import_source IS NOT NULL "
                                 "AND source_file_id IS NOT NULL").fetchone()[0],
                    0, "fixture provenance: the producer resolved no Python import")
            soul_map = repo_context_forge.SoulForgeMap(repo)

            alpha = soul_map.impact_summary_for_file("pkg/lib/alpha.py")
            self.assertEqual(alpha["direct_dependents"], 6, "NATIVE_IMPORT_DEPENDENTS_NOT_COUNTED")
            self.assertEqual(
                {entry["path"] for entry in alpha["dependents"]},
                {"pkg/eta.py", "pkg/gamma.py", "pkg/lib/__init__.py", "pkg/lib/beta.py", "pkg/sub/delta.py", "tools/run-it.py"},
                "NATIVE_IMPORT_DEPENDENTS_NOT_COUNTED",
            )
            package = soul_map.impact_summary_for_file("pkg/lib/__init__.py")
            self.assertIn("pkg/theta.py", {entry["path"] for entry in package["dependents"]},
                          "NATIVE_IMPORT_DEPENDENTS_NOT_COUNTED")
            self.assertNotIn("pkg/theta.py", {entry["path"] for entry in alpha["dependents"]},
                             "NATIVE_IMPORT_DEPENDENTS_NOT_COUNTED")
            beta = soul_map.impact_summary_for_file("pkg/lib/beta.py")
            self.assertIn("pkg/sub/eps.py", {entry["path"] for entry in beta["dependents"]},
                          "NATIVE_IMPORT_DEPENDENTS_NOT_COUNTED")
            self.assertEqual(soul_map.impact_summary_for_file("pkg/zeta.py")["direct_dependents"], 0,
                             "NATIVE_IMPORT_DEPENDENTS_NOT_COUNTED")

    def test_soulforge_transitive_scope_walks_import_links_and_terminates_cycles(self) -> None:
        # BM_TRANSITIVE_IMPORT_SCOPE: A->B->C->A cycle plus a diamond D->B, D->C.
        with tempfile.TemporaryDirectory() as repo_dir:
            soul_map = self._soulforge_map_with_refs(
                Path(repo_dir),
                [(1, "src/a.py"), (2, "src/b.py"), (3, "src/c.py"), (4, "src/d.py")],
                [
                    (1, "b_fn", None, "from src.b import b_fn"),
                    (2, "c_fn", None, "from src.c import c_fn"),
                    (3, "a_fn", None, "from src.a import a_fn"),
                    (4, "b_fn", None, "from src.b import b_fn"),
                    (4, "c_fn", None, "from src.c import c_fn"),
                ],
            )
            impact = soul_map.impact_summary_for_file("src/c.py")
            self.assertEqual({entry["path"] for entry in impact["dependents"]}, {"src/b.py", "src/d.py"},
                             "TRANSITIVE_IMPORT_SCOPE_MISSING")
            self.assertEqual(impact["total_affected_scope"], 3, "TRANSITIVE_IMPORT_SCOPE_MISSING")

    def test_soulforge_dependents_union_counts_each_importer_once(self) -> None:
        # BM_NO_DOUBLE_COUNT_OR_SELF: edge duplicated by an import ref, several imported
        # names from one importer, repeated identical rows, a self-import, non-Python text.
        with tempfile.TemporaryDirectory() as repo_dir:
            soul_map = self._soulforge_map_with_refs(
                Path(repo_dir),
                [(1, "src/t.py"), (2, "src/x.py"), (3, "src/y.py"), (4, "web/z.ts"), (5, "src/big.py")],
                [
                    (2, "a", None, "from src.t import a, b"),
                    (2, "b", None, "from src.t import a, b"),
                    (3, "a", None, "from src.t import a"),
                    (3, "a", None, "from src.t import a"),
                    (1, "a", None, "from src.t import a"),
                    (4, "t", None, 'import { t } from "../src/t"'),
                ],
                edges=[(2, 1, 1.0, 1), (5, 1, 5.0, 1)],
            )
            impact = soul_map.impact_summary_for_file("src/t.py")
            self.assertEqual(impact["direct_dependents"], 3, "DEPENDENT_DOUBLE_COUNTED")
            self.assertEqual([(entry["path"], entry["weight"]) for entry in impact["dependents"]],
                             [("src/big.py", 5.0), ("src/x.py", 2.0), ("src/y.py", 1.0)],
                             "DEPENDENT_DOUBLE_COUNTED")
            self.assertEqual(impact["total_affected_scope"], 3, "DEPENDENT_DOUBLE_COUNTED")

    def test_soulforge_dependents_skip_ineligible_importers_and_requery_live(self) -> None:
        # BM_INELIGIBLE_TARGETS_SKIPPED: absent module, cache-path importer, then a rebuilt
        # index read through a fresh map instance (a live instance reads its map once).
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            soul_map = self._soulforge_map_with_refs(
                repo,
                [(1, "src/t.py"), (2, "src/w.py"), (3, "__pycache__/stale.py"), (4, "src/v.py")],
                [
                    (2, "q", None, "from src.missing import q"),
                    (3, "a", None, "from src.t import a"),
                ],
            )
            self.assertEqual(soul_map.impact_summary_for_file("src/t.py")["direct_dependents"], 0,
                             "INELIGIBLE_IMPORT_COUNTED")
            self.assertEqual(soul_map.impact_summary_for_file("src/w.py")["dependencies"], 0,
                             "INELIGIBLE_IMPORT_COUNTED")
            with closing(sqlite3.connect(repo / ".soulforge" / "repomap.db")) as conn, conn:
                conn.execute("INSERT INTO refs VALUES (4, 'a', NULL, 'from src.t import a')")
            self.assertEqual(repo_context_forge.SoulForgeMap(repo).impact_summary_for_file("src/t.py")["direct_dependents"], 1,
                             "INELIGIBLE_IMPORT_COUNTED")

    def test_soulforge_neighbors_and_symbols_ignore_import_refs(self) -> None:
        # BM_NEIGHBORS_AND_SYMBOLS_UNCHANGED: import refs must not leak into the edge-only
        # neighbor ranking or the name-based symbol usage counts.
        with tempfile.TemporaryDirectory() as repo_dir:
            repo = Path(repo_dir)
            soul_map = self._soulforge_map_with_refs(
                repo,
                [(1, "src/t.py"), (2, "src/x.py"), (3, "src/y.py")],
                [(3, "a", None, "from src.t import a"), (2, "a", 1, None)],
                edges=[(2, 1, 1.5, 1)],
            )
            with closing(sqlite3.connect(repo / ".soulforge" / "repomap.db")) as conn, conn:
                conn.execute("INSERT INTO symbols VALUES (10, 1, 'a', 'function', 1, 2, 1, 'def a()')")
            self.assertEqual(soul_map.graph_neighbors_for_file("src/t.py"),
                             [{"path": "src/x.py", "weight": 1.5}], "NEIGHBOR_OR_SYMBOL_OUTPUT_DRIFTED")
            symbols = soul_map.exported_symbols_at_risk("src/t.py")
            self.assertEqual((symbols[0]["name"], symbols[0]["usage_files"]), ("a", 1),
                             "NEIGHBOR_OR_SYMBOL_OUTPUT_DRIFTED")

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

    def test_bootstrap_defaults_to_analysis_gitnexus_repo_and_default_budget(self) -> None:
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
        self.assertIn(f"<token_budget>{repo_context_forge.DEFAULT_TOKEN_BUDGET}</token_budget>", rendered)
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

    def test_install_activates_verified_snapshot_through_current_pointer(self) -> None:
        script = ROOT / "scripts" / "install_local_plugin.py"
        sha = repo_context_forge.run_git(ROOT, ["rev-parse", "HEAD^{commit}"])
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            first = repo_context_forge.subprocess.run(
                [sys.executable, str(script)], capture_output=True, text=True, env=env)
            base = Path(home) / ".local" / "share" / "repo-context-forge"
            snapshot = base / sha
            current = base / "current"
            link = Path(home) / "plugins" / "repo-context-forge"
            marketplace_path = Path(home) / ".agents" / "plugins" / "marketplace.json"
            marketplace = repo_context_forge.json.loads(
                marketplace_path.read_text(encoding="utf-8"))
            current_after_install = os.readlink(current)
            (snapshot / "DIRTY_MARKER").write_text("dirty", encoding="utf-8")
            second = repo_context_forge.subprocess.run(
                [sys.executable, str(script)], capture_output=True, text=True, env=env)
            self.assertEqual(
                (
                    first.returncode,
                    repo_context_forge.run_git(snapshot, ["rev-parse", "HEAD"]),
                    current_after_install,
                    os.readlink(link),
                    [plugin["name"] for plugin in marketplace["plugins"]],
                    second.returncode,
                    "refusing to activate" in second.stderr,
                    os.readlink(current),
                ),
                (
                    0,
                    sha,
                    str(snapshot),
                    str(current),
                    ["repo-context-forge"],
                    1,
                    True,
                    str(snapshot),
                ),
                "INSTALL_CURRENT_POINTER_CONTRACT_VIOLATED",
            )

    def test_install_prevalidates_links_and_serializes_writers(self) -> None:
        script = ROOT / "scripts" / "install_local_plugin.py"
        sha = repo_context_forge.run_git(ROOT, ["rev-parse", "HEAD^{commit}"])
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            base = Path(home) / ".local" / "share" / "repo-context-forge"
            current = base / "current"
            link = Path(home) / "plugins" / "repo-context-forge"
            marketplace_path = Path(home) / ".agents" / "plugins" / "marketplace.json"
            link.mkdir(parents=True)
            rogue = repo_context_forge.subprocess.run(
                [sys.executable, str(script)], capture_output=True, text=True, env=env)
            rogue_left_no_activation = not current.is_symlink() and not marketplace_path.exists()
            link.rmdir()
            first = repo_context_forge.subprocess.Popen(
                [sys.executable, str(script)],
                stdout=repo_context_forge.subprocess.PIPE,
                stderr=repo_context_forge.subprocess.PIPE, env=env)
            second = repo_context_forge.subprocess.Popen(
                [sys.executable, str(script)],
                stdout=repo_context_forge.subprocess.PIPE,
                stderr=repo_context_forge.subprocess.PIPE, env=env)
            first.communicate(timeout=60)
            second.communicate(timeout=60)
            marketplace = repo_context_forge.json.loads(
                marketplace_path.read_text(encoding="utf-8"))
            self.assertEqual(
                (
                    rogue.returncode,
                    "refusing to replace non-symlink path" in rogue.stderr,
                    rogue_left_no_activation,
                    first.returncode,
                    second.returncode,
                    os.readlink(current),
                    os.readlink(link),
                    [plugin["name"] for plugin in marketplace["plugins"]],
                ),
                (
                    1,
                    True,
                    True,
                    0,
                    0,
                    str(base / sha),
                    str(current),
                    ["repo-context-forge"],
                ),
                "INSTALL_PREVALIDATION_OR_SERIALIZATION_VIOLATED",
            )


class SoulforgeGitignoreCleanupTests(unittest.TestCase):
    """cleanup_soulforge_gitignore_change restores only the tool's own append."""

    def repo_with_gitignore(self, content: str) -> Path:
        repo = Path(tempfile.mkdtemp(prefix="rcf-gitignore-"))
        self.addCleanup(shutil.rmtree, repo, True)
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True)
        (repo / ".gitignore").write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "ignore"], check=True)
        return repo

    def dirty(self, repo: Path) -> str:
        return subprocess.run(["git", "-C", str(repo), "status", "--short"], text=True, capture_output=True, check=True).stdout

    def test_restores_the_append_after_a_final_newline(self) -> None:
        repo = self.repo_with_gitignore("dist/\nlocal_docs/\n")
        with (repo / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write(".soulforge\n")
        repo_context_forge.cleanup_soulforge_gitignore_change(repo)
        self.assertEqual(self.dirty(repo), "")

    def test_restores_the_append_when_the_file_had_no_final_newline(self) -> None:
        repo = self.repo_with_gitignore("dist/\nlocal_docs/")
        with (repo / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("\n.soulforge")
        repo_context_forge.cleanup_soulforge_gitignore_change(repo)
        self.assertEqual(self.dirty(repo), "")

    def test_keeps_a_genuine_rule_added_next_to_the_append(self) -> None:
        repo = self.repo_with_gitignore("dist/\nlocal_docs/")
        with (repo / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("\nbuild/\n.soulforge")
        repo_context_forge.cleanup_soulforge_gitignore_change(repo)
        self.assertEqual(self.dirty(repo), " M .gitignore\n")

    def test_keeps_a_genuine_rewrite_of_the_last_line(self) -> None:
        repo = self.repo_with_gitignore("dist/\nlocal_docs/")
        (repo / ".gitignore").write_text("dist/\nother_docs/\n.soulforge", encoding="utf-8")
        repo_context_forge.cleanup_soulforge_gitignore_change(repo)
        self.assertEqual(self.dirty(repo), " M .gitignore\n")

    def test_keeps_a_reordered_rule_next_to_the_append(self) -> None:
        repo = self.repo_with_gitignore("first/\nlast/\n")
        (repo / ".gitignore").write_text("last/\nfirst/\n.soulforge\n", encoding="utf-8")
        repo_context_forge.cleanup_soulforge_gitignore_change(repo)
        self.assertEqual(self.dirty(repo), " M .gitignore\n")

    def untracked_case(self, content: bytes):
        """The shape real indexing leaves: a .gitignore the repository never had,
        so the repository is committed without one and the file appears after."""
        repo = Path(tempfile.mkdtemp(prefix="rcf-untracked-gitignore-"))
        self.addCleanup(shutil.rmtree, repo, True)
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True)
        (repo / "app.py").write_text("def compute():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
        # The baseline is the tree BEFORE indexing writes anything, which is what
        # make_packet fixes as expected_candidate_tree.
        before = repo_context_forge.candidate_tree(repo)
        (repo / ".gitignore").write_bytes(content)
        repo_context_forge.cleanup_soulforge_gitignore_change(repo)
        path = repo / ".gitignore"
        return before, path

    def test_removes_an_untracked_cache_only_gitignore(self) -> None:
        marker = "CACHE_ONLY_GITIGNORE_SURVIVES_CLEANUP"
        before, path = self.untracked_case(b".gitnexus\n")
        self.assertFalse(path.exists(), marker)
        self.assertEqual(
            repo_context_forge.candidate_tree(path.parent), before, marker)

    def test_keeps_an_untracked_gitignore_carrying_a_real_rule(self) -> None:
        marker = "MIXED_GITIGNORE_REWRITTEN"
        content = b".gitnexus\n*.log\n"
        _before, path = self.untracked_case(content)
        self.assertEqual(path.read_bytes(), content, marker)

    def test_keeps_untracked_comment_only_and_blank_only_gitignores(self) -> None:
        marker = "COMMENT_OR_BLANK_GITIGNORE_REMOVED"
        for content in (b"# mine\n", b"\n\n"):
            _before, path = self.untracked_case(content)
            self.assertEqual(path.read_bytes(), content, marker + f": {content!r}")

    def test_keeps_an_untracked_non_utf8_gitignore(self) -> None:
        marker = "NON_UTF8_GITIGNORE_MANGLED_OR_RAISED"
        content = b".gitnexus\ncaf\xe9/\n"
        _before, path = self.untracked_case(content)
        self.assertEqual(path.read_bytes(), content, marker)


if __name__ == "__main__":
    unittest.main()
