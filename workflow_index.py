from __future__ import annotations

import html
import os
import re
import sqlite3
import tempfile
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable


INDEX_DIR = ".repo-context-forge"
INDEX_DB = "workflow-index.sqlite3"
SCHEMA_VERSION = 2
SOURCE_EXTENSIONS = {".js", ".jsx", ".mjs", ".py", ".ts", ".tsx"}
STRUCTURED_EXTENSIONS = {".json", ".toml", ".yaml", ".yml"}
FILE_COLUMNS = "path, base_score, symbol_count, line_count, base_rank"
SYMBOL_COLUMNS = "name, kind, line, end_line, signature, is_exported, summary, summary_source"

RoleForPath = Callable[[str], str]
SummaryForSymbol = Callable[[str, str, str], str]


@dataclass(frozen=True)
class IndexedFile:
    """Compatibility DTO; pagerank is workflow relevance, not graph PageRank."""

    path: str
    pagerank: float
    symbol_count: int
    line_count: int
    rank: int


@dataclass(frozen=True)
class IndexedSymbol:
    name: str
    kind: str
    line: int
    end_line: int
    signature: str
    is_exported: bool
    summary: str
    summary_source: str = "workflow_index"


class WorkflowIndex:
    """Exact-head repository orientation index used by packet generation."""

    def __init__(self, repo: Path, role_for_path: RoleForPath) -> None:
        self.repo = repo
        self.role_for_path = role_for_path
        self.db_path = repo / INDEX_DIR / INDEX_DB
        self._read_warning: str | None = None

    @property
    def is_available(self) -> bool:
        return self.db_path.is_file()

    def build(
        self,
        paths: Iterable[str],
        *,
        head_sha: str,
        dirty_overlay: bool = False,
        summary_for_symbol: SummaryForSymbol,
    ) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{INDEX_DB}.", suffix=".tmp", dir=self.db_path.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary_path)) as conn, conn:
                self._create_schema(conn)
                for rank, row in enumerate(
                    self._file_rows(paths, summary_for_symbol), start=1
                ):
                    base_score, path, role, extension, line_count, symbols, search_terms = row
                    conn.execute(
                        """
                        INSERT INTO files (
                          path, role, extension, line_count, symbol_count,
                          base_score, base_rank, search_terms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            path,
                            role,
                            extension,
                            line_count,
                            len(symbols),
                            base_score,
                            rank,
                            search_terms,
                        ),
                    )
                    conn.executemany(
                        """
                        INSERT INTO symbols (
                          file_path, name, kind, line, end_line, signature,
                          is_exported, summary, summary_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                path,
                                symbol.name,
                                symbol.kind,
                                symbol.line,
                                symbol.end_line,
                                symbol.signature,
                                1 if symbol.is_exported else 0,
                                symbol.summary,
                                symbol.summary_source,
                            )
                            for symbol in symbols
                        ],
                    )
                conn.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("head_sha", head_sha),
                        ("dirty_overlay", str(dirty_overlay).lower()),
                    ],
                )
            os.replace(temporary_path, self.db_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def status(self) -> dict[str, object]:
        if not self.is_available:
            return {"available": False, "db_path": str(self.db_path)}
        try:
            with self._open() as conn:
                files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
                symbols = int(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
                metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            schema_version = int(metadata["schema_version"])
            head_sha = metadata["head_sha"]
            dirty_overlay = metadata["dirty_overlay"]
            if schema_version != SCHEMA_VERSION:
                raise ValueError(f"unsupported schema_version {schema_version}")
            if not head_sha:
                raise ValueError("head_sha is empty")
            if dirty_overlay not in {"true", "false"}:
                raise ValueError(f"invalid dirty_overlay {dirty_overlay!r}")
        except (KeyError, ValueError, sqlite3.Error) as exc:
            warning = f"invalid workflow index: {exc}"
            if self._read_warning:
                warning = f"{warning}; {self._read_warning}"
            return {
                "available": False,
                "db_path": str(self.db_path),
                "warning": warning,
            }
        result: dict[str, object] = {
            "available": True,
            "db_path": str(self.db_path),
            "files": files,
            "symbols": symbols,
            "head_sha": head_sha,
            "dirty_overlay": dirty_overlay == "true",
            "schema_version": schema_version,
        }
        if self._read_warning:
            result["warning"] = self._read_warning
        return result

    def is_reusable(self, head_sha: str) -> bool:
        status = self.status()
        return (
            status.get("available") is True
            and status.get("head_sha") == head_sha
            and status.get("dirty_overlay") is False
        )

    def ensure_current(
        self,
        paths: Iterable[str],
        *,
        head_sha: str,
        dirty_overlay: bool,
        summary_for_symbol: SummaryForSymbol,
        reuse: bool,
    ) -> None:
        if reuse and self.is_reusable(head_sha):
            return
        self.build(
            paths,
            head_sha=head_sha,
            dirty_overlay=dirty_overlay,
            summary_for_symbol=summary_for_symbol,
        )

    def ranked_files(self, limit: int) -> list[IndexedFile]:
        if not self.is_available or limit <= 0:
            return []
        rows = self._select_files(
            "ORDER BY role != 'production', base_score DESC, path ASC LIMIT ?",
            (limit,),
        )
        return [self._indexed_file(row) for row in rows]

    def lookup_files(self, paths: Iterable[str]) -> dict[str, IndexedFile]:
        wanted = list(dict.fromkeys(paths))
        if not self.is_available or not wanted:
            return {}
        marks = ",".join("?" for _ in wanted)
        rows = self._select_files(f"WHERE path IN ({marks})", wanted)
        return {str(row[0]): self._indexed_file(row) for row in rows}

    def file_symbols(self, path: str, limit: int) -> list[IndexedSymbol]:
        if limit < 1 or not self.is_available:
            return []
        rows = self._fetchall(
            f"""SELECT {SYMBOL_COLUMNS} FROM symbols
            WHERE file_path = ?
            ORDER BY is_exported DESC, line ASC
            LIMIT ?""",
            (path, limit),
        )
        return [
            IndexedSymbol(
                name=str(name),
                kind=str(kind),
                line=int(line),
                end_line=int(end_line),
                signature=str(signature),
                is_exported=bool(is_exported),
                summary=str(summary),
                summary_source=str(summary_source),
            )
            for name, kind, line, end_line, signature, is_exported, summary, summary_source in rows
        ]

    def rank_intent(self, tokens: list[str], limit: int) -> list[str]:
        if not self.is_available or not tokens or limit <= 0:
            return []
        forms = self._intent_forms([token.lower() for token in tokens])
        candidates: list[str] = []
        for token, singular in forms:
            candidates.extend((token, singular) if singular else (token,))
        candidates = list(dict.fromkeys(candidates))
        predicates = " OR ".join("instr(search_terms, ?) > 0" for _ in candidates)
        rows = self._fetchall(
            f"""
            SELECT path, role, base_score, search_terms
            FROM files
            WHERE {predicates}
            """,
            candidates,
        )
        scored: list[tuple[float, str, str]] = []
        for path, role, base_score, search_terms in rows:
            score = float(base_score)
            matched = False
            haystack = str(search_terms)
            for token, singular in forms:
                if re.search(rf"\b{re.escape(token)}\b", haystack):
                    score += 45
                    matched = True
                elif singular and re.search(rf"\b{re.escape(singular)}\b", haystack):
                    score += 35
                    matched = True
            if matched:
                scored.append((score, str(role), str(path)))
        return [
            path
            for _score, _role, path in sorted(
                scored,
                key=lambda item: (item[1] != "production", -item[0], item[2]),
            )[:limit]
        ]

    def related_paths(self, paths: Iterable[str], limit: int) -> list[str]:
        if not self.is_available or limit <= 0:
            return []
        base_paths = set(paths)
        stems = {self._logical_stem(path) for path in base_paths}
        parent_dirs = {str(Path(path).parent) for path in base_paths}
        candidates = [str(row[0]) for row in self._fetchall("SELECT path FROM files")]
        scores: dict[str, float] = {}
        for candidate in candidates:
            if candidate in base_paths:
                continue
            candidate_path = Path(candidate)
            candidate_stem = self._logical_stem(candidate)
            score = 0.0
            if candidate_stem in stems:
                score += 80
            if str(candidate_path.parent) in parent_dirs:
                score += 20
            if self.role_for_path(candidate) == "test" and candidate_stem in stems:
                score += 30
            if score:
                scores[candidate] = score
        return [
            path
            for path, _score in sorted(
                scores.items(), key=lambda item: (-item[1], item[0])
            )[:limit]
        ]

    def _open(self) -> AbstractContextManager[sqlite3.Connection]:
        return closing(
            sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        )

    def _fetchall(
        self, query: str, parameters: Iterable[object] = ()
    ) -> list[tuple[object, ...]]:
        try:
            with self._open() as conn:
                rows = conn.execute(query, tuple(parameters)).fetchall()
            self._read_warning = None
            return rows
        except sqlite3.Error as exc:
            self._read_warning = f"workflow index read failed: {exc}"
            return []

    def _select_files(
        self, clause: str, parameters: Iterable[object]
    ) -> list[tuple[object, ...]]:
        return self._fetchall(f"SELECT {FILE_COLUMNS} FROM files {clause}", parameters)

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (
              path TEXT PRIMARY KEY,
              role TEXT NOT NULL,
              extension TEXT NOT NULL,
              line_count INTEGER NOT NULL,
              symbol_count INTEGER NOT NULL,
              base_score REAL NOT NULL,
              base_rank INTEGER NOT NULL,
              search_terms TEXT NOT NULL
            );
            CREATE TABLE symbols (
              file_path TEXT NOT NULL,
              name TEXT NOT NULL,
              kind TEXT NOT NULL,
              line INTEGER NOT NULL,
              end_line INTEGER NOT NULL,
              signature TEXT NOT NULL,
              is_exported INTEGER NOT NULL,
              summary TEXT NOT NULL,
              summary_source TEXT NOT NULL,
              PRIMARY KEY (file_path, name, line)
            );
            CREATE INDEX idx_files_role_rank ON files(role, base_rank);
            CREATE INDEX idx_symbols_file ON symbols(file_path, line);
            """
        )

    def _file_rows(
        self,
        paths: Iterable[str],
        summary_for_symbol: SummaryForSymbol,
    ) -> list[tuple[float, str, str, str, int, list[IndexedSymbol], str]]:
        rows = []
        for path in dict.fromkeys(paths):
            target = self.repo / path
            if not target.is_file():
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            symbols = self._extract_symbols(path, content, summary_for_symbol)
            line_count = len(content.splitlines())
            role = self.role_for_path(path)
            rows.append(
                (
                    self._base_score(path, role, line_count, len(symbols)),
                    path,
                    role,
                    Path(path).suffix.lower(),
                    line_count,
                    symbols,
                    self._search_terms(path, symbols),
                )
            )
        return sorted(rows, key=lambda item: (item[2] != "production", -item[0], item[1]))

    @staticmethod
    def _extract_symbols(
        path: str,
        content: str,
        summary_for_symbol: SummaryForSymbol,
    ) -> list[IndexedSymbol]:
        """Index declarations with direct same-line export heuristics."""
        extension = Path(path).suffix.lower()
        patterns: list[tuple[str, str]] = []
        if extension == ".py":
            patterns = [
                ("function", r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
                ("class", r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
            ]
        elif extension in SOURCE_EXTENSIONS:
            patterns = [
                ("function", r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("),
                ("function", r"^\s*(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("),
                ("class", r"^\s*export\s+(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"),
                ("class", r"^\s*class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"),
                ("arrow", r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\("),
            ]
        symbols: list[IndexedSymbol] = []
        indentations: list[int] = []
        seen: set[tuple[str, int]] = set()
        offset = 0
        lines = content.splitlines(keepends=True)
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.rstrip("\r\n")
            for kind, pattern in patterns:
                match = re.search(pattern, line)
                if not match:
                    continue
                if kind == "arrow" and not WorkflowIndex._is_arrow_initializer(
                    content, offset + match.end() - 1
                ):
                    continue
                name = match.group(1)
                key = (name, line_number)
                if key in seen:
                    continue
                seen.add(key)
                indentations.append(len(line) - len(line.lstrip()))
                symbol_kind = "function" if kind == "arrow" else kind
                symbols.append(
                    IndexedSymbol(
                        name=name,
                        kind=symbol_kind,
                        line=line_number,
                        end_line=line_number,
                        signature=line.strip(),
                        is_exported=(
                            line == line.lstrip() and not name.startswith("_")
                            if extension == ".py"
                            else line.lstrip().startswith("export ")
                        ),
                        summary=summary_for_symbol(path, name, symbol_kind),
                    )
                )
            offset += len(raw_line)
        for index, symbol in enumerate(symbols):
            end_line = len(lines)
            for next_index in range(index + 1, len(symbols)):
                if indentations[next_index] <= indentations[index]:
                    end_line = symbols[next_index].line - 1
                    break
            symbols[index] = replace(symbol, end_line=end_line)
        return symbols

    @staticmethod
    def _is_arrow_initializer(content: str, open_paren: int) -> bool:
        depth = 0
        quote = ""
        escaped = False
        index = open_paren
        while index < len(content):
            char = content[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in {'"', "'", "`"}:
                quote = char
            elif content.startswith("//", index):
                index = content.find("\n", index)
                if index < 0:
                    return False
            elif content.startswith("/*", index):
                index = content.find("*/", index + 2)
                if index < 0:
                    return False
                index += 1
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    tail = content[index + 1 :]
                    direct = re.match(r"\s*=>", tail)
                    if direct:
                        return True
                    annotation = re.match(r"\s*:", tail)
                    if not annotation:
                        return False
                    nesting = 0
                    for tail_index in range(annotation.end(), len(tail)):
                        if tail.startswith("=>", tail_index) and nesting == 0:
                            return True
                        tail_char = tail[tail_index]
                        if tail_char in "([{":
                            nesting += 1
                        elif tail_char in ")]}" and nesting:
                            nesting -= 1
                        elif tail_char in ";=" and nesting == 0:
                            return False
                    return False
            index += 1
        return False

    @staticmethod
    def _base_score(path: str, role: str, line_count: int, symbol_count: int) -> float:
        extension = Path(path).suffix.lower()
        score = 60.0 if role == "production" else 35.0 if role == "test" else 5.0
        if extension in SOURCE_EXTENSIONS:
            score += 35
        elif extension in STRUCTURED_EXTENSIONS:
            score += 15
        elif extension in {".md", ".mdx", ".rst"}:
            score += 8
        score += min(symbol_count * 4, 40)
        score += min(line_count / 20, 25)
        return score - path.count("/") * 0.5

    @staticmethod
    def _search_terms(path: str, symbols: list[IndexedSymbol]) -> str:
        values = [path, Path(path).name]
        values.extend(symbol.name for symbol in symbols)
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", " ".join(values))
        separators = re.sub(r"[^A-Za-z0-9]+", " ", spaced)
        return f"{' '.join(values)} {separators}".lower()

    @staticmethod
    def _intent_forms(tokens: list[str]) -> list[tuple[str, str | None]]:
        return [(token, WorkflowIndex._singular_intent_token(token)) for token in tokens]

    @staticmethod
    def _singular_intent_token(token: str) -> str | None:
        if len(token) > 4 and token.endswith("ies"):
            return token[:-3] + "y"
        if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
            return token[:-1]
        return None

    @staticmethod
    def _logical_stem(path: str) -> str:
        return Path(path).stem.replace(".test", "").replace(".spec", "").removeprefix("test_")

    @staticmethod
    def _indexed_file(row: tuple[object, ...]) -> IndexedFile:
        path, score, symbol_count, line_count, rank = row
        return IndexedFile(
            path=str(path),
            pagerank=max(float(score), 0) / 1000,
            symbol_count=int(symbol_count),
            line_count=int(line_count),
            rank=int(rank),
        )


def semantic_summary(targets: list[dict[str, object]]) -> dict[str, object]:
    counts: dict[str, int] = {}
    for target in targets:
        symbols = target.get("symbols")
        for symbol in symbols if isinstance(symbols, list) else []:
            if isinstance(symbol, dict):
                source = str(symbol.get("summary_source") or "none")
                counts[source] = counts.get(source, 0) + 1
    return {
        "mode": "full_cached",
        "synthetic_fill": True,
        "live_llm_generation": False,
        "source_counts": dict(sorted(counts.items())),
    }


def summarize_architecture(targets: list[dict[str, object]]) -> dict[str, object]:
    roles: dict[str, int] = {}
    hotspots: list[dict[str, object]] = []
    entry_points: list[str] = []
    related_surfaces: list[str] = []
    for target in targets:
        role = str(target.get("surface_role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
        impact = target.get("soulforge_impact")
        impact = impact if isinstance(impact, dict) else {}
        path = str(target.get("path") or "")
        risk = str(impact.get("risk") or "unknown")
        direct = _integer(impact.get("direct_dependents"))
        total = _integer(impact.get("total_affected_scope"))
        if path and (risk not in {"low", "unknown"} or direct or total):
            hotspots.append(
                {
                    "path": path,
                    "risk": risk,
                    "direct_dependents": direct,
                    "total_affected": total,
                }
            )
        if role == "production":
            symbols = target.get("changed_symbols") or target.get("symbols") or []
            for symbol in symbols if isinstance(symbols, list) else []:
                if not isinstance(symbol, dict) or symbol.get("kind") not in {
                    "function",
                    "class",
                    "method",
                }:
                    continue
                name = symbol.get("name")
                if isinstance(name, str) and name and name not in entry_points:
                    entry_points.append(name)
        for field in ("graph_neighbors", "cochanges"):
            values = target.get(field)
            for item in values if isinstance(values, list) else []:
                related = _related_path(item)
                if related and related not in related_surfaces:
                    related_surfaces.append(related)
    hotspots.sort(
        key=lambda item: (
            -_integer(item["direct_dependents"]),
            -_integer(item["total_affected"]),
            str(item["path"]),
        )
    )
    return {
        "roles": dict(sorted(roles.items())),
        "hotspots": hotspots[:5],
        "entry_points": entry_points[:8],
        "related_surfaces": related_surfaces[:8],
    }


def architecture_lines(summary: object) -> list[str]:
    summary = summary if isinstance(summary, dict) else {}
    roles = summary.get("roles") if isinstance(summary.get("roles"), dict) else {}
    hotspots = summary.get("hotspots") if isinstance(summary.get("hotspots"), list) else []
    entries = summary.get("entry_points") if isinstance(summary.get("entry_points"), list) else []
    related = summary.get("related_surfaces") if isinstance(summary.get("related_surfaces"), list) else []
    lines = [
        "architecture_summary:",
        "- roles | " + (",".join(f"{role}={roles[role]}" for role in sorted(roles)) or "none"),
    ]
    lines.extend(
        "- hotspots | "
        f"{item.get('path') or ''} risk={item.get('risk') or 'unknown'} "
        f"direct_dependents={item.get('direct_dependents') or 0} "
        f"total_affected={item.get('total_affected') or 0}"
        for item in hotspots[:5]
        if isinstance(item, dict)
    )
    if not hotspots:
        lines.append("- hotspots | none")
    lines.append("- entry_points | " + (",".join(map(str, entries[:8])) or "none"))
    lines.append("- related_surfaces | " + (",".join(map(str, related[:8])) or "none"))
    return lines


def context_digest_lines(
    packet: dict[str, object], indent: str, default_token_budget: int
) -> list[str]:
    mode = packet.get("mode")
    target_state = packet.get("target_state")
    gitnexus = packet.get("gitnexus")
    workflow = packet.get("workflow_index", {})
    semantic = packet.get("semantic_summaries", {})
    targets = packet.get("targets")
    if not isinstance(mode, str):
        raise ValueError("packet mode must be a string")
    if not isinstance(target_state, dict):
        raise ValueError("packet target_state must be an object")
    if not isinstance(gitnexus, dict):
        raise ValueError("packet gitnexus must be an object")
    if not isinstance(workflow, dict):
        raise ValueError("packet workflow_index must be an object")
    if not isinstance(semantic, dict):
        raise ValueError("packet semantic_summaries must be an object")
    if not isinstance(targets, list):
        raise ValueError("packet targets must be a list")
    architecture = packet.get("architecture_summary") or summarize_architecture(
        [target for target in targets if isinstance(target, dict)]
    )
    lines = [
        f"{indent}<context_digest>",
        (
            f"{indent}  <required_agent_intake>State mode, head_sha, token_budget, "
            "semantic sources, architecture summary, top targets, SoulForge impact, "
            "GitNexus authority repo/status, and coverage_plan before code reasoning."
            "</required_agent_intake>"
        ),
        f"{indent}  <mode>{html.escape(mode)}</mode>",
        f"{indent}  <head_sha>{html.escape(str(target_state.get('head_sha') or ''))}</head_sha>",
        (
            f"{indent}  <token_budget>"
            f"{html.escape(str(packet.get('token_budget') or default_token_budget))}"
            "</token_budget>"
        ),
        (
            f"{indent}  <workflow_index "
            f"available=\"{str(workflow.get('available', False)).lower()}\" "
            f"head_sha=\"{html.escape(str(workflow.get('head_sha') or ''))}\" "
            f"dirty_overlay=\"{str(workflow.get('dirty_overlay', False)).lower()}\" "
            f"files=\"{html.escape(str(workflow.get('files') or 0))}\" "
            f"symbols=\"{html.escape(str(workflow.get('symbols') or 0))}\"/>"
        ),
        f"{indent}  <semantic_mode>{html.escape(str(semantic.get('mode') or 'unknown'))}</semantic_mode>",
        f"{indent}  <semantic_sources>",
    ]
    lines.extend(source_count_lines(semantic, f"{indent}    "))
    lines.extend(
        [
            f"{indent}  </semantic_sources>",
            (
                f"{indent}  <gitnexus authority=\"packet\" "
                f"repo=\"{html.escape(str(gitnexus.get('repo') or ''))}\" "
                f"status=\"{html.escape(str(gitnexus.get('status') or 'unknown'))}\" "
                f"expected_head_sha=\"{html.escape(str(gitnexus.get('expected_head_sha') or ''))}\" "
                f"indexed_head_sha=\"{html.escape(str(gitnexus.get('indexed_head_sha') or ''))}\" "
                "required_checks_resolved=\""
                f"{str(gitnexus.get('required_checks_resolved', False)).lower()}\"/>"
            ),
        ]
    )
    lines.extend(_architecture_xml_lines(architecture, f"{indent}  "))
    lines.append(f"{indent}  <top_targets>")
    for target in targets[:5]:
        if not isinstance(target, dict):
            continue
        impact = target.get("soulforge_impact")
        impact = impact if isinstance(impact, dict) else {}
        signals = target.get("rank_signals")
        signal_text = ",".join(map(str, signals)) if isinstance(signals, list) else ""
        lines.append(
            f"{indent}    <file path=\"{html.escape(str(target.get('path') or ''))}\" "
            f"score=\"{html.escape(str(target.get('priority_score') or 0))}\" "
            f"role=\"{html.escape(str(target.get('surface_role') or 'unknown'))}\" "
            f"risk=\"{html.escape(str(impact.get('risk') or 'unknown'))}\" "
            f"direct_dependents=\"{html.escape(str(impact.get('direct_dependents') or 0))}\" "
            f"signals=\"{html.escape(signal_text)}\"/>"
        )
    lines.extend([f"{indent}  </top_targets>", f"{indent}</context_digest>"])
    return lines


def source_count_lines(semantic: dict[str, object], indent: str) -> list[str]:
    counts = semantic.get("source_counts")
    if not isinstance(counts, dict):
        return []
    return [
        f'{indent}<source name="{html.escape(str(source))}" '
        f'count="{html.escape(str(count))}"/>'
        for source, count in counts.items()
    ]


def _architecture_xml_lines(summary: object, indent: str) -> list[str]:
    return [f"{indent}<architecture_summary>"] + [
        f"{indent}  <line>{html.escape(line)}</line>"
        for line in architecture_lines(summary)[1:]
    ] + [f"{indent}</architecture_summary>"]


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _related_path(item: object) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and isinstance(item.get("path"), str):
        return str(item["path"])
    return None
