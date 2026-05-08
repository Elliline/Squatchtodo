"""Data-access layer for SquatchTodo.

The repository is a flat module of functions, each taking a
``sqlite3.Connection`` as the first argument. This keeps the API and MCP layers
free to manage connection lifecycle however they want (per-request in FastAPI,
per-tool-call in the MCP server) without forcing them through an instance.

Conventions
-----------
* No hard deletes. ``archive_*`` setters set ``status='archived'``.
* ``created_by`` / ``author`` are explicit arguments — the caller is
  responsible for resolving the actor identity. The repository never reads it
  from request context.
* Tags are passed and returned as ``list[str]``; serialisation to/from JSON
  text happens here.
* All write functions return the freshly fetched row so callers can rely on
  server-generated values (id, timestamps) without a follow-up query.
* ``UNSET`` is a sentinel distinguishing "caller did not pass this" from
  "caller explicitly passed None" — used by ``update_*`` so we can leave a
  column untouched vs. explicitly clearing it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from .db import transaction
from .models import (
    PROJECT_STATUSES,
    TODO_PRIORITIES,
    TODO_STATUSES,
    Note,
    Project,
    ProjectStatus,
    SearchResults,
    Todo,
    TodoPriority,
    TodoStatus,
)


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNSET"


UNSET: Final[Any] = _Unset()


# -- row mapping ------------------------------------------------------------


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row["created_by"],
    )


def _row_to_todo(row: sqlite3.Row) -> Todo:
    return Todo(
        id=row["id"],
        project_id=row["project_id"],
        parent_todo_id=row["parent_todo_id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        priority=row["priority"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        created_by=row["created_by"],
    )


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        todo_id=row["todo_id"],
        content=row["content"],
        author=row["author"],
        created_at=row["created_at"],
    )


# -- validation -------------------------------------------------------------
# The DB enforces enum constraints, but failing fast with a clearer error
# message before hitting SQLite makes API/MCP errors more useful.


class ValidationError(ValueError):
    """Raised when an input fails repository-level validation."""


def _validate_project_status(status: str) -> None:
    if status not in PROJECT_STATUSES:
        raise ValidationError(
            f"invalid project status {status!r}; expected one of {PROJECT_STATUSES}"
        )


def _validate_todo_status(status: str) -> None:
    if status not in TODO_STATUSES:
        raise ValidationError(
            f"invalid todo status {status!r}; expected one of {TODO_STATUSES}"
        )


def _validate_todo_priority(priority: str) -> None:
    if priority not in TODO_PRIORITIES:
        raise ValidationError(
            f"invalid todo priority {priority!r}; expected one of {TODO_PRIORITIES}"
        )


def _validate_tags(tags: list[str]) -> None:
    if not isinstance(tags, list):
        raise ValidationError(f"tags must be a list of strings, got {type(tags).__name__}")
    for tag in tags:
        if not isinstance(tag, str) or not tag:
            raise ValidationError(f"tags must be non-empty strings; got {tag!r}")


def _validate_identity(identity: str, field_name: str = "created_by") -> None:
    if not isinstance(identity, str) or not identity.strip():
        raise ValidationError(f"{field_name} must be a non-empty string")


# -- projects ---------------------------------------------------------------


def list_projects(
    conn: sqlite3.Connection,
    *,
    status: ProjectStatus | None = None,
    tag: str | None = None,
) -> list[Project]:
    """List projects, newest activity first.

    ``tag`` filters projects whose JSON tags array contains the given tag.
    """
    sql = ["SELECT * FROM projects"]
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        _validate_project_status(status)
        where.append("status = ?")
        params.append(status)
    if tag is not None:
        # json_each is the canonical way to filter on a JSON-array column.
        where.append(
            "EXISTS (SELECT 1 FROM json_each(projects.tags) WHERE json_each.value = ?)"
        )
        params.append(tag)
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY updated_at DESC")
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [_row_to_project(r) for r in rows]


def get_project(conn: sqlite3.Connection, project_id: int) -> Project | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return _row_to_project(row) if row else None


def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    created_by: str,
    description: str | None = None,
    tags: list[str] | None = None,
) -> Project:
    if not name or not name.strip():
        raise ValidationError("project name must be a non-empty string")
    _validate_identity(created_by)
    tags = tags or []
    _validate_tags(tags)

    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO projects(name, description, tags, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (name.strip(), description, json.dumps(tags), created_by),
        )
        project_id = cursor.lastrowid
    project = get_project(conn, project_id)
    assert project is not None  # we just inserted it
    return project


def update_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: str | _Unset = UNSET,
    description: str | None | _Unset = UNSET,
    status: ProjectStatus | _Unset = UNSET,
    tags: list[str] | _Unset = UNSET,
) -> Project | None:
    sets: list[str] = []
    params: list[Any] = []

    if not isinstance(name, _Unset):
        if not name or not name.strip():
            raise ValidationError("project name must be a non-empty string")
        sets.append("name = ?")
        params.append(name.strip())
    if not isinstance(description, _Unset):
        sets.append("description = ?")
        params.append(description)
    if not isinstance(status, _Unset):
        _validate_project_status(status)
        sets.append("status = ?")
        params.append(status)
    if not isinstance(tags, _Unset):
        _validate_tags(tags)
        sets.append("tags = ?")
        params.append(json.dumps(tags))

    if not sets:
        return get_project(conn, project_id)

    params.append(project_id)
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params
        )
        if cursor.rowcount == 0:
            return None
    return get_project(conn, project_id)


# -- todos ------------------------------------------------------------------


def list_todos(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    parent_todo_id: int | None | _Unset = UNSET,
    status: TodoStatus | None = None,
) -> list[Todo]:
    """List todos in a project.

    ``parent_todo_id`` distinguishes three modes:
        * ``UNSET`` — return *all* todos in the project (full flat list)
        * ``None`` — only top-level todos (parent_todo_id IS NULL)
        * ``int`` — only direct children of that todo
    """
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    if isinstance(parent_todo_id, _Unset):
        pass
    elif parent_todo_id is None:
        where.append("parent_todo_id IS NULL")
    else:
        where.append("parent_todo_id = ?")
        params.append(parent_todo_id)
    if status is not None:
        _validate_todo_status(status)
        where.append("status = ?")
        params.append(status)

    rows = conn.execute(
        f"SELECT * FROM todos WHERE {' AND '.join(where)} ORDER BY id",
        params,
    ).fetchall()
    return [_row_to_todo(r) for r in rows]


def get_todo(conn: sqlite3.Connection, todo_id: int) -> Todo | None:
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return _row_to_todo(row) if row else None


def create_todo(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    title: str,
    created_by: str,
    parent_todo_id: int | None = None,
    description: str | None = None,
    priority: TodoPriority = "medium",
) -> Todo:
    if not title or not title.strip():
        raise ValidationError("todo title must be a non-empty string")
    _validate_identity(created_by)
    _validate_todo_priority(priority)

    # Parent-project consistency: a subtodo must live in the same project as
    # its parent. Cheap to check and prevents data corruption that would be
    # awkward to recover from.
    if parent_todo_id is not None:
        parent_row = conn.execute(
            "SELECT project_id FROM todos WHERE id = ?", (parent_todo_id,)
        ).fetchone()
        if parent_row is None:
            raise ValidationError(f"parent_todo_id {parent_todo_id} does not exist")
        if parent_row["project_id"] != project_id:
            raise ValidationError(
                f"parent_todo_id {parent_todo_id} belongs to a different project"
            )

    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO todos(project_id, parent_todo_id, title, description, priority, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                parent_todo_id,
                title.strip(),
                description,
                priority,
                created_by,
            ),
        )
        todo_id = cursor.lastrowid
    todo = get_todo(conn, todo_id)
    assert todo is not None
    return todo


def update_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    *,
    title: str | _Unset = UNSET,
    description: str | None | _Unset = UNSET,
    status: TodoStatus | _Unset = UNSET,
    priority: TodoPriority | _Unset = UNSET,
) -> Todo | None:
    sets: list[str] = []
    params: list[Any] = []

    if not isinstance(title, _Unset):
        if not title or not title.strip():
            raise ValidationError("todo title must be a non-empty string")
        sets.append("title = ?")
        params.append(title.strip())
    if not isinstance(description, _Unset):
        sets.append("description = ?")
        params.append(description)
    if not isinstance(status, _Unset):
        _validate_todo_status(status)
        sets.append("status = ?")
        params.append(status)
        # Mirror status transitions onto completed_at so consumers don't have to
        # special-case the bookkeeping.
        if status == "done":
            sets.append("completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
        elif status in ("open", "in_progress", "blocked"):
            sets.append("completed_at = NULL")
    if not isinstance(priority, _Unset):
        _validate_todo_priority(priority)
        sets.append("priority = ?")
        params.append(priority)

    if not sets:
        return get_todo(conn, todo_id)

    params.append(todo_id)
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE todos SET {', '.join(sets)} WHERE id = ?", params
        )
        if cursor.rowcount == 0:
            return None
    return get_todo(conn, todo_id)


def complete_todo(conn: sqlite3.Connection, todo_id: int) -> Todo | None:
    """Convenience: mark a todo done and stamp ``completed_at``."""
    return update_todo(conn, todo_id, status="done")


# -- notes ------------------------------------------------------------------


def add_note(
    conn: sqlite3.Connection,
    *,
    todo_id: int,
    content: str,
    author: str,
) -> Note:
    if not content or not content.strip():
        raise ValidationError("note content must be a non-empty string")
    _validate_identity(author, field_name="author")

    # Verify the todo exists rather than letting the FK error bubble up — the
    # caller gets a clearer error and we avoid the FK-violation rollback path.
    if conn.execute("SELECT 1 FROM todos WHERE id = ?", (todo_id,)).fetchone() is None:
        raise ValidationError(f"todo_id {todo_id} does not exist")

    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO notes(todo_id, content, author) VALUES (?, ?, ?)",
            (todo_id, content, author),
        )
        note_id = cursor.lastrowid
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row is not None
    return _row_to_note(row)


def list_notes(conn: sqlite3.Connection, todo_id: int) -> list[Note]:
    rows = conn.execute(
        "SELECT * FROM notes WHERE todo_id = ? ORDER BY id ASC",
        (todo_id,),
    ).fetchall()
    return [_row_to_note(r) for r in rows]


# -- search -----------------------------------------------------------------


_SEARCH_SCOPES: Final = ("all", "projects", "todos", "notes")


def _fts_query(query: str) -> str:
    """Convert a plain-text query to a permissive FTS5 MATCH expression.

    FTS5's default syntax treats double quotes, NEAR, AND/OR/NOT, etc. as
    operators. For a user-typed search box we want substring-friendly
    behaviour, so we tokenise on whitespace, strip FTS5 metacharacters, and
    join with implicit AND.
    """
    tokens: list[str] = []
    for raw in query.split():
        cleaned = "".join(c for c in raw if c.isalnum() or c in "_-")
        if cleaned:
            tokens.append(f'"{cleaned}"*')
    if not tokens:
        return ""
    return " ".join(tokens)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    scope: str = "all",
    limit: int = 50,
) -> SearchResults:
    if scope not in _SEARCH_SCOPES:
        raise ValidationError(
            f"invalid scope {scope!r}; expected one of {_SEARCH_SCOPES}"
        )
    fts = _fts_query(query)
    results = SearchResults()
    if not fts:
        return results

    if scope in ("all", "projects"):
        rows = conn.execute(
            """
            SELECT projects.*
              FROM projects_fts
              JOIN projects ON projects.id = projects_fts.rowid
             WHERE projects_fts MATCH ?
             ORDER BY rank
             LIMIT ?
            """,
            (fts, limit),
        ).fetchall()
        results.projects = [_row_to_project(r) for r in rows]

    if scope in ("all", "todos"):
        rows = conn.execute(
            """
            SELECT todos.*
              FROM todos_fts
              JOIN todos ON todos.id = todos_fts.rowid
             WHERE todos_fts MATCH ?
             ORDER BY rank
             LIMIT ?
            """,
            (fts, limit),
        ).fetchall()
        results.todos = [_row_to_todo(r) for r in rows]

    if scope in ("all", "notes"):
        rows = conn.execute(
            """
            SELECT notes.*
              FROM notes_fts
              JOIN notes ON notes.id = notes_fts.rowid
             WHERE notes_fts MATCH ?
             ORDER BY rank
             LIMIT ?
            """,
            (fts, limit),
        ).fetchall()
        results.notes = [_row_to_note(r) for r in rows]

    return results
