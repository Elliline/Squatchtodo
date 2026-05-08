"""End-to-end tests for the FastAPI REST API.

We exercise the live ASGI app against a per-test SQLite file so the migrations,
connection lifecycle, identity resolution, and the FastAPI<->repository
boundary are all exercised. No mocks.
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
        database=DatabaseConfig(path=tmp_path / "test.db"),
        identity=IdentityConfig(default="ellie"),
        mcp=McpConfig(enabled=False),
    )
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def _create_project(client: TestClient, **overrides) -> dict:
    body = {"name": "SNH"} | overrides
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _create_todo(client: TestClient, project_id: int, **overrides) -> dict:
    body = {"title": "First"} | overrides
    r = client.post(f"/api/projects/{project_id}/todos", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# -- health -----------------------------------------------------------------


def test_healthz(client: TestClient):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# -- projects ---------------------------------------------------------------


def test_create_and_list_project(client: TestClient):
    p = _create_project(client, description="memory persistence", tags=["fleet"])
    assert p["id"] == 1
    assert p["created_by"] == "ellie"
    assert p["status"] == "active"
    assert p["tags"] == ["fleet"]

    r = client.get("/api/projects")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == p["id"]


def test_create_project_uses_identity_header(client: TestClient):
    r = client.post(
        "/api/projects",
        json={"name": "Aqueduct"},
        headers={"X-Squatchtodo-Identity": "halo"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == "halo"


def test_create_project_validation(client: TestClient):
    r = client.post("/api/projects", json={"name": ""})
    assert r.status_code == 422  # Pydantic length check


def test_get_project_404(client: TestClient):
    r = client.get("/api/projects/999")
    assert r.status_code == 404


def test_list_projects_filters(client: TestClient):
    p1 = _create_project(client, name="A", tags=["client"])
    p2 = _create_project(client, name="B", tags=["fleet"])
    client.patch(f"/api/projects/{p2['id']}", json={"status": "archived"})

    actives = client.get("/api/projects", params={"status": "active"}).json()
    assert {p["name"] for p in actives} == {"A"}

    fleet = client.get("/api/projects", params={"tag": "fleet"}).json()
    assert {p["name"] for p in fleet} == {"B"}


def test_update_project_partial_does_not_touch_other_fields(client: TestClient):
    p = _create_project(client, name="A", description="orig", tags=["client"])
    r = client.patch(f"/api/projects/{p['id']}", json={"status": "paused"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body["name"] == "A"
    assert body["description"] == "orig"
    assert body["tags"] == ["client"]


def test_update_project_can_clear_description(client: TestClient):
    p = _create_project(client, description="will go away")
    r = client.patch(f"/api/projects/{p['id']}", json={"description": None})
    assert r.status_code == 200
    assert r.json()["description"] is None


def test_update_project_rejects_unknown_status(client: TestClient):
    p = _create_project(client)
    r = client.patch(f"/api/projects/{p['id']}", json={"status": "wibbly"})
    assert r.status_code == 422


def test_get_project_with_todo_tree(client: TestClient):
    p = _create_project(client)
    parent = _create_todo(client, p["id"], title="parent")
    _create_todo(client, p["id"], title="child", parent_todo_id=parent["id"])
    _create_todo(client, p["id"], title="other top-level")

    r = client.get(
        f"/api/projects/{p['id']}", params={"include_todos": "true"}
    )
    assert r.status_code == 200
    body = r.json()
    titles = {t["title"]: t for t in body["todos"]}
    assert set(titles) == {"parent", "other top-level"}
    assert len(titles["parent"]["subtodos"]) == 1
    assert titles["parent"]["subtodos"][0]["title"] == "child"


# -- todos ------------------------------------------------------------------


def test_create_todo_in_missing_project_404s(client: TestClient):
    r = client.post("/api/projects/999/todos", json={"title": "x"})
    assert r.status_code == 404


def test_list_project_todos_modes(client: TestClient):
    p = _create_project(client)
    a = _create_todo(client, p["id"], title="a")
    b = _create_todo(client, p["id"], title="b")
    _create_todo(client, p["id"], title="c", parent_todo_id=a["id"])

    flat = client.get(f"/api/projects/{p['id']}/todos").json()
    assert {t["title"] for t in flat} == {"a", "b", "c"}

    top = client.get(
        f"/api/projects/{p['id']}/todos", params={"only_top_level": "true"}
    ).json()
    assert {t["title"] for t in top} == {"a", "b"}

    children = client.get(
        f"/api/projects/{p['id']}/todos",
        params={"parent_todo_id": a["id"]},
    ).json()
    assert {t["title"] for t in children} == {"c"}


def test_get_todo_with_children_and_notes(client: TestClient):
    p = _create_project(client)
    parent = _create_todo(client, p["id"], title="parent")
    _create_todo(client, p["id"], title="child", parent_todo_id=parent["id"])
    client.post(
        f"/api/todos/{parent['id']}/notes",
        json={"content": "tried X"},
        headers={"X-Squatchtodo-Identity": "halo"},
    )

    r = client.get(
        f"/api/todos/{parent['id']}",
        params={"include_subtodos": "true", "include_notes": "true"},
    )
    body = r.json()
    assert len(body["subtodos"]) == 1
    assert body["subtodos"][0]["title"] == "child"
    assert len(body["notes"]) == 1
    assert body["notes"][0]["author"] == "halo"


def test_complete_todo_endpoint(client: TestClient):
    p = _create_project(client)
    t = _create_todo(client, p["id"])
    r = client.post(f"/api/todos/{t['id']}/complete")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["completed_at"] is not None


def test_complete_missing_todo_404s(client: TestClient):
    r = client.post("/api/todos/999/complete")
    assert r.status_code == 404


def test_update_todo_status_clears_completed_at(client: TestClient):
    p = _create_project(client)
    t = _create_todo(client, p["id"])
    client.post(f"/api/todos/{t['id']}/complete")
    r = client.patch(f"/api/todos/{t['id']}", json={"status": "in_progress"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_progress"
    assert body["completed_at"] is None


# -- notes ------------------------------------------------------------------


def test_add_note_uses_identity_header(client: TestClient):
    p = _create_project(client)
    t = _create_todo(client, p["id"])
    r = client.post(
        f"/api/todos/{t['id']}/notes",
        json={"content": "tried Brotli streaming"},
        headers={"X-Squatchtodo-Identity": "halo"},
    )
    assert r.status_code == 201
    assert r.json()["author"] == "halo"

    listed = client.get(f"/api/todos/{t['id']}/notes").json()
    assert len(listed) == 1
    assert listed[0]["content"] == "tried Brotli streaming"


def test_list_notes_for_missing_todo_404s(client: TestClient):
    r = client.get("/api/todos/999/notes")
    assert r.status_code == 404


# -- search -----------------------------------------------------------------


def test_search_endpoint(client: TestClient):
    p = _create_project(
        client, name="Heartbeat", description="memory persistence"
    )
    t = _create_todo(client, p["id"], title="Fix JSON parsing")
    client.post(
        f"/api/todos/{t['id']}/notes",
        json={"content": "switched to streaming Brotli decode"},
    )

    r = client.get("/api/search", params={"q": "brotli"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["notes"]) == 1
    assert body["notes"][0]["content"].lower().startswith("switched")


def test_search_scoped(client: TestClient):
    p = _create_project(client, name="Heartbeat")
    _create_todo(client, p["id"], title="json work")
    r = client.get("/api/search", params={"q": "json", "scope": "todos"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["todos"]) == 1
    assert body["projects"] == []
    assert body["notes"] == []


def test_search_rejects_empty(client: TestClient):
    r = client.get("/api/search", params={"q": ""})
    assert r.status_code == 422
