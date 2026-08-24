from __future__ import annotations

import html
import os
import re
import sqlite3
import tempfile
import tokenize
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable


INDEX_DIR = ".repo-context-forge"
INDEX_DB = "workflow-index.sqlite3"
SCHEMA_VERSION = 8
SOURCE_EXTENSIONS = {".js", ".jsx", ".mjs", ".py", ".ts", ".tsx"}
STRUCTURED_EXTENSIONS = {".json", ".toml", ".yaml", ".yml"}
FILE_COLUMNS = "path, base_score, symbol_count, line_count, base_rank"
SYMBOL_COLUMNS = "name, kind, line, end_line, signature, is_exported, summary, summary_source"

RoleForPath = Callable[[str], str]
SummaryForSymbol = Callable[[str, str, str], str]


def identifier_words(value: str) -> tuple[str, ...]:
    return tuple(
        word.lower()
        for chunk in re.split(r"[^A-Za-z0-9]+", value)
        for word in re.findall(
            r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", chunk)
    )


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


@dataclass(frozen=True)
class IntentSymbolMatch:
    path: str
    symbol: IndexedSymbol


@dataclass(frozen=True)
class IntentCoverageGap:
    kind: str
    reference: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class IntentSymbolRelevance:
    path: str
    line: int
    name: str
    score: int
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class IntentFileEvidence:
    path: str
    relevance_score: float
    exact_file: bool
    matched_terms: tuple[str, ...]
    matched_symbols: tuple[str, ...]


@dataclass(frozen=True)
class IntentResolution:
    file_evidence: tuple[IntentFileEvidence, ...]
    required_symbols: tuple[IntentSymbolMatch, ...]
    symbol_relevance: tuple[IntentSymbolRelevance, ...]
    coverage_gaps: tuple[IntentCoverageGap, ...]


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

    def exact_symbols(
        self, names: Iterable[str]
    ) -> dict[str, list[tuple[str, IndexedSymbol]]]:
        wanted = list(dict.fromkeys(name for name in names if name))
        if not self.is_available or not wanted:
            return {}
        marks = ",".join("?" for _ in wanted)
        rows = self._fetchall(
            f"""SELECT file_path, {SYMBOL_COLUMNS} FROM symbols
            WHERE name IN ({marks})
            ORDER BY name ASC, file_path ASC, line ASC""",
            wanted,
        )
        matches: dict[str, list[tuple[str, IndexedSymbol]]] = {}
        for file_path, name, kind, line, end_line, signature, is_exported, summary, summary_source in rows:
            symbol = IndexedSymbol(
                name=str(name),
                kind=str(kind),
                line=int(line),
                end_line=int(end_line),
                signature=str(signature),
                is_exported=bool(is_exported),
                summary=str(summary),
                summary_source=str(summary_source),
            )
            matches.setdefault(symbol.name, []).append((str(file_path), symbol))
        return matches

    def resolve_intent(self, intent: str) -> IntentResolution:
        file_rows = self._fetchall("SELECT path, role, base_score, search_terms FROM files")
        file_roles = {str(path): str(role) for path, role, _score, _terms in file_rows}
        qualified_reference_matches = [
            match
            for match in re.finditer(
                r"(?<![/A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*\b",
                intent,
            )
            if match.group(0).rsplit(".", 1)[-1].lower()
            not in {"js", "json", "md", "py", "sh", "ts", "tsx", "yaml", "yml"}
        ]
        qualified_references = list(dict.fromkeys(
            match.group(0) for match in qualified_reference_matches
        ))
        path_tokens = {path for path in re.findall(
            r"(?<![/A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*", intent) if not path.endswith("/")}
        explicit_paths = set(re.findall(r"`((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)`", intent)) | set(re.findall(r"(?i)\b(?:update|modify|change|fix|edit|remove|delete|add|create)\s+`((?:[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+|\.[A-Za-z0-9_-][A-Za-z0-9_.-]*))`(?=\s+behavior\b|\.?$)", intent))
        creation_paths = set(re.findall(r"(?i)\b(?:add|create)\s+`?((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+)`?", intent))
        root_paths = set(re.findall(r"(?i)\b(?:update|modify|change|fix|edit|remove|delete|add|create)\s+([A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+)\b(?=\s+behavior\b|\.?$)", intent))
        intent_paths = path_tokens | explicit_paths | creation_paths | root_paths
        exact_intent_paths = {
            path if path in file_roles else path.rstrip(".")
            for path in intent_paths
        } & file_roles.keys()
        path_prefix_references = {
            reference
            for reference in qualified_references
            if any(path.startswith(f"{reference}/") for path in exact_intent_paths)
            and all(
                match.end() < len(intent) and intent[match.end()] == "/"
                for match in qualified_reference_matches
                if match.group(0) == reference
            )
        }
        path_bound_references = path_prefix_references | (
            set(qualified_references) & exact_intent_paths
        )
        path_tokens |= explicit_paths | creation_paths | (
            root_paths - (set(qualified_references) - path_bound_references)
        )
        code_identifiers = re.findall(
            r"`([A-Za-z_][A-Za-z0-9_]*)`",
            intent,
        )
        bare_identifiers = list(dict.fromkeys(
            match.group(0)
            for match in re.finditer(
                r"\b[A-Za-z_][A-Za-z0-9_]*\b",
                re.sub(r"`[^`]*`", lambda match: " " * len(match.group(0)), intent),
            )
            if ("_" in match.group(0) or any(character.isupper()
                                               for character in match.group(0)[1:])) and not (
                (match.start() > 0 and intent[match.start() - 1] in "./`")
                or (match.end() < len(intent) and (
                    intent[match.end()] in "/`" or (
                        intent[match.end()] == "." and match.end() + 1 < len(intent)
                        and re.match(r"[A-Za-z_]", intent[match.end() + 1]))
                ))
            )
        ))
        identifiers = list(dict.fromkeys([*code_identifiers, *bare_identifiers]))
        required: list[IntentSymbolMatch] = []
        gaps: list[IntentCoverageGap] = []
        qualified_matches = self.exact_symbols(
            reference.rsplit(".", 1)[-1] for reference in qualified_references
        )
        created_references = set(re.findall(
            r"\b(?:add|create|introduce)\s+(?:(?:a|an|new|class|function|method)\s+)*`?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
            intent,
            re.IGNORECASE,
        ))
        created_identifiers = {
            reference.rsplit(".", 1)[-1] for reference in created_references}
        for reference in qualified_references:
            if reference in path_prefix_references:
                continue
            qualifier, name = reference.rsplit(".", 1)
            qualifier = qualifier.rsplit(".", 1)[-1]
            candidates = qualified_matches.get(name, [])
            class_matches = []
            for path, symbol in candidates:
                owner = min(
                    (
                        container
                        for container in self.file_symbols(path, 1_000_000)
                        if container.line < symbol.line
                        and symbol.end_line <= container.end_line
                    ),
                    key=lambda container: container.end_line - container.line,
                    default=None,
                )
                if owner is not None and owner.kind == "class" and owner.name == qualifier:
                    class_matches.append((path, symbol))
            qualifier_words = set(identifier_words(qualifier))
            file_matches = [
                (path, symbol)
                for path, symbol in candidates
                if qualifier_words <= set(identifier_words(path))
            ]
            selected = (
                class_matches if len(class_matches) == 1
                else file_matches if not class_matches and len(file_matches) == 1
                else candidates if not class_matches and len(candidates) == 1
                else []
            )
            required.extend(
                IntentSymbolMatch(path=path, symbol=symbol)
                for path, symbol in selected
            )
            if (
                not selected
                and reference not in created_references
                and (candidates or reference not in path_bound_references)
            ):
                gaps.append(IntentCoverageGap(
                    kind="ambiguous_symbol" if candidates else "absent_symbol",
                    reference=reference,
                    candidates=tuple(path for path, _symbol in candidates),
                ))
        unqualified_matches = self.exact_symbols(identifiers)
        for reference in identifiers:
            matches = unqualified_matches.get(reference, [])
            if len(matches) == 1:
                required.append(IntentSymbolMatch(path=matches[0][0], symbol=matches[0][1]))
            elif matches or (
                reference not in created_identifiers
                and (
                    reference in code_identifiers
                    or (
                        reference in bare_identifiers
                        and not reference.isupper()
                        and re.search(
                            rf"(?:\b(?:update|modify|change|fix|edit|remove|delete)\s+{re.escape(reference)}\b[.!?]?\s*$|\b{re.escape(reference)}\s+(?:behavior|implementation|definition|callers?)\b)",
                            intent,
                            re.IGNORECASE,
                        )
                    )
                )
            ):
                gaps.append(IntentCoverageGap(
                    kind="ambiguous_symbol" if matches else "absent_symbol",
                    reference=reference,
                    candidates=tuple(path for path, _symbol in matches),
                ))

        terms = list(dict.fromkeys(
            token.lower()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", intent)
            if len(token) > 2
        ))
        relevance: list[IntentSymbolRelevance] = []
        for path, name, line, summary in self._fetchall(
            "SELECT file_path, name, line, summary FROM symbols"
        ):
            haystack = f"{name} {summary}".lower()
            matched = tuple(
                term for term in terms
                if re.search(rf"\b{re.escape(term)}\b", haystack)
            )
            if matched:
                relevance.append(IntentSymbolRelevance(
                    path=str(path),
                    line=int(line),
                    name=str(name),
                    score=len(matched),
                    matched_terms=matched,
                ))
        required_by_path: dict[str, list[str]] = {}
        for match in required:
            required_by_path.setdefault(match.path, []).append(match.symbol.name)
        relevant_by_path: dict[str, list[IntentSymbolRelevance]] = {}
        for item in relevance:
            relevant_by_path.setdefault(item.path, []).append(item)
        file_evidence: list[IntentFileEvidence] = []
        forms = self._intent_forms(terms)
        known_dirs = {str(parent) for path in file_roles for parent in Path(path).parents if str(parent) != "."}
        path_tokens = {path if path in file_roles else path.rstrip(".") for path in path_tokens}
        exact_files = {path for path in path_tokens if path in file_roles or (
            path not in creation_paths and (path in explicit_paths or "." in Path(path).name or str(Path(path).parent) in known_dirs))}
        gaps.extend(
            IntentCoverageGap(kind="absent_file", reference=path, candidates=())
            for path in sorted(exact_files - file_roles.keys())
        )
        for path, role, base_score, search_terms in file_rows:
            path = str(path)
            matched_terms = []
            score = float(base_score)
            for token, singular in forms:
                if re.search(rf"\b{re.escape(token)}\b", str(search_terms)):
                    matched_terms.append(token)
                    score += 45
                elif singular and re.search(rf"\b{re.escape(singular)}\b", str(search_terms)):
                    matched_terms.append(token)
                    score += 35
            exact_file = path in exact_files
            matched_symbols = list(dict.fromkeys([
                *(item.name for item in relevant_by_path.get(path, [])),
                *required_by_path.get(path, []),
            ]))
            if exact_file:
                matched_terms.insert(0, path)
                score += 10_000
            if required_by_path.get(path):
                score += 5_000
            if matched_terms or matched_symbols:
                file_evidence.append(IntentFileEvidence(
                    path=path,
                    relevance_score=score,
                    exact_file=exact_file,
                    matched_terms=tuple(dict.fromkeys(matched_terms)),
                    matched_symbols=tuple(matched_symbols),
                ))
        if not file_evidence and not required and not gaps:
            gaps.append(IntentCoverageGap(
                kind="no_relevant_seam", reference=intent.strip(), candidates=()))
        return IntentResolution(
            file_evidence=tuple(sorted(
                file_evidence,
                key=lambda item: (
                    file_roles[item.path] != "production", -item.relevance_score, item.path),
            )),
            required_symbols=tuple(required),
            symbol_relevance=tuple(sorted(
                relevance,
                key=lambda item: (item.path, -item.score, item.line, item.name),
            )),
            coverage_gaps=tuple(gaps),
        )

    def rank_intent(self, tokens: list[str], limit: int) -> list[str]:
        if not self.is_available or not tokens or limit <= 0:
            return []
        return [
            item.path
            for item in self.resolve_intent(" ".join(tokens)).file_evidence[:limit]
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
                ("function", r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)(?:<(?:[^<>()]|<[^<>()]*>)+>)?\s*\("),
                ("class", r"^\s*export\s+(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"),
                ("class", r"^\s*class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"),
                ("arrow", r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)(?:\s*:\s*.*?)?\s*=\s*(?:async\s*)?(?:<[^<>()]+>\s*)?\("),
                ("function", r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$]*\s*=>"),
            ]
        symbols: list[IndexedSymbol] = []
        indentations: list[int] = []
        seen: set[tuple[str, int]] = set()
        offset = 0
        lines = content.splitlines(keepends=True)
        string_spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
        if extension == ".py":
            source = iter(lines)
            string_token_types = {
                tokenize.STRING,
                getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING),
            }
            try:
                for token in tokenize.generate_tokens(lambda: next(source, "")):
                    if token.type in string_token_types:
                        string_spans.append((token.start, token.end))
            except (IndentationError, tokenize.TokenError):
                pass
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.rstrip("\r\n")
            for kind, pattern in patterns:
                match = re.search(pattern, line)
                if not match:
                    continue
                match_start = (line_number, match.start(1))
                if any(start <= match_start < end for start, end in string_spans):
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
            if extension != ".py":
                stop_line = len(lines)
                for next_index in range(index + 1, len(symbols)):
                    if indentations[next_index] <= indentations[index]:
                        stop_line = symbols[next_index].line - 1
                        break
                symbols[index] = replace(
                    symbol,
                    end_line=WorkflowIndex._javascript_end_line(
                        lines, symbol.line, stop_line
                    ),
                )
                continue
            start_line = symbol.line
            cursor = symbol.line - 1
            while cursor >= 1:
                candidate = lines[cursor - 1]
                stripped = candidate.strip()
                candidate_indentation = len(candidate) - len(candidate.lstrip())
                if not stripped or candidate_indentation < indentations[index]:
                    break
                if candidate_indentation == indentations[index]:
                    if stripped.startswith("@"):
                        start_line = cursor
                    elif not stripped.startswith((")", "]")):
                        break
                cursor -= 1
            header_end, inline_suite = WorkflowIndex._python_header(
                lines, symbol.line
            )
            end_line = header_end
            if not inline_suite:
                end_line = len(lines)
                triple_quote = ""
                for line_number in range(header_end + 1, len(lines) + 1):
                    candidate = lines[line_number - 1]
                    stripped = candidate.strip()
                    if triple_quote:
                        if candidate.count(triple_quote) % 2:
                            triple_quote = ""
                        continue
                    if (
                        stripped
                        and not stripped.startswith("#")
                        and len(candidate) - len(candidate.lstrip())
                        <= indentations[index]
                    ):
                        end_line = line_number - 1
                        break
                    marker = min(
                        (
                            (candidate.find(quote), quote)
                            for quote in ('"""', "'''")
                            if candidate.count(quote) % 2
                        ),
                        default=(-1, ""),
                    )[1]
                    if marker:
                        triple_quote = marker
            symbols[index] = replace(
                symbol, line=start_line, end_line=max(start_line, end_line)
            )
        return symbols

    @staticmethod
    def _python_header(lines: list[str], start_line: int) -> tuple[int, bool]:
        depth = 0
        for line_number in range(start_line, len(lines) + 1):
            line = lines[line_number - 1]
            if any(marker in line for marker in ('"', "'", "#")):
                break
            depth += line.count("(") - line.count(")")
            if depth <= 0 and ":" in line:
                return line_number, bool(line.rsplit(":", 1)[-1].strip())
        source = iter(lines[start_line - 1 :])
        depth = 0
        try:
            tokens = tokenize.generate_tokens(lambda: next(source, ""))
            for token in tokens:
                if token.type == tokenize.OP:
                    if token.string in "([{":
                        depth += 1
                    elif token.string in ")]}":
                        depth -= 1
                    elif token.string == ":" and depth == 0:
                        header_end = start_line + token.start[0] - 1
                        for following in tokens:
                            if following.type in {
                                tokenize.COMMENT,
                                tokenize.INDENT,
                                tokenize.NL,
                            }:
                                continue
                            return header_end, following.type != tokenize.NEWLINE
                        return header_end, False
        except (IndentationError, tokenize.TokenError):
            pass
        return start_line, False

    @staticmethod
    def _javascript_end_line(
        lines: list[str], start_line: int, stop_line: int
    ) -> int:
        indentation = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
        quote = ""
        escaped = False
        block_comment = False
        templates: list[int | None] = []
        parentheses = 0
        brackets = 0
        braces = 0
        for line_number in range(start_line, stop_line + 1):
            line = lines[line_number - 1]
            stripped = line.strip()
            if (
                line_number > start_line
                and not (
                    quote
                    or block_comment
                    or templates
                    or parentheses
                    or brackets
                    or braces
                )
                and stripped
                and len(line) - len(line.lstrip()) <= indentation
            ):
                return line_number - 1
            index = 0
            while index < len(line):
                char = line[index]
                pair = line[index : index + 2]
                if templates and templates[-1] is None:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == "`":
                        templates.pop()
                    elif pair == "${":
                        templates[-1] = braces
                        braces += 1
                        index += 2
                        continue
                elif block_comment:
                    if pair == "*/":
                        block_comment = False
                        index += 2
                        continue
                elif quote:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        quote = ""
                elif pair == "//":
                    break
                elif pair == "/*":
                    block_comment = True
                    index += 2
                    continue
                elif char == "/":
                    prefix = line[:index].rstrip()
                    previous = prefix[-1:] if prefix else ""
                    word = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)$", prefix)
                    if (
                        not prefix
                        or previous in "([{:;,=!?&|+-*%^~<>"
                        or (word and word.group(1) in {"case", "return", "throw", "yield"})
                    ):
                        regex_end = None
                        cursor = index + 1
                        in_character_class = False
                        while cursor < len(line):
                            if line[cursor] == "\\":
                                cursor += 2
                                continue
                            if line[cursor] == "[":
                                in_character_class = True
                            elif line[cursor] == "]":
                                in_character_class = False
                            elif line[cursor] == "/" and not in_character_class:
                                regex_end = cursor + 1
                                break
                            cursor += 1
                        if regex_end is not None:
                            index = regex_end
                            continue
                elif char in {'"', "'"}:
                    quote = char
                elif char == "`":
                    templates.append(None)
                elif char == "(":
                    parentheses += 1
                elif char == ")" and parentheses:
                    parentheses -= 1
                elif char == "[":
                    brackets += 1
                elif char == "]" and brackets:
                    brackets -= 1
                elif char == "{":
                    braces += 1
                elif char == "}" and braces:
                    braces -= 1
                    if (
                        templates
                        and templates[-1] is not None
                        and braces == templates[-1]
                    ):
                        templates[-1] = None
                index += 1
        return stop_line

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
