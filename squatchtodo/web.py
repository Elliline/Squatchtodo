"""HTML web UI routes.

Distinct from the JSON API: page routes (``/``, ``/projects/{id}``,
``/todos/{id}``, ``/search``) render Jinja2 templates; HTMX endpoints under
``/web/`` return HTML *fragments* that the browser swaps into the existing
DOM. Keeping the two surfaces separate avoids content negotiation in the API
layer and lets the fragments evolve without breaking the JSON contract.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import repository
from .api import DbDep, IdentityDep, _build_todo_tree, _project_or_404, _todo_or_404
from .config import Config
from .models import Note, Project, Todo

PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _parse_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _todo_dict_with_subtodos(todos: list[Todo]) -> list[dict[str, Any]]:
    """Flat list of todos -> nested forest of dicts with ``subtodos`` key."""
    nodes: dict[int, dict[str, Any]] = {}
    for t in todos:
        node = {
            "id": t.id,
            "project_id": t.project_id,
            "parent_todo_id": t.parent_todo_id,
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "priority": t.priority,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
            "completed_at": t.completed_at,
            "created_by": t.created_by,
            "subtodos": [],
        }
        nodes[t.id] = node
    roots: list[dict[str, Any]] = []
    for t in todos:
        node = nodes[t.id]
        if t.parent_todo_id is None or t.parent_todo_id not in nodes:
            roots.append(node)
        else:
            nodes[t.parent_todo_id]["subtodos"].append(node)
    return roots


def _human_identity(request: Request) -> str:
    config: Config = request.app.state.config
    return config.identity.default


# -- page routes ------------------------------------------------------------


def _build_page_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        conn: DbDep,
        status: Annotated[Optional[str], Query()] = None,
    ):
        # Default view: active projects. ``?status=archived`` flips to retired.
        status_filter = status if status in ("active", "paused", "archived") else "active"
        projects = repository.list_projects(conn, status=status_filter)

        # Per-project todo counts (open / in_progress / blocked / done) — one
        # batched query for the whole list rather than N+1.
        counts: dict[int, dict[str, int]] = defaultdict(
            lambda: {"open": 0, "in_progress": 0, "blocked": 0, "done": 0, "archived": 0}
        )
        if projects:
            placeholders = ",".join(["?"] * len(projects))
            rows = conn.execute(
                f"""
                SELECT project_id, status, COUNT(*) AS n
                  FROM todos
                 WHERE project_id IN ({placeholders})
                 GROUP BY project_id, status
                """,
                tuple(p.id for p in projects),
            ).fetchall()
            for r in rows:
                counts[r["project_id"]][r["status"]] = r["n"]

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "projects": projects,
                "counts": counts,
                "status_filter": status_filter,
                "human_identity": _human_identity(request),
                "active_nav": "archived" if status_filter == "archived" else "projects",
            },
        )

    @router.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_detail(
        request: Request,
        conn: DbDep,
        project_id: int,
    ):
        project = _project_or_404(conn, project_id)
        todos = repository.list_todos(conn, project_id=project_id)
        tree = _todo_dict_with_subtodos(todos)

        # Recent notes across the project — most recent first, capped.
        recent_rows = conn.execute(
            """
            SELECT notes.*, todos.title AS todo_title
              FROM notes
              JOIN todos ON todos.id = notes.todo_id
             WHERE todos.project_id = ?
             ORDER BY notes.id DESC
             LIMIT 8
            """,
            (project_id,),
        ).fetchall()
        recent_notes = [
            {
                "todo_id": r["todo_id"],
                "todo_title": r["todo_title"],
                "note": Note(
                    id=r["id"],
                    todo_id=r["todo_id"],
                    content=r["content"],
                    author=r["author"],
                    created_at=r["created_at"],
                ),
            }
            for r in recent_rows
        ]

        return templates.TemplateResponse(
            request,
            "project_detail.html",
            {
                "project": project,
                "todo_tree": tree,
                "todo_count": len(todos),
                "recent_notes": recent_notes,
                "human_identity": _human_identity(request),
                "active_nav": "projects",
            },
        )

    @router.get("/todos/{todo_id}", response_class=HTMLResponse)
    def todo_detail(request: Request, conn: DbDep, todo_id: int):
        todo = _todo_or_404(conn, todo_id)
        project = repository.get_project(conn, todo.project_id)
        if project is None:
            raise HTTPException(404, "todo's project no longer exists")
        parent = (
            repository.get_todo(conn, todo.parent_todo_id)
            if todo.parent_todo_id
            else None
        )
        children_flat = repository.list_todos(
            conn, project_id=todo.project_id, parent_todo_id=todo.id
        )
        # Direct children are rendered as subtodos here. We don't recurse all
        # the way down — that lives on the project detail page tree view.
        subtodos = _todo_dict_with_subtodos(children_flat)
        notes = repository.list_notes(conn, todo_id)

        return templates.TemplateResponse(
            request,
            "todo_detail.html",
            {
                "todo": todo,
                "project": project,
                "parent": parent,
                "subtodos": subtodos,
                "notes": notes,
                "human_identity": _human_identity(request),
                "active_nav": "projects",
            },
        )

    @router.get("/search", response_class=HTMLResponse)
    def search(
        request: Request,
        conn: DbDep,
        q: Annotated[Optional[str], Query()] = None,
    ):
        query = (q or "").strip()
        if query:
            results = repository.search(conn, query)
            total = (
                len(results.projects) + len(results.todos) + len(results.notes)
            )
        else:
            results = None
            total = 0

        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "query": query,
                "results": results,
                "total": total,
                "search_query": query,
                "human_identity": _human_identity(request),
                "active_nav": None,
            },
        )

    return router


# -- HTMX fragment routes ---------------------------------------------------


def _build_htmx_router() -> APIRouter:
    router = APIRouter(prefix="/web")

    @router.post("/projects")
    def create_project_form(
        conn: DbDep,
        identity: IdentityDep,
        name: Annotated[str, Form(...)],
        tags: Annotated[Optional[str], Form()] = "",
        description: Annotated[Optional[str], Form()] = "",
    ):
        project = repository.create_project(
            conn,
            name=name,
            description=description or None,
            tags=_parse_tags(tags),
            created_by=identity,
        )
        # Plain form post (no HTMX) — redirect to the new project page.
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @router.post("/projects/{project_id}/status")
    def set_project_status(
        conn: DbDep,
        project_id: int,
        status: Annotated[str, Form(...)],
    ):
        updated = repository.update_project(conn, project_id, status=status)  # type: ignore[arg-type]
        if updated is None:
            raise HTTPException(404, "project not found")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @router.post("/projects/{project_id}/todos", response_class=HTMLResponse)
    def add_project_todo(
        request: Request,
        conn: DbDep,
        identity: IdentityDep,
        project_id: int,
        title: Annotated[str, Form(...)],
        priority: Annotated[str, Form()] = "medium",
    ):
        _project_or_404(conn, project_id)
        todo = repository.create_todo(
            conn,
            project_id=project_id,
            title=title,
            priority=priority,  # type: ignore[arg-type]
            created_by=identity,
        )
        # HTMX swap: append a new <li> with the freshly-created todo. Plain
        # form posts (no HTMX) fall back to a full-page redirect.
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "partials/todo_node.html",
                {
                    "t": _todo_dict_with_subtodos([todo])[0],
                    "human_identity": _human_identity(request),
                    "project_id": project_id,
                },
            )
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @router.post("/todos/{todo_id}/subtodos", response_class=HTMLResponse)
    def add_subtodo(
        request: Request,
        conn: DbDep,
        identity: IdentityDep,
        todo_id: int,
        title: Annotated[str, Form(...)],
        priority: Annotated[str, Form()] = "medium",
    ):
        parent = _todo_or_404(conn, todo_id)
        child = repository.create_todo(
            conn,
            project_id=parent.project_id,
            parent_todo_id=parent.id,
            title=title,
            priority=priority,  # type: ignore[arg-type]
            created_by=identity,
        )
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "partials/todo_node.html",
                {
                    "t": _todo_dict_with_subtodos([child])[0],
                    "human_identity": _human_identity(request),
                    "project_id": parent.project_id,
                },
            )
        return RedirectResponse(f"/todos/{todo_id}", status_code=303)

    @router.post("/todos/{todo_id}/toggle", response_class=HTMLResponse)
    def toggle_todo(request: Request, conn: DbDep, todo_id: int):
        todo = _todo_or_404(conn, todo_id)
        new_status = "open" if todo.status == "done" else "done"
        updated = repository.update_todo(conn, todo_id, status=new_status)  # type: ignore[arg-type]
        assert updated is not None
        # Re-render the same single <li> node so HTMX can outerHTML-swap it.
        # We need to fetch direct children too so the subtree stays intact
        # under the toggled node.
        children = repository.list_todos(
            conn, project_id=updated.project_id, parent_todo_id=updated.id
        )
        node_root = _todo_dict_with_subtodos([updated, *children])
        # _todo_dict_with_subtodos rebuilds the forest — the toggled todo is
        # the root because no parent in this slice owns it.
        return templates.TemplateResponse(
            request,
            "partials/todo_node.html",
            {
                "t": node_root[0],
                "human_identity": _human_identity(request),
                "project_id": updated.project_id,
            },
        )

    @router.post("/todos/{todo_id}/edit")
    def edit_todo(
        conn: DbDep,
        todo_id: int,
        title: Annotated[str, Form(...)],
        description: Annotated[Optional[str], Form()] = None,
        status: Annotated[str, Form(...)] = "open",
        priority: Annotated[str, Form(...)] = "medium",
    ):
        # Web form posts come back as full-page redirects. The status select
        # on the project page submits to /web/projects/.../status above.
        updated = repository.update_todo(
            conn,
            todo_id,
            title=title,
            description=description if description else None,
            status=status,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
        )
        if updated is None:
            raise HTTPException(404, "todo not found")
        return RedirectResponse(f"/todos/{todo_id}", status_code=303)

    @router.post("/todos/{todo_id}/notes", response_class=HTMLResponse)
    def add_note_form(
        request: Request,
        conn: DbDep,
        identity: IdentityDep,
        todo_id: int,
        content: Annotated[str, Form(...)],
    ):
        _todo_or_404(conn, todo_id)
        note = repository.add_note(
            conn, todo_id=todo_id, content=content, author=identity
        )
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "partials/note_entry.html",
                {"note": note},
            )
        return RedirectResponse(f"/todos/{todo_id}", status_code=303)

    return router


def install_web_routes(app: FastAPI) -> None:
    """Mount static files, register page routes, and HTMX fragment routes."""
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(_build_page_router())
    app.include_router(_build_htmx_router())
