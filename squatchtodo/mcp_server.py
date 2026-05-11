"""MCP server layer.

Wraps the repository as MCP tools and mounts a FastMCP HTTP transport under
``/mcp`` of the FastAPI app. Tools take a ``sqlite3.Connection`` from a tiny
per-call helper that uses the captured ``Config`` — no global module state,
which keeps the module testable.

Identity propagation
--------------------
``X-Squatchtodo-Identity`` is read by a middleware installed on the parent
FastAPI app and stashed in a contextvar. MCP tool implementations read that
var, falling back to the configured default. The middleware is added to the
parent app rather than the mounted Starlette app so the same code path serves
both REST and MCP requests, and so test setups can inject identity without
caring which transport is in play.

Lifespan
--------
``streamable_http_app()`` returns a Starlette app whose lifespan starts a
session manager. FastAPI does not auto-drive the lifespan of mounted ASGI
apps, so the FastAPI lifespan in ``api.py`` enters
``mcp_server.session_manager.run()`` itself.

Mount path
----------
The MCP server is built with ``streamable_http_path="/"`` and mounted at
``/mcp``. Starlette's ``Mount`` strips the mount prefix before dispatch, so
the inner route's path is what's left after ``/mcp``. With the default
``streamable_http_path="/mcp"`` you'd have to hit ``/mcp/mcp`` to reach the
endpoint; with ``"/"`` the canonical ``/mcp/`` URL works.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from . import db, repository
from .config import Config
from .models import Note, Project, Todo

log = logging.getLogger("squatchtodo.mcp")

# Contextvar set by middleware on the parent FastAPI app. Defaults to "" so a
# missing header falls through to the configured identity default.
_identity_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "squatchtodo_mcp_identity", default=""
)
IDENTITY_HEADER = "X-Squatchtodo-Identity"


# -- helpers ----------------------------------------------------------------


def _to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_to_dict(item) for item in obj]
    return obj


def _build_todo_tree(todos: list[Todo]) -> list[dict[str, Any]]:
    nodes: dict[int, dict[str, Any]] = {}
    for t in todos:
        d = dataclasses.asdict(t)
        d["subtodos"] = []
        nodes[t.id] = d
    roots: list[dict[str, Any]] = []
    for t in todos:
        node = nodes[t.id]
        if t.parent_todo_id is None or t.parent_todo_id not in nodes:
            roots.append(node)
        else:
            nodes[t.parent_todo_id]["subtodos"].append(node)
    return roots


# -- factory ----------------------------------------------------------------


def build_mcp_server(config: Config) -> Any:
    """Construct a FastMCP server with all SquatchTodo tools registered.

    The ``mcp`` package is imported lazily so test environments without it
    (or older Python versions) can import this module without exploding.
    """
    from mcp.server.fastmcp import FastMCP  # local import; mcp requires 3.10+

    # streamable_http_path="/" because we mount the resulting Starlette app at
    # "/mcp" of the parent FastAPI. Starlette's Mount strips the "/mcp" prefix
    # before dispatching, so the inner Route's path is what's left — set it to
    # "/" so a request to "/mcp/" lands on the route. With the default of
    # "/mcp", clients would have to hit "/mcp/mcp" to reach the endpoint.
    server = FastMCP(
        "squatchtodo",
        instructions=(
            "Shared todo and project tracking system. Read with list_projects "
            "/ get_project / list_todos / get_todo / list_notes / search; write "
            "with create_project / update_project / create_todo / update_todo / "
            "complete_todo / add_note. Items are never hard-deleted — set status "
            "to 'archived' to retire them."
        ),
        streamable_http_path="/",
    )

    @contextmanager
    def open_db() -> Iterator[sqlite3.Connection]:
        conn = db.connect(config.database.path)
        try:
            yield conn
        finally:
            conn.close()

    def actor() -> str:
        val = _identity_var.get()
        return val.strip() if val and val.strip() else config.identity.default

    # -- project tools ------------------------------------------------------

    @server.tool(description="List projects, optionally filtered by status and tag.")
    def list_projects(
        status: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        with open_db() as conn:
            rows = repository.list_projects(conn, status=status, tag=tag)
            return [dataclasses.asdict(p) for p in rows]

    @server.tool(
        description=(
            "Fetch a single project by id. Set include_todos=true to include the "
            "full todo tree under a 'todos' key."
        )
    )
    def get_project(
        project_id: int,
        include_todos: bool = False,
    ) -> Optional[dict[str, Any]]:
        with open_db() as conn:
            project = repository.get_project(conn, project_id)
            if project is None:
                return None
            result = dataclasses.asdict(project)
            if include_todos:
                todos = repository.list_todos(conn, project_id=project_id)
                result["todos"] = _build_todo_tree(todos)
            return result

    @server.tool(description="Create a new project.")
    def create_project(
        name: str,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        with open_db() as conn:
            project = repository.create_project(
                conn,
                name=name,
                description=description,
                tags=tags or [],
                created_by=actor(),
            )
            return dataclasses.asdict(project)

    @server.tool(
        description=(
            "Update a project. Pass only the fields you want to change. "
            "Set status='archived' to retire a project — there are no hard deletes."
        )
    )
    def update_project(
        project_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        # MCP can't represent "field not sent" vs "field set to null" cleanly,
        # so we treat None as "leave unchanged" here. Callers who want to clear
        # a description should pass an empty string.
        kwargs: dict[str, Any] = {}
        if name is not None:
            kwargs["name"] = name
        if description is not None:
            kwargs["description"] = description
        if status is not None:
            kwargs["status"] = status
        if tags is not None:
            kwargs["tags"] = tags

        with open_db() as conn:
            updated = repository.update_project(conn, project_id, **kwargs)
            return dataclasses.asdict(updated) if updated else None

    # -- todo tools ---------------------------------------------------------

    @server.tool(
        description=(
            "List todos in a project. parent_todo_id=null returns top-level "
            "todos only; pass an integer to list children of that todo. Omit "
            "parent_todo_id to get a flat list of all todos in the project."
        )
    )
    def list_todos(
        project_id: int,
        parent_todo_id: Optional[int] = None,
        only_top_level: bool = False,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        with open_db() as conn:
            if parent_todo_id is not None:
                rows = repository.list_todos(
                    conn,
                    project_id=project_id,
                    parent_todo_id=parent_todo_id,
                    status=status,
                )
            elif only_top_level:
                rows = repository.list_todos(
                    conn,
                    project_id=project_id,
                    parent_todo_id=None,
                    status=status,
                )
            else:
                rows = repository.list_todos(
                    conn, project_id=project_id, status=status
                )
            return [dataclasses.asdict(t) for t in rows]

    @server.tool(
        description=(
            "Fetch a single todo. include_subtodos=true adds a 'subtodos' list "
            "of direct children; include_notes=true adds the full notes log."
        )
    )
    def get_todo(
        todo_id: int,
        include_subtodos: bool = False,
        include_notes: bool = False,
    ) -> Optional[dict[str, Any]]:
        with open_db() as conn:
            todo = repository.get_todo(conn, todo_id)
            if todo is None:
                return None
            result = dataclasses.asdict(todo)
            if include_subtodos:
                children = repository.list_todos(
                    conn, project_id=todo.project_id, parent_todo_id=todo.id
                )
                result["subtodos"] = _build_todo_tree(children)
            if include_notes:
                notes = repository.list_notes(conn, todo_id)
                result["notes"] = [dataclasses.asdict(n) for n in notes]
            return result

    @server.tool(description="Create a new todo, optionally as a subtodo of another.")
    def create_todo(
        project_id: int,
        title: str,
        parent_todo_id: Optional[int] = None,
        description: Optional[str] = None,
        priority: str = "medium",
    ) -> dict[str, Any]:
        with open_db() as conn:
            todo = repository.create_todo(
                conn,
                project_id=project_id,
                title=title,
                parent_todo_id=parent_todo_id,
                description=description,
                priority=priority,  # type: ignore[arg-type]
                created_by=actor(),
            )
            return dataclasses.asdict(todo)

    @server.tool(
        description=(
            "Update a todo. Pass only the fields to change. Setting status to "
            "'done' stamps completed_at; setting status back to open/in_progress/"
            "blocked clears it."
        )
    )
    def update_todo(
        todo_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        if status is not None:
            kwargs["status"] = status
        if priority is not None:
            kwargs["priority"] = priority

        with open_db() as conn:
            updated = repository.update_todo(conn, todo_id, **kwargs)
            return dataclasses.asdict(updated) if updated else None

    @server.tool(
        description="Mark a todo as done. Equivalent to update_todo(status='done')."
    )
    def complete_todo(todo_id: int) -> Optional[dict[str, Any]]:
        with open_db() as conn:
            updated = repository.complete_todo(conn, todo_id)
            return dataclasses.asdict(updated) if updated else None

    # -- note tools ---------------------------------------------------------

    @server.tool(description="Append a note to a todo's running log.")
    def add_note(todo_id: int, content: str) -> dict[str, Any]:
        with open_db() as conn:
            note = repository.add_note(
                conn, todo_id=todo_id, content=content, author=actor()
            )
            return dataclasses.asdict(note)

    @server.tool(description="List all notes on a todo, oldest first.")
    def list_notes(todo_id: int) -> list[dict[str, Any]]:
        with open_db() as conn:
            return [dataclasses.asdict(n) for n in repository.list_notes(conn, todo_id)]

    # -- search -------------------------------------------------------------

    @server.tool(
        description=(
            "Full-text search across projects, todos, and notes. scope can be "
            "'all', 'projects', 'todos', or 'notes'. Multi-word queries use "
            "implicit AND with prefix matching."
        )
    )
    def search(
        query: str,
        scope: str = "all",
        limit: int = 50,
    ) -> dict[str, list[dict[str, Any]]]:
        with open_db() as conn:
            results = repository.search(conn, query, scope=scope, limit=limit)
            return {
                "projects": [dataclasses.asdict(p) for p in results.projects],
                "todos": [dataclasses.asdict(t) for t in results.todos],
                "notes": [dataclasses.asdict(n) for n in results.notes],
            }

    return server


# -- FastAPI integration ----------------------------------------------------


def install_identity_middleware(app: Any) -> None:
    """Attach the identity-extraction middleware to a FastAPI/Starlette app.

    Runs for *all* requests (REST and MCP) so any code path that wants the
    actor identity can read it from ``_identity_var``.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    class _IdentityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            token = _identity_var.set(request.headers.get(IDENTITY_HEADER) or "")
            try:
                return await call_next(request)
            finally:
                _identity_var.reset(token)

    app.add_middleware(_IdentityMiddleware)


def mount_on_fastapi(app: Any, config: Config) -> Optional[Any]:
    """Build the MCP server and mount it under ``/mcp`` if enabled.

    Returns the server instance (or ``None`` if MCP is disabled in config) so
    the caller can drive its lifespan from the FastAPI lifespan.
    """
    if not config.mcp.enabled:
        log.info("MCP disabled in config; not mounting /mcp")
        return None
    server = build_mcp_server(config)
    install_identity_middleware(app)
    app.mount("/mcp", server.streamable_http_app())
    log.info("mounted MCP server at /mcp")
    return server
