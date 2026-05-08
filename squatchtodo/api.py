"""FastAPI REST API for SquatchTodo.

Routes are organised under ``/api/`` so the top-level paths remain free for the
HTML web UI (added in step 5). The MCP server (step 4) reuses the repository
layer directly and does *not* go through these endpoints — that avoids a
self-HTTP hop and lets the MCP transport be configured independently.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Iterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import db, repository
from .config import Config, load_config
from .identity import resolve_identity
from .models import (
    Note,
    Project,
    ProjectStatus,
    Todo,
    TodoPriority,
    TodoStatus,
)
from .repository import UNSET, ValidationError

log = logging.getLogger("squatchtodo.api")


# -- Pydantic schemas -------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[ProjectStatus] = None
    tags: Optional[list[str]] = None


class TodoCreate(BaseModel):
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    parent_todo_id: Optional[int] = None
    priority: TodoPriority = "medium"


class TodoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TodoStatus] = None
    priority: Optional[TodoPriority] = None


class NoteCreate(BaseModel):
    content: str = Field(..., min_length=1)


# Response shapes mirror the dataclasses but live as Pydantic so FastAPI can
# generate OpenAPI cleanly. ``model_validate`` accepts dataclasses thanks to
# ``from_attributes=True``.

class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]
    status: ProjectStatus
    tags: list[str]
    created_at: str
    updated_at: str
    created_by: str


class TodoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    parent_todo_id: Optional[int]
    title: str
    description: Optional[str]
    status: TodoStatus
    priority: TodoPriority
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    created_by: str


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    todo_id: int
    content: str
    author: str
    created_at: str


class TodoTreeOut(TodoOut):
    """Todo plus optionally-included children and notes."""

    subtodos: Optional[list["TodoTreeOut"]] = None
    notes: Optional[list[NoteOut]] = None


class ProjectDetailOut(ProjectOut):
    todos: Optional[list[TodoTreeOut]] = None


class SearchOut(BaseModel):
    projects: list[ProjectOut]
    todos: list[TodoOut]
    notes: list[NoteOut]


# -- helpers ----------------------------------------------------------------


def _build_todo_tree(todos: list[Todo]) -> list[TodoTreeOut]:
    """Turn a flat list of todos (in a single project) into a forest by parent."""
    nodes: dict[int, TodoTreeOut] = {
        t.id: TodoTreeOut.model_validate(t).model_copy(update={"subtodos": []})
        for t in todos
    }
    roots: list[TodoTreeOut] = []
    for t in todos:
        node = nodes[t.id]
        if t.parent_todo_id is None or t.parent_todo_id not in nodes:
            roots.append(node)
        else:
            parent = nodes[t.parent_todo_id]
            assert parent.subtodos is not None
            parent.subtodos.append(node)
    return roots


def _project_or_404(conn: sqlite3.Connection, project_id: int) -> Project:
    project = repository.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    return project


def _todo_or_404(conn: sqlite3.Connection, todo_id: int) -> Todo:
    todo = repository.get_todo(conn, todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail=f"todo {todo_id} not found")
    return todo


# -- dependencies -----------------------------------------------------------


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request SQLite connection. WAL mode means readers don't block writers."""
    config: Config = request.app.state.config
    conn = db.connect(config.database.path)
    try:
        yield conn
    finally:
        conn.close()


DbDep = Annotated[sqlite3.Connection, Depends(get_db)]
IdentityDep = Annotated[str, Depends(resolve_identity)]


# -- app factory ------------------------------------------------------------


def create_app(config: Optional[Config] = None) -> FastAPI:
    if config is None:
        config = load_config()

    # Build the MCP server up front so the lifespan closure can drive its
    # session-manager startup/shutdown. ``mcp_server`` is imported lazily
    # because the ``mcp`` package requires Python 3.10+ and we want this
    # module to be importable on older interpreters when MCP is disabled.
    mcp_server_instance = None
    if config.mcp.enabled:
        from . import mcp_server as _mcp_module
        mcp_server_instance = _mcp_module.build_mcp_server(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config.database.path.parent.mkdir(parents=True, exist_ok=True)
        applied = db.run_migrations(config.database.path)
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
        if mcp_server_instance is not None:
            # FastAPI does not auto-drive the lifespan of mounted ASGI apps;
            # we have to enter the session manager ourselves.
            async with mcp_server_instance.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(
        title="SquatchTodo",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = config

    @app.exception_handler(ValidationError)
    async def _validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    _register_routes(app)

    # Web UI: mount static files and HTML routes. Imported lazily so the API
    # can be used headlessly (no jinja2 lookup) when only the JSON surface
    # matters — useful for tests and integration scripts.
    from . import web as _web_module
    _web_module.install_web_routes(app)

    if mcp_server_instance is not None:
        from . import mcp_server as _mcp_module
        _mcp_module.install_identity_middleware(app)
        app.mount("/mcp", mcp_server_instance.streamable_http_app())
        log.info("mounted MCP server at /mcp")

    return app


def _register_routes(app: FastAPI) -> None:
    api_prefix = "/api"

    # -- projects -----------------------------------------------------------

    @app.get(f"{api_prefix}/projects", response_model=list[ProjectOut])
    def list_projects(
        conn: DbDep,
        status_filter: Annotated[Optional[ProjectStatus], Query(alias="status")] = None,
        tag: Optional[str] = None,
    ) -> list[Project]:
        return repository.list_projects(conn, status=status_filter, tag=tag)

    @app.post(
        f"{api_prefix}/projects",
        response_model=ProjectOut,
        status_code=status.HTTP_201_CREATED,
    )
    def create_project(
        body: ProjectCreate,
        conn: DbDep,
        identity: IdentityDep,
    ) -> Project:
        return repository.create_project(
            conn,
            name=body.name,
            description=body.description,
            tags=body.tags,
            created_by=identity,
        )

    @app.get(f"{api_prefix}/projects/{{project_id}}", response_model=ProjectDetailOut)
    def get_project(
        conn: DbDep,
        project_id: Annotated[int, Path(ge=1)],
        include_todos: bool = False,
    ) -> Any:
        project = _project_or_404(conn, project_id)
        out = ProjectDetailOut.model_validate(project)
        if include_todos:
            todos = repository.list_todos(conn, project_id=project_id)
            out.todos = _build_todo_tree(todos)
        return out

    @app.patch(f"{api_prefix}/projects/{{project_id}}", response_model=ProjectOut)
    def update_project(
        body: ProjectUpdate,
        conn: DbDep,
        project_id: Annotated[int, Path(ge=1)],
    ) -> Project:
        # Translate Pydantic's "field not sent" into the repository UNSET sentinel.
        kwargs: dict[str, Any] = body.model_dump(exclude_unset=True)
        updated = repository.update_project(conn, project_id, **kwargs)
        if updated is None:
            raise HTTPException(404, f"project {project_id} not found")
        return updated

    # -- todos --------------------------------------------------------------

    @app.get(
        f"{api_prefix}/projects/{{project_id}}/todos",
        response_model=list[TodoOut],
    )
    def list_project_todos(
        conn: DbDep,
        project_id: Annotated[int, Path(ge=1)],
        parent_todo_id: Optional[int] = None,
        only_top_level: bool = False,
        status_filter: Annotated[Optional[TodoStatus], Query(alias="status")] = None,
    ) -> list[Todo]:
        _project_or_404(conn, project_id)
        # Three modes: not provided → flat list of all todos; only_top_level=true
        # → top-level only; parent_todo_id=N → children of N. ``only_top_level``
        # is a separate flag because ``parent_todo_id=null`` over a query string
        # is awkward.
        if parent_todo_id is not None:
            return repository.list_todos(
                conn,
                project_id=project_id,
                parent_todo_id=parent_todo_id,
                status=status_filter,
            )
        if only_top_level:
            return repository.list_todos(
                conn,
                project_id=project_id,
                parent_todo_id=None,
                status=status_filter,
            )
        return repository.list_todos(
            conn, project_id=project_id, status=status_filter
        )

    @app.post(
        f"{api_prefix}/projects/{{project_id}}/todos",
        response_model=TodoOut,
        status_code=status.HTTP_201_CREATED,
    )
    def create_todo(
        body: TodoCreate,
        conn: DbDep,
        identity: IdentityDep,
        project_id: Annotated[int, Path(ge=1)],
    ) -> Todo:
        _project_or_404(conn, project_id)
        return repository.create_todo(
            conn,
            project_id=project_id,
            title=body.title,
            description=body.description,
            parent_todo_id=body.parent_todo_id,
            priority=body.priority,
            created_by=identity,
        )

    @app.get(f"{api_prefix}/todos/{{todo_id}}", response_model=TodoTreeOut)
    def get_todo(
        conn: DbDep,
        todo_id: Annotated[int, Path(ge=1)],
        include_subtodos: bool = False,
        include_notes: bool = False,
    ) -> Any:
        todo = _todo_or_404(conn, todo_id)
        out = TodoTreeOut.model_validate(todo)
        if include_subtodos:
            children = repository.list_todos(
                conn, project_id=todo.project_id, parent_todo_id=todo.id
            )
            out.subtodos = _build_todo_tree(children)
        if include_notes:
            notes = repository.list_notes(conn, todo_id)
            out.notes = [NoteOut.model_validate(n) for n in notes]
        return out

    @app.patch(f"{api_prefix}/todos/{{todo_id}}", response_model=TodoOut)
    def update_todo(
        body: TodoUpdate,
        conn: DbDep,
        todo_id: Annotated[int, Path(ge=1)],
    ) -> Todo:
        kwargs: dict[str, Any] = body.model_dump(exclude_unset=True)
        updated = repository.update_todo(conn, todo_id, **kwargs)
        if updated is None:
            raise HTTPException(404, f"todo {todo_id} not found")
        return updated

    @app.post(f"{api_prefix}/todos/{{todo_id}}/complete", response_model=TodoOut)
    def complete_todo(
        conn: DbDep,
        todo_id: Annotated[int, Path(ge=1)],
    ) -> Todo:
        updated = repository.complete_todo(conn, todo_id)
        if updated is None:
            raise HTTPException(404, f"todo {todo_id} not found")
        return updated

    # -- notes --------------------------------------------------------------

    @app.get(f"{api_prefix}/todos/{{todo_id}}/notes", response_model=list[NoteOut])
    def list_notes(
        conn: DbDep,
        todo_id: Annotated[int, Path(ge=1)],
    ) -> list[Note]:
        _todo_or_404(conn, todo_id)
        return repository.list_notes(conn, todo_id)

    @app.post(
        f"{api_prefix}/todos/{{todo_id}}/notes",
        response_model=NoteOut,
        status_code=status.HTTP_201_CREATED,
    )
    def add_note(
        body: NoteCreate,
        conn: DbDep,
        identity: IdentityDep,
        todo_id: Annotated[int, Path(ge=1)],
    ) -> Note:
        _todo_or_404(conn, todo_id)
        return repository.add_note(
            conn, todo_id=todo_id, content=body.content, author=identity
        )

    # -- search -------------------------------------------------------------

    @app.get(f"{api_prefix}/search", response_model=SearchOut)
    def search(
        conn: DbDep,
        q: Annotated[str, Query(min_length=1)],
        scope: str = "all",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Any:
        results = repository.search(conn, q, scope=scope, limit=limit)
        return SearchOut(
            projects=[ProjectOut.model_validate(p) for p in results.projects],
            todos=[TodoOut.model_validate(t) for t in results.todos],
            notes=[NoteOut.model_validate(n) for n in results.notes],
        )

    # -- health -------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}


# For ad-hoc dev runs without main.py, use the ASGI factory pattern:
#     uvicorn squatchtodo.api:create_app --factory --reload
# main.py is the production entry point — it parses CLI args and configures
# logging.
