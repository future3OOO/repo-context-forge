from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Iterator, TextIO


GITNEXUS_REGISTRY = Path.home() / ".gitnexus" / "registry.json"
CALL_TIMEOUT_SECONDS = 30
TOTAL_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 256_000
MAX_FACTS = 50

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ACTIVE_TRANSACTION: ContextVar[tuple[str, TextIO] | None] = ContextVar("active_analysis_transaction", default=None)


class AnalysisTransactionBusy(RuntimeError):
    """The analysis checkout lock is held by another transaction."""


def analysis_transaction_path(analysis_repo: Path) -> Path:
    return analysis_repo.parent / f".{analysis_repo.name}.gitnexus.lock"


@contextmanager
def analysis_transaction(lock_path: Path) -> Iterator[None]:
    key = str(lock_path.resolve())
    if (active := ACTIVE_TRANSACTION.get()) is not None and active[0] == key and not active[1].closed:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AnalysisTransactionBusy(
                "candidate analysis transaction is already running") from exc
        token = ACTIVE_TRANSACTION.set((key, lock_file))
        try:
            yield
        finally:
            ACTIVE_TRANSACTION.reset(token)


def _read_registry(registry_path: Path) -> list[dict[str, object]]:
    if not registry_path.exists():
        return []
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitNexus registry is not valid JSON: {registry_path}") from exc
    if not isinstance(raw, list):
        raise RuntimeError(f"GitNexus registry must contain a list: {registry_path}")
    return [entry for entry in raw if isinstance(entry, dict)]


def _find_entry(
    analysis_repo: Path,
    repo_name: str | None,
    registry_path: Path,
) -> dict[str, object] | None:
    resolved = str(analysis_repo.resolve())
    entries = _read_registry(registry_path)
    for entry in entries:
        if str(entry.get("path") or "") == resolved:
            return entry
    if repo_name:
        for entry in entries:
            if entry.get("name") == repo_name and str(entry.get("path") or "") == resolved:
                return entry
    return None


def _read_json_object(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _status_from_entry(
    entry: dict[str, object] | None,
    analysis_repo: Path,
    expected_head: str,
    expected_candidate_tree: str | None,
    repo_name: str,
    run_command: RunCommand,
) -> dict[str, object]:
    indexed_head = str(entry.get("lastCommit") or "") if entry else ""
    index_path = Path(str(entry.get("storagePath") or "")) if entry else None
    if entry and (not index_path or str(index_path) == "."):
        index_path = analysis_repo / ".gitnexus"
    if entry and not indexed_head:
        indexed_path = Path(str(entry.get("path") or ""))
        if indexed_path.resolve() == analysis_repo.resolve():
            proc = run_command(
                ["git", "-C", str(indexed_path), "rev-parse", "HEAD"],
                allow_fail=True,
            )
            indexed_head = proc.stdout.strip() if proc.returncode == 0 else ""
    index_present = bool(index_path and index_path.exists())
    indexed_at = str(entry.get("indexedAt") or "") if entry else ""
    local_meta = _read_json_object(index_path / "meta.json") if index_path else None
    local_indexed_at = str(local_meta.get("indexedAt") or "") if local_meta else ""
    generation_fresh = bool(
        indexed_head == expected_head
        and index_present
        and indexed_at
        and indexed_at == local_indexed_at
        and str(local_meta.get("repoPath") or "") == str(analysis_repo.resolve())
    )
    receipt_path = index_path / "repo-context-forge-receipt.json" if index_path else None
    receipt = _read_json_object(receipt_path) if receipt_path else None
    receipt_fresh = bool(
        generation_fresh
        and expected_candidate_tree
        and receipt
        and receipt.get("schemaVersion") == 1
        and receipt.get("analysisRepo") == str(analysis_repo.resolve())
        and receipt.get("committedHeadOid") == expected_head
        and receipt.get("candidateTree") == expected_candidate_tree
        and receipt.get("indexedAt") == indexed_at
    )
    return {
        "repo": str(entry.get("name") or repo_name) if entry else repo_name,
        "expected_repo_path": str(analysis_repo),
        "expected_head_sha": expected_head,
        "expected_candidate_tree": expected_candidate_tree or "",
        "indexed_head_sha": indexed_head,
        "indexed_at": indexed_at,
        "local_indexed_at": local_indexed_at,
        "index_generation": indexed_at if generation_fresh else "",
        "index_path": str(index_path or ""),
        "index_present": index_present,
        "index_generation_fresh": generation_fresh,
        "indexed_candidate_tree": expected_candidate_tree if receipt_fresh else "",
        "index_fresh": receipt_fresh,
    }


def _current_status(
    analysis_repo: Path,
    expected_head: str,
    expected_candidate_tree: str | None,
    repo_name: str,
    registry_path: Path,
    run_command: RunCommand,
) -> dict[str, object]:
    return _status_from_entry(
        _find_entry(analysis_repo, repo_name, registry_path),
        analysis_repo,
        expected_head,
        expected_candidate_tree,
        repo_name,
        run_command,
    )


def _accept_fresh(status: dict[str, object]) -> bool:
    if not status["index_fresh"]:
        return False
    status.update(status="fresh", reindex_attempted=False)
    return True


def ensure_index(
    analysis_repo: Path,
    expected_head: str,
    repo_name: str | None,
    mode: str,
    *,
    run_command: RunCommand,
    expected_candidate_tree: str | None = None,
    registry_path: Path = GITNEXUS_REGISTRY,
    gitnexus_bin: str | None = None,
) -> dict[str, object]:
    chosen_repo_name = repo_name or analysis_repo.name
    base_status: dict[str, object] = {
        "repo": chosen_repo_name,
        "expected_repo_path": str(analysis_repo),
        "expected_head_sha": expected_head,
        "expected_candidate_tree": expected_candidate_tree or "",
        "reindex_attempted": False,
        "required_checks_resolved": False,
    }
    if mode == "off":
        return {**base_status, "status": "disabled"}

    binary = gitnexus_bin or shutil.which("gitnexus")
    if not binary:
        return {
            **base_status,
            "status": "unavailable",
            "warning": "GitNexus binary not found; blast-radius claims are blocked",
        }

    status_args = (
        analysis_repo,
        expected_head,
        expected_candidate_tree,
        chosen_repo_name,
        registry_path,
        run_command,
    )
    status = _current_status(*status_args)
    if _accept_fresh(status):
        return status
    if mode != "auto":
        status.update({"status": "blocked", "reindex_attempted": False})
        if not status.get("index_present"):
            status["warning"] = "GitNexus index storage is missing; blast-radius claims are blocked"
        return status

    def reindex() -> dict[str, object]:
        current = _current_status(*status_args)
        if _accept_fresh(current):
            return current
        proc = run_command(
            [binary, "analyze", "--force", "--skip-agents-md", str(analysis_repo)],
            allow_fail=True,
            suppress_core_dump=True,
        )
        current = _current_status(*status_args)
        current.update(reindex_attempted=True, reindex_returncode=proc.returncode)
        if proc.returncode != 0:
            current.update(
                status="blocked",
                warning="GitNexus reindex failed; blast-radius claims are blocked",
                stderr=proc.stderr[-2000:],
            )
        elif current["index_generation_fresh"]:
            current.update(status="reindexed", receipt_pending=True)
        else:
            current["status"] = "blocked"
            failure = "did not create registered index storage" if not current.get(
                "index_present"
            ) else "index generation is stale after reindex"
            current["warning"] = f"GitNexus reindex {failure}; blast-radius claims are blocked"
        return current

    try:
        with analysis_transaction(analysis_transaction_path(analysis_repo)):
            try:
                return reindex()
            except RuntimeError as exc:
                status.update(
                    status="blocked", reindex_attempted=True,
                    warning=f"{exc}; blast-radius claims are blocked")
                return status
    except OSError as exc:
        status.update(
            status="blocked",
            reindex_attempted=False,
            warning=f"GitNexus reindex lock failed; blast-radius claims are blocked ({exc})",
        )
    except AnalysisTransactionBusy:
        status.update(
            status="blocked",
            reindex_attempted=False,
            warning="GitNexus reindex is already running for this analysis checkout",
        )
    return status


def publish_receipt(
    gitnexus_status: dict[str, object],
    analysis_repo: Path,
    expected_head: str,
    expected_candidate_tree: str,
    *,
    run_command: RunCommand,
    registry_path: Path = GITNEXUS_REGISTRY,
) -> dict[str, object]:
    status_args = (
        analysis_repo,
        expected_head,
        expected_candidate_tree,
        str(gitnexus_status.get("repo") or analysis_repo.name),
        registry_path,
        run_command,
    )
    current = _current_status(*status_args)
    warning = "GitNexus index generation changed before receipt publication"
    if current.get("index_generation_fresh"):
        index_path = Path(str(current["index_path"]))
        receipt_path = index_path / "repo-context-forge-receipt.json"
        temporary_path = receipt_path.with_suffix(".tmp")
        try:
            temporary_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "analysisRepo": str(analysis_repo.resolve()),
                        "committedHeadOid": expected_head,
                        "candidateTree": expected_candidate_tree,
                        "indexedAt": current["indexed_at"],
                    },
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(receipt_path)
        except OSError as exc:
            gitnexus_status.update(
                status="blocked", required_checks_resolved=False,
                warning=f"GitNexus receipt publication failed; blast-radius claims are blocked ({exc})")
            return gitnexus_status
        current = _current_status(*status_args)
        warning = "GitNexus receipt validation failed after publication"

    if current.get("index_fresh"):
        gitnexus_status.update(
            index_generation=current["index_generation"],
            indexed_candidate_tree=expected_candidate_tree,
            index_fresh=True,
            receipt_pending=False,
        )
    else:
        gitnexus_status.update(
            status="blocked",
            required_checks_resolved=False,
            warning=warning,
        )
    return gitnexus_status


def _check_key(item: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(item.get("kind") or ""),
        str(item.get("file") or ""),
        str(item.get("target") or ""),
        str(item.get("direction") or ""),
    )


def _references(value: object) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return []
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for items in value.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("uid") or item.get("id") or "")
            if not identity or identity in seen:
                continue
            references.append(
                {
                    "identity": identity,
                    "name": str(item.get("name") or ""),
                    "file": str(item.get("filePath") or ""),
                }
            )
            seen.add(identity)
            if len(references) >= MAX_FACTS:
                return references
    return references


def result_is_resolved(value: object) -> bool:
    if not isinstance(value, dict) or value.get("status") != "resolved":
        return False
    entries = value.get("entries")
    unresolved = value.get("unresolved_checks")
    metrics = ("elapsed_ms", "process_count", "graph_call_count", "output_bytes")
    if not isinstance(entries, list) or unresolved != []:
        return False
    if any(not isinstance(value.get(metric), int) for metric in metrics):
        return False
    return all(
        isinstance(entry, dict)
        and entry.get("status") == "resolved"
        and bool(entry.get("kind"))
        and bool(entry.get("file"))
        and bool(entry.get("target"))
        and bool(entry.get("resolved_identity"))
        for entry in entries
    )


def execute(
    plan: list[dict[str, object]],
    gitnexus_status: dict[str, object],
    *,
    run_command: RunCommand,
    omitted_check_count: int = 0,
    omitted_required_checks: list[dict[str, object]] | None = None,
    gitnexus_bin: str | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    unresolved_required_checks: list[dict[str, object]] = []
    for item in omitted_required_checks or []:
        kind, file_path, target, direction = _check_key(item)
        unresolved_required_checks.append(
            {
                "kind": kind,
                "file": file_path,
                "target": target,
                "direction": direction,
                "status": "omitted",
                "diagnostic": "required intent check was omitted from the GitNexus plan",
            }
        )
    analysis: dict[str, object] = {
        "status": "blocked",
        "entries": [],
        "unresolved_checks": unresolved_required_checks,
        "elapsed_ms": 0,
        "process_count": 0,
        "graph_call_count": 0,
        "output_bytes": 0,
        "estimated_output_tokens": 0,
        "omitted_check_count": omitted_check_count,
    }
    gitnexus_status["analysis"] = analysis
    if gitnexus_status.get("status") not in {"fresh", "reindexed"}:
        gitnexus_status["required_checks_resolved"] = False
        return gitnexus_status
    binary = gitnexus_bin or shutil.which("gitnexus")
    if not binary:
        gitnexus_status.update(status="unavailable", required_checks_resolved=False)
        return gitnexus_status

    repo_name = str(gitnexus_status["repo"])
    seen: set[tuple[str, str, str, str]] = set()
    resolved_symbols: dict[tuple[str, str], str] = {}
    entries = analysis["entries"]
    unresolved = analysis["unresolved_checks"]
    assert isinstance(entries, list) and isinstance(unresolved, list)
    for item in plan:
        key = _check_key(item)
        if key in seen:
            continue
        seen.add(key)
        kind, file_path, target, direction = key
        entry: dict[str, object] = {
            "kind": kind,
            "file": file_path,
            "target": target,
            "direction": direction,
            "status": "unresolved",
        }
        entries.append(entry)
        elapsed = time.monotonic() - started
        if elapsed >= TOTAL_TIMEOUT_SECONDS:
            entry["diagnostic"] = "GitNexus total timeout exceeded"
            unresolved.append(dict(entry))
            continue
        if kind == "file_context":
            command = [binary, "context", "-r", repo_name, "-u", f"File:{target}"]
        elif kind == "symbol_context":
            command = [binary, "context", "-r", repo_name, "-f", file_path, target]
        elif kind == "symbol_impact":
            expected_identity = resolved_symbols.get((file_path, target))
            if not expected_identity:
                entry["diagnostic"] = "GitNexus impact has no file-resolved context identity"
                unresolved.append(dict(entry))
                continue
            command = [
                binary,
                "impact",
                "--uid",
                expected_identity,
                "-d",
                direction or "upstream",
                "-r",
                repo_name,
                "--include-tests",
            ]
        else:
            entry["diagnostic"] = f"unsupported GitNexus check kind: {kind}"
            unresolved.append(dict(entry))
            continue
        try:
            proc = run_command(
                command,
                allow_fail=True,
                suppress_core_dump=True,
                capture_output_to_file=True,
                timeout=min(CALL_TIMEOUT_SECONDS, TOTAL_TIMEOUT_SECONDS - elapsed),
            )
        except subprocess.TimeoutExpired:
            entry["diagnostic"] = "GitNexus check timed out"
            unresolved.append(dict(entry))
            continue
        analysis["process_count"] = int(analysis["process_count"]) + 1
        analysis["graph_call_count"] = int(analysis["graph_call_count"]) + 1
        output_bytes = len(proc.stdout.encode("utf-8")) + len(proc.stderr.encode("utf-8"))
        analysis["output_bytes"] = int(analysis["output_bytes"]) + output_bytes
        if output_bytes > MAX_OUTPUT_BYTES:
            entry["diagnostic"] = "GitNexus check output exceeded the byte limit"
            unresolved.append(dict(entry))
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
        if proc.returncode != 0 or not isinstance(payload, dict) or payload.get("error"):
            diagnostic = (
                str(payload.get("error"))
                if isinstance(payload, dict) and payload.get("error")
                else proc.stderr.strip() or "GitNexus returned malformed output"
            )
            entry["diagnostic"] = diagnostic[:1000]
            unresolved.append(dict(entry))
            continue

        entity = payload.get("symbol") if kind.endswith("context") else payload.get("target")
        if not isinstance(entity, dict):
            entry["diagnostic"] = "GitNexus result has no resolved entity"
            unresolved.append(dict(entry))
            continue
        resolved_identity = str(entity.get("uid") or entity.get("id") or "")
        resolved_file = str(entity.get("filePath") or "")
        resolved_name = str(entity.get("name") or "")
        expected_file = target if kind == "file_context" else file_path
        if (
            not resolved_identity
            or resolved_file != expected_file
            or (kind == "file_context" and resolved_identity != f"File:{target}")
            or (kind != "file_context" and resolved_name != target)
        ):
            entry["diagnostic"] = "GitNexus result identity does not match the planned check"
            unresolved.append(dict(entry))
            continue
        if kind == "symbol_impact":
            expected_identity = resolved_symbols.get((file_path, target))
            if not expected_identity or resolved_identity != expected_identity:
                entry["diagnostic"] = "GitNexus impact identity does not match file-resolved context"
                unresolved.append(dict(entry))
                continue
            if str(payload.get("direction") or "") != (direction or "upstream"):
                entry["diagnostic"] = "GitNexus impact direction does not match the planned check"
                unresolved.append(dict(entry))
                continue

        entry.update(
            status="resolved",
            resolved_identity=resolved_identity,
            entity={"name": resolved_name, "file": resolved_file},
        )
        if kind == "symbol_context":
            resolved_symbols[(file_path, target)] = resolved_identity
            entry["callers"] = _references(payload.get("incoming"))
        elif kind == "file_context":
            entry["references"] = _references(payload.get("incoming"))
        else:
            impacted = _references(payload.get("byDepth"))
            entry["impacted_files"] = list(
                dict.fromkeys(reference["file"] for reference in impacted if reference["file"])
            )[:MAX_FACTS]

    analysis["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    analysis["estimated_output_tokens"] = (int(analysis["output_bytes"]) + 3) // 4
    analysis["status"] = "resolved" if not unresolved else "blocked"
    gitnexus_status["required_checks_resolved"] = result_is_resolved(analysis)
    gitnexus_status["missing_required_symbols"] = [
        str(item.get("target") or "") for item in unresolved if isinstance(item, dict)
    ]
    if unresolved:
        gitnexus_status.update(
            status="blocked",
            warning="GitNexus semantic analysis is unresolved; blast-radius claims are blocked",
        )
    return gitnexus_status
