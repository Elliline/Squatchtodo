"""Dataclasses for the SquatchTodo domain entities.

These mirror the SQL schema directly. Timestamps stay as ISO-8601 strings —
SQLite only stores TEXT/REAL/INTEGER, and round-tripping to ``datetime`` is a
boundary concern (API serialisation, template rendering) rather than a
data-model concern. Enum-valued columns are typed via ``Literal`` so callers
get static type checking, but the database CHECK constraints are the
authoritative validators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProjectStatus = Literal["active", "paused", "archived"]
TodoStatus = Literal["open", "in_progress", "blocked", "done", "archived"]
TodoPriority = Literal["low", "medium", "high", "critical"]

PROJECT_STATUSES: tuple[ProjectStatus, ...] = ("active", "paused", "archived")
TODO_STATUSES: tuple[TodoStatus, ...] = (
    "open",
    "in_progress",
    "blocked",
    "done",
    "archived",
)
TODO_PRIORITIES: tuple[TodoPriority, ...] = ("low", "medium", "high", "critical")


@dataclass
class Project:
    id: int
    name: str
    description: str | None
    status: ProjectStatus
    tags: list[str]
    created_at: str
    updated_at: str
    created_by: str


@dataclass
class Todo:
    id: int
    project_id: int
    parent_todo_id: int | None
    title: str
    description: str | None
    status: TodoStatus
    priority: TodoPriority
    created_at: str
    updated_at: str
    completed_at: str | None
    created_by: str


@dataclass
class Note:
    id: int
    todo_id: int
    content: str
    author: str
    created_at: str


@dataclass
class SearchResults:
    """Container returned by ``repository.search``.

    Each list contains entities whose indexed fields matched the query, ordered
    by FTS5 rank (best match first).
    """

    projects: list[Project] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
