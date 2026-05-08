"""Tests for the bootstrap seeding script.

These cover idempotency, partial top-up, and dry-run — the three behaviours
that matter for "safe to run after every PROJECT.md edit".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import bootstrap
from squatchtodo import db, repository


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "boot.db"
    db.run_migrations(db_path)
    c = db.connect(db_path)
    try:
        yield c
    finally:
        c.close()


def test_seed_creates_full_set(conn):
    p_count, t_count = bootstrap.seed(conn, identity="ellie")
    assert p_count == len(bootstrap.BOOTSTRAP_PROJECTS)
    expected_todos = sum(len(s.get("todos", [])) for s in bootstrap.BOOTSTRAP_PROJECTS)
    assert t_count == expected_todos

    # All project names match the seed list, all created_by=ellie.
    projects = {p.name: p for p in repository.list_projects(conn)}
    assert set(projects) == {s["name"] for s in bootstrap.BOOTSTRAP_PROJECTS}
    assert all(p.created_by == "ellie" for p in projects.values())


def test_seed_is_idempotent(conn):
    bootstrap.seed(conn, identity="ellie")
    second = bootstrap.seed(conn, identity="ellie")
    assert second == (0, 0)


def test_seed_tops_up_missing_todos(conn):
    bootstrap.seed(conn, identity="ellie")
    snh = next(p for p in repository.list_projects(conn) if p.name == "SNH")
    # Archive (don't delete — repository has no delete) one of the SNH todos
    # by manually rewriting its title so the seeder treats it as "missing".
    conn.execute(
        "UPDATE todos SET title = 'something else' WHERE project_id = ? AND title = ?",
        (snh.id, "Memory archive system"),
    )
    p_added, t_added = bootstrap.seed(conn, identity="ellie")
    assert p_added == 0
    assert t_added == 1
    # The original title is back.
    titles = {t.title for t in repository.list_todos(conn, project_id=snh.id)}
    assert "Memory archive system" in titles


def test_dry_run_does_not_write(conn):
    p_added, t_added = bootstrap.seed(conn, identity="ellie", dry_run=True)
    # Dry-run reports zero (it doesn't tally would-creates) and the DB stays empty.
    assert p_added == 0
    assert t_added == 0
    assert repository.list_projects(conn) == []


def test_seed_uses_supplied_identity(conn):
    bootstrap.seed(conn, identity="halo")
    assert all(p.created_by == "halo" for p in repository.list_projects(conn))


def test_seed_data_matches_project_md_shape():
    """Sanity guard: every seed entry has a name and at least one todo
    (except where intentionally omitted) so the script never silently
    creates a no-todo project that PROJECT.md actually populated."""
    for s in bootstrap.BOOTSTRAP_PROJECTS:
        assert s["name"], s
        assert isinstance(s.get("tags", []), list)
        assert isinstance(s.get("todos", []), list)
        # Every project we seed has at least one todo per PROJECT.md.
        assert len(s["todos"]) >= 1, s["name"]
