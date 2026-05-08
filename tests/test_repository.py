"""Tests for the repository layer.

These run against a real SQLite database (a fresh temp file per test).
Mocking SQLite would defeat the point — CHECK constraints, FK rules, FTS
triggers, and updated_at triggers are all part of the contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from squatchtodo import db, repository
from squatchtodo.repository import ValidationError


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.run_migrations(db_path)
    connection = db.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


# -- projects ---------------------------------------------------------------


def test_create_project_minimal(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="SNH", created_by="ellie")
    assert p.id == 1
    assert p.name == "SNH"
    assert p.description is None
    assert p.status == "active"
    assert p.tags == []
    assert p.created_by == "ellie"
    assert p.created_at == p.updated_at


def test_create_project_with_tags_and_description(conn: sqlite3.Connection):
    p = repository.create_project(
        conn,
        name="Aqueduct Fleet",
        description="Cross-fleet AI orchestration",
        tags=["fleet", "ops"],
        created_by="ellie",
    )
    assert p.tags == ["fleet", "ops"]
    assert p.description == "Cross-fleet AI orchestration"


def test_create_project_rejects_blank_name(conn: sqlite3.Connection):
    with pytest.raises(ValidationError):
        repository.create_project(conn, name="   ", created_by="ellie")


def test_create_project_rejects_bad_tags(conn: sqlite3.Connection):
    with pytest.raises(ValidationError):
        repository.create_project(
            conn, name="x", created_by="ellie", tags=["", "ok"]
        )


def test_list_projects_filters_by_status_and_tag(conn: sqlite3.Connection):
    repository.create_project(conn, name="A", created_by="ellie", tags=["client"])
    b = repository.create_project(conn, name="B", created_by="ellie", tags=["fleet"])
    repository.update_project(conn, b.id, status="archived")

    actives = repository.list_projects(conn, status="active")
    assert [p.name for p in actives] == ["A"]

    fleet = repository.list_projects(conn, tag="fleet")
    assert [p.name for p in fleet] == ["B"]

    archived = repository.list_projects(conn, status="archived")
    assert [p.name for p in archived] == ["B"]


def test_update_project_no_args_returns_existing(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    same = repository.update_project(conn, p.id)
    assert same is not None and same.id == p.id


def test_update_project_missing_returns_none(conn: sqlite3.Connection):
    assert repository.update_project(conn, 99, name="x") is None


def test_update_project_partial(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    updated = repository.update_project(conn, p.id, status="paused")
    assert updated is not None
    assert updated.status == "paused"
    assert updated.name == "A"  # untouched


# -- todos ------------------------------------------------------------------


def test_create_todo_basic(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    t = repository.create_todo(
        conn, project_id=p.id, title="First", created_by="ellie"
    )
    assert t.id == 1
    assert t.title == "First"
    assert t.status == "open"
    assert t.priority == "medium"
    assert t.parent_todo_id is None
    assert t.completed_at is None


def test_create_subtodo(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    parent = repository.create_todo(
        conn, project_id=p.id, title="parent", created_by="ellie"
    )
    child = repository.create_todo(
        conn,
        project_id=p.id,
        parent_todo_id=parent.id,
        title="child",
        created_by="halo",
    )
    assert child.parent_todo_id == parent.id
    assert child.created_by == "halo"


def test_create_subtodo_rejects_cross_project(conn: sqlite3.Connection):
    p1 = repository.create_project(conn, name="A", created_by="ellie")
    p2 = repository.create_project(conn, name="B", created_by="ellie")
    parent = repository.create_todo(
        conn, project_id=p1.id, title="parent", created_by="ellie"
    )
    with pytest.raises(ValidationError):
        repository.create_todo(
            conn,
            project_id=p2.id,
            parent_todo_id=parent.id,
            title="child",
            created_by="ellie",
        )


def test_list_todos_parent_modes(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    a = repository.create_todo(
        conn, project_id=p.id, title="a", created_by="ellie"
    )
    b = repository.create_todo(
        conn, project_id=p.id, title="b", created_by="ellie"
    )
    c = repository.create_todo(
        conn,
        project_id=p.id,
        parent_todo_id=a.id,
        title="c",
        created_by="ellie",
    )

    flat = repository.list_todos(conn, project_id=p.id)
    assert {t.title for t in flat} == {"a", "b", "c"}

    top_only = repository.list_todos(conn, project_id=p.id, parent_todo_id=None)
    assert {t.title for t in top_only} == {"a", "b"}

    children_of_a = repository.list_todos(
        conn, project_id=p.id, parent_todo_id=a.id
    )
    assert {t.title for t in children_of_a} == {"c"}


def test_complete_todo_sets_completed_at(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    t = repository.create_todo(
        conn, project_id=p.id, title="t", created_by="ellie"
    )
    done = repository.complete_todo(conn, t.id)
    assert done is not None
    assert done.status == "done"
    assert done.completed_at is not None


def test_reopen_todo_clears_completed_at(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    t = repository.create_todo(
        conn, project_id=p.id, title="t", created_by="ellie"
    )
    repository.complete_todo(conn, t.id)
    reopened = repository.update_todo(conn, t.id, status="in_progress")
    assert reopened is not None
    assert reopened.completed_at is None


def test_update_todo_rejects_bad_priority(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    t = repository.create_todo(
        conn, project_id=p.id, title="t", created_by="ellie"
    )
    with pytest.raises(ValidationError):
        repository.update_todo(conn, t.id, priority="urgent")  # type: ignore[arg-type]


# -- notes ------------------------------------------------------------------


def test_add_and_list_notes(conn: sqlite3.Connection):
    p = repository.create_project(conn, name="A", created_by="ellie")
    t = repository.create_todo(
        conn, project_id=p.id, title="t", created_by="ellie"
    )
    n1 = repository.add_note(
        conn, todo_id=t.id, content="tried X", author="halo"
    )
    n2 = repository.add_note(
        conn, todo_id=t.id, content="tried Y", author="ellie"
    )
    notes = repository.list_notes(conn, t.id)
    assert [n.id for n in notes] == [n1.id, n2.id]
    assert notes[0].author == "halo"


def test_add_note_to_missing_todo_raises(conn: sqlite3.Connection):
    with pytest.raises(ValidationError):
        repository.add_note(conn, todo_id=999, content="x", author="ellie")


# -- search -----------------------------------------------------------------


def test_search_across_scopes(conn: sqlite3.Connection):
    p = repository.create_project(
        conn,
        name="Heartbeat memory",
        description="Persistence work",
        tags=["fleet"],
        created_by="ellie",
    )
    t = repository.create_todo(
        conn,
        project_id=p.id,
        title="Fix JSON parsing",
        description="Truncated heartbeat lines blow up parser",
        created_by="halo",
    )
    repository.add_note(
        conn, todo_id=t.id, content="Switched to streaming Brotli decode", author="ellie"
    )

    res = repository.search(conn, "heartbeat")
    assert any(pp.id == p.id for pp in res.projects)
    assert any(tt.id == t.id for tt in res.todos)

    json_only = repository.search(conn, "json", scope="todos")
    assert any(tt.id == t.id for tt in json_only.todos)
    assert json_only.projects == []
    assert json_only.notes == []

    brotli = repository.search(conn, "brotli", scope="notes")
    assert len(brotli.notes) == 1


def test_search_empty_query(conn: sqlite3.Connection):
    res = repository.search(conn, "   ")
    assert res.projects == [] and res.todos == [] and res.notes == []


def test_search_strips_fts_metacharacters(conn: sqlite3.Connection):
    """Raw user input that would crash a naive MATCH expression must be safe."""
    p = repository.create_project(
        conn, name="quotes are tricky", created_by="ellie"
    )
    # Quotes and the bare word "OR" are FTS5 syntax — naive interpolation
    # would either raise OperationalError or treat "OR" as a real operator.
    res = repository.search(conn, '"tricky"')
    assert any(pp.id == p.id for pp in res.projects)
    # Bare operator words are stripped to plain tokens. With implicit-AND,
    # the project doesn't contain "OR" or "injection" so no match is expected
    # — the assertion here is that the call succeeds without raising.
    repository.search(conn, '"tricky" OR injection')
    repository.search(conn, "(((")
