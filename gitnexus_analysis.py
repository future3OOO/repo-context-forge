from __future__ import annotations

import fcntl
import functools
import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Iterator, Optional, TextIO


GITNEXUS_REGISTRY = Path.home() / ".gitnexus" / "registry.json"
CALL_TIMEOUT_SECONDS = 30
TOTAL_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 256_000
MAX_FACTS = 50
# Containment, not dependency: an owning class is not a caller and a member is not a callee.
MEMBERSHIP_RELATIONS = {"has_method", "has_property"}
# `gitnexus context` returns at most this many rows per direction, an arbitrary subset beyond it;
# a list at the limit is completed from one query over the dependency relations context reads.
CONTEXT_ROW_LIMIT = 30
DEPENDENCY_RELATIONS = [
    "CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS", "METHOD_OVERRIDES", "OVERRIDES", "METHOD_IMPLEMENTS", "ACCESSES",
]

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
Reply = tuple[Optional[dict[str, object]], str]  # a tool call: its JSON answer, or None and the reason
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


def receipted_commit(
    repo_name: str,
    candidate_tree: str,
    *,
    run_command: RunCommand,
    registry_path: Path = GITNEXUS_REGISTRY,
) -> str | None:
    """The indexed commit of `repo_name` while its current index is the one a packet run receipted
    for `candidate_tree`; None once anything has re-indexed it."""
    for entry in _read_registry(registry_path):
        if entry.get("name") == repo_name:
            commit = str(entry.get("lastCommit") or "")
            status = _status_from_entry(
                entry, Path(str(entry.get("path") or "")), commit, candidate_tree, repo_name, run_command)
            return commit if status["index_fresh"] else None
    return None


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
    for relation, items in value.items():
        if relation in MEMBERSHIP_RELATIONS or not isinstance(items, list):
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
        and bool(entry.get("kind"))
        and bool(entry.get("file"))
        and bool(entry.get("target"))
        and (
            (entry.get("status") == "resolved" and bool(entry.get("resolved_identity")))
            or (entry.get("status") == "unindexed" and entry.get("kind") == "file_context")
        )
        for entry in entries
    )


class GraphSession:
    """One `gitnexus mcp` process answering every graph call for one repository.

    Each tool takes the arguments the GitNexus CLI passes for the same command, so an answer
    is what the CLI prints for that symbol alone. The process starts at the first call. It
    runs with a private HOME holding only this repository's registry entries: the server
    writes a reply over its 16 KB bound to a results directory under HOME and prunes that
    directory, which must not be the user's. Such a reply is read whole from its file."""

    def __init__(self, repo: str, gitnexus_bin: str | None = None) -> None:
        self.repo = repo
        self.binary = gitnexus_bin or shutil.which("gitnexus")
        self.calls = self.processes = self.output_bytes = 0
        self.started: float | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._home: tempfile.TemporaryDirectory[str] | None = None
        self._stderr: TextIO | None = None
        self._pending = b""
        self._ident = 0

    def __enter__(self) -> GraphSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> str:
        """Stop the process and remove its HOME; what the process wrote to stderr."""
        process, self._process = self._process, None
        diagnostic = ""
        if process and process.stdin and process.stdout and self._stderr:
            process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
            self._stderr.seek(self._stderr_start)  # what it wrote once answering, not its start-up banner
            diagnostic = self._stderr.read().strip()
            self._stderr.close()
        if self._home:
            self._home.cleanup()
            self._home = None
        return diagnostic

    def _send(self, method: str, params: dict[str, object]) -> int:
        process = self._process
        assert process and process.stdin
        self._ident += 1
        message = {"jsonrpc": "2.0", "id": self._ident, "method": method, "params": params}
        process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        return self._ident

    def _collect(self, wanted: set[int], deadline: float) -> dict[int, dict[str, object]]:
        """The JSON-RPC replies to `wanted` that arrive before the deadline or the process's end."""
        process = self._process
        assert process and process.stdout
        replies: dict[int, dict[str, object]] = {}
        try:
            while wanted - replies.keys():
                line, newline, rest = self._pending.partition(b"\n")
                if newline:
                    self._pending = rest
                    reply = json.loads(line)
                    if isinstance(reply, dict) and reply.get("id") in wanted:
                        replies[reply["id"]] = reply
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                self._pending += chunk
        except (OSError, json.JSONDecodeError):
            pass
        return replies

    def _start(self, deadline: float) -> bool:
        self._home = tempfile.TemporaryDirectory(prefix="rcf-gitnexus-")
        registry = Path(self._home.name) / ".gitnexus" / "registry.json"
        registry.parent.mkdir()
        registry.write_text(json.dumps([
            entry for entry in _read_registry(Path.home() / ".gitnexus" / "registry.json")
            if entry.get("name") == self.repo
        ]), encoding="utf-8")
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._stderr_start = 0
        self._pending = b""
        self._process = subprocess.Popen(
            # the core-dump guard run_cmd applies to every GitNexus process
            ["/bin/sh", "-c", 'printf 0 > /proc/self/coredump_filter && exec "$@"', "sh", str(self.binary), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env={**os.environ, "HOME": self._home.name},
            bufsize=0,
        )
        self.processes += 1
        self.started = self.started or time.monotonic()
        client = {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "repo-context-forge", "version": "1"}}
        try:
            if not self._collect({self._send("initialize", client)}, deadline):
                return False
            self._stderr_start = os.fstat(self._stderr.fileno()).st_size
            assert self._process.stdin
            self._process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        except OSError:
            return False
        return True

    def _answer(self, reply: dict[str, object]) -> Reply:
        malformed = "GitNexus returned malformed output"
        result = reply.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        text = content[0].get("text") if isinstance(content, list) and content and isinstance(content[0], dict) else None
        if not isinstance(result, dict) or not isinstance(text, str) or not self._home:
            return None, malformed
        if result.get("isError"):
            return {"error": text.removeprefix("Error: ")}, ""
        try:
            payload = json.loads(text)
            spilled = payload.get("complete_result") if isinstance(payload, dict) else None
            if spilled is not None:
                complete = Path(str(spilled)).resolve()
                if Path(self._home.name).resolve() not in complete.parents:
                    return None, malformed
                text = complete.read_text(encoding="utf-8")
                payload = json.loads(text)
        except (OSError, json.JSONDecodeError):
            return None, malformed
        size = len(text.encode("utf-8"))
        self.output_bytes += size
        if size > MAX_OUTPUT_BYTES:
            return None, "GitNexus check output exceeded the byte limit"
        if payload == []:  # a cypher query with no rows answers with an empty list
            payload = {}
        return (payload, "") if isinstance(payload, dict) else (None, malformed)

    def call_many(self, requests: list[tuple[str, dict[str, object]]], timeout: float) -> list[Reply]:
        """Send every tool call at once; per request its JSON answer, or None and the reason.

        A structured error is an answer. The server works on the calls together, so a batch
        costs about its slowest call. A call left unanswered ends the process; the next
        call starts another."""
        self.calls += len(requests)
        deadline = time.monotonic() + timeout
        sent: list[int] = []
        replies: dict[int, dict[str, object]] = {}
        if requests and (self._process or self._start(deadline)):
            try:
                for tool, arguments in requests:
                    sent.append(self._send("tools/call", {"name": tool, "arguments": {**arguments, "repo": self.repo}}))
            except OSError:
                pass
            replies = self._collect(set(sent), deadline)
        answers = {ident: self._answer(reply) for ident, reply in replies.items()}
        reason = ""
        if len(replies) < len(requests):
            timed_out = time.monotonic() >= deadline
            stderr = self.close()
            reason = "GitNexus check timed out" if timed_out else stderr or "GitNexus returned malformed output"
        sent += [0] * (len(requests) - len(sent))
        return [answers.get(ident, (None, reason)) for ident in sent]

    def call(self, tool: str, arguments: dict[str, object], timeout: float) -> Reply:
        return self.call_many([(tool, arguments)], timeout)[0]


def _table_rows(payload: dict[str, object] | None) -> list[list[str]] | None:
    """Rows of a cypher reply's markdown table; None when the query did not answer."""
    if payload is None or payload.get("error"):
        return None
    table = payload.get("markdown")
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in (table.splitlines()[2:] if isinstance(table, str) else [])
    ]


def rank_symbols(graph: GraphSession, slots: dict[str, int]) -> dict[str, list[str]] | None:
    """Per file, the names of its `slots` symbols with the most CALLS edges, most first.

    One bounded query whose reply grows with the slots, not the symbols. None when the
    query did not answer: there is then no ranking to plan from."""
    query = " UNION ALL ".join(
        'MATCH (n)-[r:CodeRelation]-() WHERE r.type = "CALLS" '
        f'AND n.filePath = {json.dumps(path, ensure_ascii=False)} AND labels(n)[0] <> "File" '
        f"RETURN n.filePath AS file, n.name AS name, count(*) AS edges ORDER BY edges DESC, name LIMIT {count}"
        for path, count in slots.items()
    )
    rows = _table_rows(graph.call("cypher", {"query": query}, CALL_TIMEOUT_SECONDS)[0])
    if rows is None:
        return None
    ranked: dict[str, list[str]] = {}
    for path, name, _edges in sorted(
        (row for row in rows if len(row) == 3 and row[2].isdigit()),
        key=lambda row: (-int(row[2]), row[1]),
    ):
        ranked.setdefault(path, []).append(name)
    return ranked


def _relations_query(identity: str, outgoing: bool) -> str:
    """Every dependency relation of `identity` in one direction, unlimited."""
    pattern = "(n)-[r:CodeRelation]->(other)" if outgoing else "(other)-[r:CodeRelation]->(n)"
    return (
        f"MATCH {pattern} WHERE n.id = {json.dumps(identity, ensure_ascii=False)} "
        f"AND r.type IN {json.dumps(DEPENDENCY_RELATIONS)} "
        "RETURN DISTINCT other.id AS id, other.name AS name, other.filePath AS file"
    )


def _all_references(
    identity: str, outgoing: bool, returned: list[dict[str, str]], payload: dict[str, object] | None,
) -> tuple[list[dict[str, str]], bool]:
    """A list GitNexus cut at its row limit, completed from `_relations_query`'s answer: every
    such relation joined with what GitNexus returned, in a fixed order, and whether the list
    may still be incomplete. A class's incoming list may be: GitNexus adds callers of its
    constructor and importers of its file, known here only as far as it returned them."""
    rows = _table_rows(payload)
    if rows is None:
        return returned, True
    found = {reference["identity"]: reference for reference in returned}
    found.update({row[0]: {"identity": row[0], "name": row[1], "file": row[2]} for row in rows if len(row) == 3})
    ordered = sorted(found.values(), key=lambda reference: (reference["file"], reference["identity"]))
    class_like = not outgoing and identity.split(":", 1)[0] in {"Class", "Interface"}
    return ordered, class_like


def execute(
    plan: list[dict[str, object]],
    gitnexus_status: dict[str, object],
    *,
    graph: GraphSession | None = None,
    omitted_check_count: int = 0,
    omitted_required_checks: list[dict[str, object]] | None = None,
    gitnexus_bin: str | None = None,
) -> dict[str, object]:
    """Answer every planned check from `graph`, or from a session of its own when none is given."""
    if graph is None:
        with GraphSession(str(gitnexus_status.get("repo") or ""), gitnexus_bin) as session:
            return execute(
                plan,
                gitnexus_status,
                graph=session,
                omitted_check_count=omitted_check_count,
                omitted_required_checks=omitted_required_checks,
            )
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
        "process_count": graph.processes,
        "graph_call_count": graph.calls,
        "output_bytes": graph.output_bytes,
        "estimated_output_tokens": 0,
        "omitted_check_count": omitted_check_count,
    }
    gitnexus_status["analysis"] = analysis
    if gitnexus_status.get("status") not in {"fresh", "reindexed"}:
        gitnexus_status["required_checks_resolved"] = False
        return gitnexus_status
    if not graph.binary:
        gitnexus_status.update(status="unavailable", required_checks_resolved=False)
        return gitnexus_status

    analysis_repo = Path(str(gitnexus_status.get("expected_repo_path") or ""))
    seen: set[tuple[str, ...]] = set()
    resolved_symbols: dict[tuple[str, str], str] = {}
    entries = analysis["entries"]
    unresolved = analysis["unresolved_checks"]
    assert isinstance(entries, list) and isinstance(unresolved, list)

    def unindexed_file(entry: dict[str, object], error: object) -> bool:
        # GitNexus indexes source files only. A file that exists in the checkout but has no
        # File node (a lockfile) cannot resolve a file_context check and must not block the
        # plan; a file that does not exist keeps blocking as the absent-file gap.
        target = str(entry["target"])
        if (
            entry["kind"] != "file_context"
            or error != f"Symbol 'File:{target}' not found"
            or not (analysis_repo / target).is_file()
        ):
            return False
        entry.update(status="unindexed", diagnostic="GitNexus does not index this file")
        return True

    def remaining() -> float:
        return max(0.0, min(CALL_TIMEOUT_SECONDS, TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started)))

    def single_eligible_candidate(payload: dict[str, object], file_path: str, target: str) -> str | None:
        # GitNexus answers "ambiguous" when a name is shared inside one file (a module
        # function and a dataclass field, say); the planned symbol is the one
        # Function/Class/Method candidate in the planned file, re-queried by uid. Among
        # several (a function and a same-named method), it is the one named exactly,
        # class-qualified when the intent qualified it.
        candidates = payload.get("candidates")
        eligible = [
            str(candidate["uid"])
            for candidate in (candidates if isinstance(candidates, list) else [])
            if isinstance(candidate, dict)
            and str(candidate.get("filePath") or "") == file_path
            and str(candidate.get("uid") or "").split(":", 1)[0] in {"Function", "Class", "Method"}
        ]
        if len(eligible) > 1:
            eligible = [uid for uid in eligible if uid.rsplit(":", 1)[-1] == target]
        return eligible[0] if len(eligible) == 1 else None

    limited: list[tuple[dict[str, object], str, str, bool]] = []

    def settle(
        entry: dict[str, object], kind: str, file_path: str, target: str, direction: str, member: str, reply: Reply,
    ) -> None:
        """Check one reply against its planned check and record the entry's facts."""

        def refused(answer: Reply) -> bool:
            # A structured error reply is kept so an unindexed file can be told from a failure.
            payload, diagnostic = answer
            if payload is not None and not payload.get("error"):
                return False
            entry["diagnostic"] = (diagnostic if payload is None else str(payload["error"]))[:1000]
            unindexed_file(entry, payload.get("error") if payload else None)
            return True

        if refused(reply):
            return
        payload = reply[0]
        assert payload is not None
        if kind == "symbol_context" and payload.get("status") == "ambiguous":
            uid = single_eligible_candidate(payload, file_path, member)
            if uid is None:
                entry["diagnostic"] = "GitNexus result is ambiguous and no single candidate matches the planned file"
                return
            reply = graph.call("context", {"uid": uid, "include_content": False}, remaining())
            if refused(reply):
                return
            payload = reply[0]
            assert payload is not None

        entity = payload.get("symbol") if kind.endswith("context") else payload.get("target")
        if not isinstance(entity, dict):
            entry["diagnostic"] = "GitNexus result has no resolved entity"
            return
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
            return
        if kind == "symbol_impact":
            if resolved_identity != resolved_symbols.get((file_path, member)):
                entry["diagnostic"] = "GitNexus impact identity does not match file-resolved context"
                return
            if str(payload.get("direction") or "") != (direction or "upstream"):
                entry["diagnostic"] = "GitNexus impact direction does not match the planned check"
                return

        entry.update(
            status="resolved",
            resolved_identity=resolved_identity,
            entity={"name": resolved_name, "file": resolved_file},
        )
        if kind == "symbol_context":
            resolved_symbols[(file_path, member)] = resolved_identity
            for key, side in (("callers", "incoming"), ("callees", "outgoing")):
                relations = payload.get(side)
                entry[key] = _references(relations)
                if isinstance(relations, dict) and CONTEXT_ROW_LIMIT <= sum(
                    len(items) for items in relations.values() if isinstance(items, list)
                ):
                    limited.append((entry, key, resolved_identity, side == "outgoing"))
        elif kind == "file_context":
            entry["references"] = _references(payload.get("incoming"))
        else:
            impacted = _references(payload.get("byDepth"))
            entry["impacted_files"] = list(
                dict.fromkeys(reference["file"] for reference in impacted if reference["file"])
            )[:MAX_FACTS]
            count = payload.get("impactedCount")
            entry["impacted_count"] = count if isinstance(count, int) else len(impacted)
            entry["risk"] = str(payload.get("risk") or "unknown")

    def complete(entry: dict[str, object], key: str, identity: str, outgoing: bool, reply: Reply) -> None:
        returned = entry[key]
        assert isinstance(returned, list)
        entry[key], still_limited = _all_references(identity, outgoing, returned, reply[0])
        if still_limited:
            entry[f"{key}_limited"] = True

    checks: list[tuple[dict[str, object], str, str, str, str, str]] = []
    for item in plan:
        key = _check_key(item)
        kind, file_path, target, direction = key
        member = str(item.get("qualified_target") or target)
        if (*key, member) in seen:
            continue
        seen.add((*key, member))
        entry: dict[str, object] = {
            "kind": kind,
            "file": file_path,
            "target": target,
            "direction": direction,
            "status": "unresolved",
        }
        entries.append(entry)
        checks.append((entry, kind, file_path, target, direction, member))

    # Two batches, each sent at once: every context, then every impact, which needs its
    # context's identity, together with the queries completing lists cut at the row limit.
    for impacts in (False, True):
        requests: list[tuple[Callable[[Reply], None], str, dict[str, object]]] = []
        for check in checks:
            entry, kind, file_path, target, direction, member = check
            if (kind == "symbol_impact") is not impacts:
                continue
            identity = resolved_symbols.get((file_path, member))
            if time.monotonic() - started >= TOTAL_TIMEOUT_SECONDS:
                entry["diagnostic"] = "GitNexus total timeout exceeded"
            elif kind == "file_context":
                requests.append((functools.partial(settle, *check), "context", {"uid": f"File:{target}", "include_content": False}))
            elif kind == "symbol_context":
                requests.append((
                    functools.partial(settle, *check),
                    "context",
                    {"name": target, "file_path": file_path, "include_content": False},
                ))
            elif kind != "symbol_impact":
                entry["diagnostic"] = f"unsupported GitNexus check kind: {kind}"
            elif not identity:
                entry["diagnostic"] = "GitNexus impact has no file-resolved context identity"
            else:
                requests.append((
                    functools.partial(settle, *check),
                    "impact",
                    {"uid": identity, "direction": direction or "upstream", "includeTests": True},
                ))
        if impacts:
            requests += [
                (functools.partial(complete, *cut), "cypher", {"query": _relations_query(cut[2], cut[3])})
                for cut in limited
            ]
        replies = graph.call_many([(tool, arguments) for _finish, tool, arguments in requests], remaining())
        for (finish, _tool, _arguments), reply in zip(requests, replies):
            finish(reply)
    unresolved.extend(dict(entry) for entry in entries if entry["status"] == "unresolved")

    analysis.update(
        elapsed_ms=round((time.monotonic() - (graph.started or started)) * 1000),
        process_count=graph.processes,
        graph_call_count=graph.calls,
        output_bytes=graph.output_bytes,
    )
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
