"""Tests for the MCP server layer.

The ``mcp`` package requires Python 3.10+; we ``importorskip`` so this file
becomes a no-op on older interpreters used for partial local dev. The tests
exercise tools by calling the registered functions directly through the tool
manager — no HTTP transport involved — which is sufficient to verify the
repository plumbing and identity propagation.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest

try:
    import mcp  # noqa: F401  (the mcp package uses 3.10+ syntax)
except (ImportError, SyntaxError):
    pytest.skip(
        "mcp package unavailable on this Python version", allow_module_level=True
    )

from squatchtodo import mcp_server
from squatchtodo.config import (
    Config,
    DatabaseConfig,
    IdentityConfig,
    McpConfig,
    ServerConfig,
)
from squatchtodo import db


EXPECTED_TOOLS = {
    "list_projects",
    "get_project",
    "create_project",
    "update_project",
    "list_todos",
    "get_todo",
    "create_todo",
    "update_todo",
    "complete_todo",
    "add_note",
    "list_notes",
    "search",
}


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config(
        server=ServerConfig(),
        database=DatabaseConfig(path=tmp_path / "mcp.db"),
        identity=IdentityConfig(default="ellie"),
        mcp=McpConfig(enabled=True),
    )
    db.run_migrations(cfg.database.path)
    return cfg


@pytest.fixture()
def server(config: Config):
    return mcp_server.build_mcp_server(config)


async def _call(server, name: str, **kwargs):
    """Invoke a registered tool by name and return its raw result."""
    return await server._tool_manager.call_tool(name, kwargs, context=None)


# -- structural ------------------------------------------------------------


def test_all_tools_registered(server):
    names = {t.name for t in server._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS


# -- projects ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_projects(server):
    p = await _call(server, "create_project", name="SNH", tags=["fleet"])
    assert p["id"] == 1
    assert p["created_by"] == "ellie"  # configured default

    listed = await _call(server, "list_projects")
    assert len(listed) == 1
    assert listed[0]["id"] == p["id"]


@pytest.mark.asyncio
async def test_get_project_with_todos(server):
    p = await _call(server, "create_project", name="SNH")
    parent = await _call(server, "create_todo", project_id=p["id"], title="parent")
    await _call(
        server,
        "create_todo",
        project_id=p["id"],
        title="child",
        parent_todo_id=parent["id"],
    )

    full = await _call(server, "get_project", project_id=p["id"], include_todos=True)
    assert full is not None
    titles = {t["title"]: t for t in full["todos"]}
    assert "parent" in titles
    assert len(titles["parent"]["subtodos"]) == 1
    assert titles["parent"]["subtodos"][0]["title"] == "child"


@pytest.mark.asyncio
async def test_get_project_missing_returns_none(server):
    assert await _call(server, "get_project", project_id=999) is None


@pytest.mark.asyncio
async def test_update_project_leaves_unsent_fields_alone(server):
    p = await _call(server, "create_project", name="A", description="orig", tags=["x"])
    updated = await _call(server, "update_project", project_id=p["id"], status="paused")
    assert updated["status"] == "paused"
    assert updated["name"] == "A"
    assert updated["description"] == "orig"
    assert updated["tags"] == ["x"]


# -- todos ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_todos_modes(server):
    p = await _call(server, "create_project", name="A")
    a = await _call(server, "create_todo", project_id=p["id"], title="a")
    await _call(server, "create_todo", project_id=p["id"], title="b")
    await _call(
        server,
        "create_todo",
        project_id=p["id"],
        title="c",
        parent_todo_id=a["id"],
    )

    flat = await _call(server, "list_todos", project_id=p["id"])
    assert {t["title"] for t in flat} == {"a", "b", "c"}

    top = await _call(
        server, "list_todos", project_id=p["id"], only_top_level=True
    )
    assert {t["title"] for t in top} == {"a", "b"}

    children = await _call(
        server, "list_todos", project_id=p["id"], parent_todo_id=a["id"]
    )
    assert {t["title"] for t in children} == {"c"}


@pytest.mark.asyncio
async def test_complete_todo(server):
    p = await _call(server, "create_project", name="A")
    t = await _call(server, "create_todo", project_id=p["id"], title="x")
    done = await _call(server, "complete_todo", todo_id=t["id"])
    assert done["status"] == "done"
    assert done["completed_at"] is not None


@pytest.mark.asyncio
async def test_get_todo_with_subtodos_and_notes(server):
    p = await _call(server, "create_project", name="A")
    parent = await _call(server, "create_todo", project_id=p["id"], title="parent")
    await _call(
        server,
        "create_todo",
        project_id=p["id"],
        title="child",
        parent_todo_id=parent["id"],
    )
    await _call(server, "add_note", todo_id=parent["id"], content="tried X")

    full = await _call(
        server,
        "get_todo",
        todo_id=parent["id"],
        include_subtodos=True,
        include_notes=True,
    )
    assert len(full["subtodos"]) == 1
    assert full["subtodos"][0]["title"] == "child"
    assert len(full["notes"]) == 1
    assert full["notes"][0]["content"] == "tried X"


# -- notes ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_and_list_notes(server):
    p = await _call(server, "create_project", name="A")
    t = await _call(server, "create_todo", project_id=p["id"], title="t")
    await _call(server, "add_note", todo_id=t["id"], content="first")
    await _call(server, "add_note", todo_id=t["id"], content="second")
    notes = await _call(server, "list_notes", todo_id=t["id"])
    assert [n["content"] for n in notes] == ["first", "second"]


# -- search -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_search(server):
    p = await _call(
        server, "create_project", name="Heartbeat", description="memory persistence"
    )
    t = await _call(server, "create_todo", project_id=p["id"], title="json parsing")
    await _call(
        server,
        "add_note",
        todo_id=t["id"],
        content="switched to streaming Brotli decode",
    )

    res = await _call(server, "search", query="brotli")
    assert len(res["notes"]) == 1
    assert "Brotli" in res["notes"][0]["content"]

    scoped = await _call(server, "search", query="json", scope="todos")
    assert len(scoped["todos"]) == 1
    assert scoped["projects"] == []
    assert scoped["notes"] == []


# -- identity propagation ---------------------------------------------------


@pytest.mark.asyncio
async def test_identity_contextvar_overrides_default(server, config):
    """Setting the contextvar (as the middleware would) routes to created_by."""
    token = mcp_server._identity_var.set("halo")
    try:
        p = await _call(server, "create_project", name="from-halo")
        assert p["created_by"] == "halo"

        t = await _call(server, "create_todo", project_id=p["id"], title="from-halo")
        assert t["created_by"] == "halo"

        n = await _call(server, "add_note", todo_id=t["id"], content="halo note")
        assert n["author"] == "halo"
    finally:
        mcp_server._identity_var.reset(token)

    # After reset, falls back to the configured default.
    p2 = await _call(server, "create_project", name="from-default")
    assert p2["created_by"] == config.identity.default


# -- FastAPI integration ----------------------------------------------------


def test_mcp_mounts_on_fastapi(tmp_path: Path):
    """End-to-end: create_app produces a FastAPI with /mcp mounted and a
    health route still reachable."""
    from fastapi.testclient import TestClient

    from squatchtodo.api import create_app

    cfg = Config(
        server=ServerConfig(),
        database=DatabaseConfig(path=tmp_path / "integ.db"),
        identity=IdentityConfig(default="ellie"),
        mcp=McpConfig(enabled=True),
    )
    app = create_app(cfg)

    has_mcp_mount = any(
        getattr(r, "path", "") == "/mcp" for r in app.routes
    )
    assert has_mcp_mount, "expected /mcp mount in app.routes"

    with TestClient(app) as client:
        # Health stays available with MCP mounted.
        r = client.get("/healthz")
        assert r.status_code == 200
        # /mcp without proper MCP handshake should not 404 — it accepts the
        # request and responds with an MCP-protocol error rather than nothing.
        r = client.get("/mcp/")
        assert r.status_code != 404
