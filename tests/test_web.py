"""Smoke tests for the HTML web UI.

We assert structurally — page renders 200, key markup is present, HTMX
endpoints return the expected fragment shape — rather than parsing HTML
exactly. The goal is to catch regressions in routes/templates, not to verify
visual layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from squatchtodo.api import create_app
from squatchtodo.config import (
    Config,
    DatabaseConfig,
    IdentityConfig,
    McpConfig,
    ServerConfig,
)


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = Config(
        server=ServerConfig(),
        database=DatabaseConfig(path=tmp_path / "web.db"),
        identity=IdentityConfig(default="ellie"),
        mcp=McpConfig(enabled=False),
    )
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _seed_basic(client: TestClient) -> tuple[int, int]:
    """Create a project + a top-level todo. Returns (project_id, todo_id)."""
    p = client.post("/api/projects", json={"name": "SNH", "tags": ["fleet"]}).json()
    t = client.post(
        f"/api/projects/{p['id']}/todos",
        json={"title": "Fix JSON parsing", "priority": "high"},
        headers={"X-Squatchtodo-Identity": "halo"},
    ).json()
    return p["id"], t["id"]


# -- pages ------------------------------------------------------------------


def test_index_empty(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "SquatchTodo" in r.text
    assert "Active projects" in r.text
    assert "No active projects" in r.text


def test_index_lists_projects(client: TestClient):
    _seed_basic(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "SNH" in r.text
    assert "fleet" in r.text  # tag rendered


def test_index_archived_filter(client: TestClient):
    pid, _ = _seed_basic(client)
    client.patch(f"/api/projects/{pid}", json={"status": "archived"})
    active = client.get("/").text
    archived = client.get("/?status=archived").text
    assert "SNH" not in active
    assert "SNH" in archived
    assert "Archived projects" in archived


def test_project_detail(client: TestClient):
    pid, tid = _seed_basic(client)
    client.post(
        f"/api/projects/{pid}/todos",
        json={"title": "child", "parent_todo_id": tid},
    )
    client.post(
        f"/api/todos/{tid}/notes",
        json={"content": "tried streaming Brotli decode"},
        headers={"X-Squatchtodo-Identity": "halo"},
    )
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "Fix JSON parsing" in r.text
    assert "child" in r.text  # subtree rendered
    assert "Recent notes" in r.text
    assert "streaming Brotli" in r.text
    assert 'id="todo-' in r.text  # HTMX swap targets present


def test_project_detail_404(client: TestClient):
    r = client.get("/projects/999")
    assert r.status_code == 404


def test_todo_detail(client: TestClient):
    pid, tid = _seed_basic(client)
    r = client.get(f"/todos/{tid}")
    assert r.status_code == 200
    assert "Fix JSON parsing" in r.text
    assert "halo" in r.text  # bot tag for AI-created todo
    assert "/projects/{}".format(pid) in r.text  # crumb link


def test_search_page_empty_query(client: TestClient):
    r = client.get("/search")
    assert r.status_code == 200
    assert "Search" in r.text
    # No section labels until a query is provided.
    assert "no project hits" not in r.text


def test_search_page_with_query(client: TestClient):
    _seed_basic(client)
    r = client.get("/search", params={"q": "json"})
    assert r.status_code == 200
    assert "Fix JSON parsing" in r.text


# -- HTMX fragments ---------------------------------------------------------


def test_htmx_toggle_marks_done(client: TestClient):
    _, tid = _seed_basic(client)
    r = client.post(f"/web/todos/{tid}/toggle", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "todo-row done" in r.text
    assert 'id="todo-{}"'.format(tid) in r.text
    # Toggling again flips back to open.
    r2 = client.post(f"/web/todos/{tid}/toggle", headers={"HX-Request": "true"})
    assert "todo-row done" not in r2.text
    assert "todo-status-open" in r2.text


def test_htmx_add_todo_returns_node(client: TestClient):
    pid, _ = _seed_basic(client)
    r = client.post(
        f"/web/projects/{pid}/todos",
        data={"title": "added via HTMX", "priority": "critical"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "added via HTMX" in r.text
    assert "priority-critical" in r.text


def test_htmx_add_subtodo_returns_node(client: TestClient):
    _, tid = _seed_basic(client)
    r = client.post(
        f"/web/todos/{tid}/subtodos",
        data={"title": "subtodo via HTMX"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "subtodo via HTMX" in r.text


def test_htmx_add_note_returns_entry(client: TestClient):
    _, tid = _seed_basic(client)
    r = client.post(
        f"/web/todos/{tid}/notes",
        data={"content": "tried streaming"},
        headers={"HX-Request": "true", "X-Squatchtodo-Identity": "halo"},
    )
    assert r.status_code == 200
    assert "note-entry" in r.text
    assert "tried streaming" in r.text
    assert "halo" in r.text


def test_form_post_without_htmx_redirects(client: TestClient):
    """Plain form posts (no HX-Request header) fall back to a full-page redirect."""
    pid, _ = _seed_basic(client)
    r = client.post(
        f"/web/projects/{pid}/todos",
        data={"title": "from form", "priority": "low"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/projects/{pid}"


def test_create_project_form(client: TestClient):
    r = client.post(
        "/web/projects",
        data={"name": "New from form", "tags": "alpha, beta", "description": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Resulting project carries the parsed tags.
    project_id = int(r.headers["location"].rsplit("/", 1)[-1])
    detail = client.get(f"/api/projects/{project_id}").json()
    assert detail["tags"] == ["alpha", "beta"]


def test_set_project_status_form(client: TestClient):
    pid, _ = _seed_basic(client)
    r = client.post(
        f"/web/projects/{pid}/status",
        data={"status": "paused"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/api/projects/{pid}").json()["status"] == "paused"


def test_edit_todo_form(client: TestClient):
    _, tid = _seed_basic(client)
    r = client.post(
        f"/web/todos/{tid}/edit",
        data={
            "title": "Edited title",
            "description": "new body",
            "status": "in_progress",
            "priority": "critical",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = client.get(f"/api/todos/{tid}").json()
    assert body["title"] == "Edited title"
    assert body["description"] == "new body"
    assert body["status"] == "in_progress"
    assert body["priority"] == "critical"


# -- static assets ----------------------------------------------------------


def test_static_files_served(client: TestClient):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert b":root" in r.content

    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert b"htmx" in r.content[:200]
