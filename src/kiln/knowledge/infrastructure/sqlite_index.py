"""
SQLite FTS5 persistence for the disposable knowledge index.

The only module that knows the database exists. It also owns the transactions: each port
method commits its own work or leaves the index untouched, so `knowledge_service` never sees a
connection, a commit or a rollback.

Disposable by design -- deleting `.kiln/knowledge.db` is a supported operation, and `sync`
reconstructs it from the committed catalog and the original documents.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from ..domain.models import ExtractedDocument, KnowledgeError, SearchResult, Source

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL, type TEXT NOT NULL,
    tags TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT,
    indexed_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path TEXT NOT NULL, title TEXT NOT NULL, media_type TEXT NOT NULL,
    content TEXT NOT NULL, fingerprint TEXT NOT NULL, indexed_at TEXT NOT NULL,
    UNIQUE(source_id, path)
);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    heading TEXT NOT NULL, page INTEGER, content TEXT NOT NULL, ordinal INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, content);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
    updated INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
    removed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0
);
"""

#: Bounded sync history: enough to see a pattern, not an unbounded log in a disposable file.
SYNC_HISTORY_LIMIT = 50


def now() -> str:
    return datetime.now(UTC).isoformat()


def document_id(source_id: str, path: str) -> str:
    return hashlib.sha256(f"{source_id}\0{path}".encode()).hexdigest()[:24]


class SqliteKnowledgeIndex:
    """
    `KnowledgeIndex` over SQLite.

    A connection lives for one sync run (`start_run` opens it, `finish_run` closes it) and for
    the duration of a single read. Deliberately not held open across calls: the database is
    documented as safe to delete at any moment, and on Windows an open handle makes deleting it
    fail -- so a long-lived connection would quietly break the one recovery this design
    promises.
    """

    def __init__(self, path: Path):
        self.path = path
        self._connection: sqlite3.Connection | None = None

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        """The sync-run connection, opened on first use within a run."""
        if self._connection is None:
            self._connection = self._open()
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def ready(self) -> bool:
        """Whether an index exists and is readable, without creating one as a side effect."""
        if not self.path.is_file():
            return False
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("SELECT 1 FROM sources LIMIT 1")
            return True
        except sqlite3.Error:
            return False

    # -- sync bookkeeping --------------------------------------------------------------------

    def start_run(self) -> int:
        cursor = self.connection.execute("INSERT INTO sync_runs(started_at) VALUES (?)", (now(),))
        self.connection.commit()
        if cursor.lastrowid is None:  # pragma: no cover - sqlite INSERT contract
            raise KnowledgeError("could not record knowledge synchronization")
        return cursor.lastrowid

    def finish_run(self, run_id: int, counts: dict[str, int]) -> None:
        self.connection.execute(
            "UPDATE sync_runs SET finished_at=?, updated=?, skipped=?, removed=?, failed=?"
            " WHERE id=?",
            (
                now(),
                counts["updated"],
                counts["skipped"],
                counts["removed"],
                counts["failed"],
                run_id,
            ),
        )
        self.connection.execute(
            "DELETE FROM sync_runs WHERE id NOT IN"
            " (SELECT id FROM sync_runs ORDER BY id DESC LIMIT ?)",
            (SYNC_HISTORY_LIMIT,),
        )
        self.connection.commit()
        # The run is over; release the file so it can be deleted while the service lives on.
        self.close()

    # -- source state ------------------------------------------------------------------------

    def mark_indexing(self, source: Source) -> None:
        self._upsert_source(source, status="indexing")
        self.connection.commit()

    def mark_indexed(self, source: Source, fingerprint: str) -> None:
        self._upsert_source(source, status="indexed", fingerprint=fingerprint)
        self.connection.commit()

    def mark_failed(self, source: Source, error: str) -> int:
        """
        Discard whatever the interrupted source wrote and record why. Returns documents dropped.

        The rollback is the point: a source that failed half way through must not leave
        partially indexed documents behind to be served as search results.
        """
        self.connection.rollback()
        self._upsert_source(source, status="failed", error=error)
        removed = self.drop_documents_except(source.id, set())
        self.connection.commit()
        return removed

    def _upsert_source(
        self,
        source: Source,
        *,
        status: str,
        fingerprint: str | None = None,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO sources(id,title,path,type,tags,status,fingerprint,indexed_at,error)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title,path=excluded.path,
               type=excluded.type,tags=excluded.tags,status=excluded.status,
               fingerprint=excluded.fingerprint,indexed_at=excluded.indexed_at,
               error=excluded.error""",
            (
                source.id,
                source.title,
                source.path,
                source.type,
                ",".join(source.tags),
                status,
                fingerprint,
                now(),
                error,
            ),
        )

    # -- documents ---------------------------------------------------------------------------

    def known_fingerprint(self, source_id: str, path: str) -> str | None:
        row = self.connection.execute(
            "SELECT fingerprint FROM documents WHERE source_id=? AND path=?", (source_id, path)
        ).fetchone()
        return str(row[0]) if row else None

    def replace_document(self, source: Source, document: ExtractedDocument) -> None:
        identifier = document_id(source.id, document.relative_path)
        self._delete_chunks(identifier)
        self.connection.execute(
            """INSERT INTO documents
               (id,source_id,path,title,media_type,content,fingerprint,indexed_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,
               media_type=excluded.media_type,content=excluded.content,
               fingerprint=excluded.fingerprint,indexed_at=excluded.indexed_at""",
            (
                identifier,
                source.id,
                document.relative_path,
                document.title,
                document.media_type,
                document.content,
                document.fingerprint,
                now(),
            ),
        )
        self._insert_chunks(identifier, document)

    def _insert_chunks(self, identifier: str, document: ExtractedDocument) -> None:
        for ordinal, section in enumerate(document.sections):
            chunk_id = f"{identifier}:{ordinal}"
            self.connection.execute(
                "INSERT INTO chunks(id,document_id,heading,page,content,ordinal)"
                " VALUES(?,?,?,?,?,?)",
                (chunk_id, identifier, section.heading, section.page, section.text, ordinal),
            )
            # The heading rides along in the indexed text so a search for it finds the body.
            self.connection.execute(
                "INSERT INTO chunks_fts(chunk_id,content) VALUES(?,?)",
                (chunk_id, f"{section.heading}\n{section.text}"),
            )

    def _delete_chunks(self, identifier: str) -> None:
        # fts5 contentless rows are not cascaded by the foreign key, so they go explicitly.
        old = self.connection.execute(
            "SELECT id FROM chunks WHERE document_id=?", (identifier,)
        ).fetchall()
        self.connection.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", old)
        self.connection.execute("DELETE FROM chunks WHERE document_id=?", (identifier,))

    def drop_documents_except(self, source_id: str, retained: set[str]) -> int:
        rows = self.connection.execute(
            "SELECT id,path FROM documents WHERE source_id=?", (source_id,)
        ).fetchall()
        doomed = [row for row in rows if row["path"] not in retained]
        for row in doomed:
            self._delete_chunks(row["id"])
            self.connection.execute("DELETE FROM documents WHERE id=?", (row["id"],))
        return len(doomed)

    def drop_sources_except(self, retained_ids: set[str]) -> int:
        rows = self.connection.execute("SELECT id FROM sources").fetchall()
        removed = 0
        for row in rows:
            if row["id"] not in retained_ids:
                self.drop_documents_except(row["id"], set())
                self.connection.execute("DELETE FROM sources WHERE id=?", (row["id"],))
                removed += 1
        return removed

    # -- retrieval -----------------------------------------------------------------------------

    def search(self, query: str, limit: int) -> list[SearchResult]:
        with closing(self._open()) as connection:
            return self._search(connection, query, limit)

    def _search(self, connection: sqlite3.Connection, query: str, limit: int) -> list[SearchResult]:
        rows = connection.execute(
            """SELECT c.id AS chunk_id,d.id AS document_id,s.id AS source_id,
                      s.title AS source_title,d.path,c.heading,c.page,c.content AS excerpt,
                      d.indexed_at,s.status,bm25(chunks_fts) AS rank
               FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.chunk_id
               JOIN documents d ON d.id=c.document_id JOIN sources s ON s.id=d.source_id
               WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (match_expression(query), limit),
        ).fetchall()
        return [_search_result(row) for row in rows]

    def document(self, identifier: str) -> dict:
        with closing(self._open()) as connection:
            return self._document(connection, identifier)

    def _document(self, connection: sqlite3.Connection, identifier: str) -> dict:
        row = connection.execute(
            """SELECT d.id,d.source_id,s.title AS source_title,d.path,d.title,d.media_type,
                      d.content,d.fingerprint,d.indexed_at,s.status
               FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?""",
            (identifier,),
        ).fetchone()
        if not row:
            raise KnowledgeError(f"knowledge document not found: {identifier}")
        return dict(row)


def match_expression(query: str) -> str:
    """
    A user's words as an FTS5 MATCH expression.

    Every token is quoted and AND-ed rather than passed through: FTS5 reads bare `-`, `*`, `OR`
    and `NEAR` as operators, so an unquoted query is both a syntax-error risk and a way for
    punctuation in a search phrase to change what was asked for.
    """
    tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)
    if not tokens:
        raise KnowledgeError("search query must contain a word")
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _search_result(row: sqlite3.Row) -> SearchResult:
    return SearchResult(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        source_id=row["source_id"],
        source_title=row["source_title"],
        path=row["path"],
        heading=row["heading"],
        page=row["page"],
        excerpt=row["excerpt"],
        indexed_at=row["indexed_at"],
        freshness="indexed" if row["status"] == "indexed" else row["status"],
        rank=row["rank"],
    )
