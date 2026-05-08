"""Seed the SquatchTodo database with the bootstrap projects from PROJECT.md.

Idempotent: safe to re-run. Existing projects (by name) are skipped, as are
existing todos within each project (matched by exact title). Re-running after
PROJECT.md is updated with new items is therefore the workflow for keeping
the seed in sync with the spec.

Usage:
    squatchtodo-bootstrap                           # uses /etc/squatchtodo/config.toml
    squatchtodo-bootstrap --db /tmp/squatch.db      # explicit DB
    squatchtodo-bootstrap --identity halo           # attribute creates to halo
    squatchtodo-bootstrap --dry-run                 # preview only
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional, TypedDict

# When invoked via ``python scripts/bootstrap.py`` the package isn't on
# sys.path. ``squatchtodo-bootstrap`` (entry point) doesn't need this.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from squatchtodo import db, repository
from squatchtodo.config import load_config

log = logging.getLogger("squatchtodo.bootstrap")


class _ProjectSeed(TypedDict, total=False):
    name: str
    description: str
    status: str
    tags: list[str]
    todos: list[str]


# Source of truth: PROJECT.md "Bootstrap Data" section. When that file
# changes, edit this list to match. Items listed per project become top-level
# todos (priority defaults to medium).
BOOTSTRAP_PROJECTS: list[_ProjectSeed] = [
    {
        "name": "SNH",
        "description": "Squatch Node Heartbeat — per-node AGI memory and observability layer.",
        "tags": ["fleet", "ai"],
        "todos": [
            "Heartbeat memory persistence",
            "Memory archive system",
            "Alerts tab",
            "Gaming cluster cleanup",
            "Uncertain fact extraction notifications",
            "JSON parsing fixes",
        ],
    },
    {
        "name": "Aqueduct Fleet",
        "description": "Cross-fleet AI orchestration and node provisioning.",
        "tags": ["fleet", "ops"],
        "todos": [
            "Model selection per node",
            "Halo wipe-and-rebuild",
            "Tabby SquatchOS conversion",
            "Dev SNH setup",
        ],
    },
    {
        "name": "SquatchOS",
        "description": "Fedora-based appliance OS for the SNH fleet.",
        "tags": ["fleet", "infra", "os"],
        "todos": [
            "Fedora Server kickstart",
            "SNH appliance install procedure",
            "Btrfs subvolume layout",
        ],
    },
    {
        "name": "SCC",
        "description": "Squatch Command Center — fleet-wide control plane.",
        "tags": ["internal", "ai"],
        "todos": [
            "Services-not-appearing bug",
            "SNH-stopped-when-running display bug",
            "Inter-agent communication",
            "Testing agent pipeline step",
        ],
    },
    {
        "name": "NAS Consolidation",
        "description": "Storage consolidation onto UGREEN DXP6800 Pro plus offsite strategy.",
        "tags": ["infra", "storage"],
        "todos": [
            "UGREEN DXP6800 Pro setup",
            "Switch 10GbE evaluation",
            "Offsite backup strategy",
        ],
    },
    {
        "name": "Client: Coyote Rock",
        "description": "Client engagement — Coyote Rock.",
        "tags": ["client"],
        "todos": [
            "Camera install scheduling",
            "UISP ladder work",
        ],
    },
    {
        "name": "Client: LCAC",
        "description": "Client engagement — LCAC.",
        "tags": ["client"],
        "todos": [
            "Chckvet presentation to Shanna",
        ],
    },
    {
        "name": "Client: ISH",
        "description": "Client engagement — ISH.",
        "tags": ["client"],
        "todos": [
            "UDM Pro Max upgrade decision",
            "Fathoms POS go-live",
            "Blue Iris / UniFi Protect integration",
        ],
    },
    {
        "name": "AGI Architecture",
        "description": "Cross-cutting design work for the AGI / fleet AI substrate.",
        "tags": ["ai", "design"],
        "todos": [
            "Per-node SNH design",
            "Inter-SNH messaging protocol",
            "Shared factual layer spec",
            "Ship of Theseus migration policy",
        ],
    },
    {
        "name": "Coastal Squatch AI",
        "description": "AI product line and benchmark / strategy work for the MSP.",
        "tags": ["business", "ai"],
        "todos": [
            "Claude API benchmark experiment",
            "English-native model evaluation",
            "MSP product strategy",
        ],
    },
    {
        # PROJECT.md flags this as "(planned)". We carry that as a tag rather
        # than a status because the schema only has active|paused|archived.
        "name": "IT Glue Replacement",
        "description": "Build our own client documentation system to replace IT Glue.",
        "tags": ["internal", "planned"],
        "todos": [
            "Define scope for own client documentation system",
        ],
    },
]


# -- core seeding logic -----------------------------------------------------


def seed(
    conn: sqlite3.Connection,
    *,
    identity: str,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Apply the bootstrap data. Returns ``(projects_created, todos_created)``."""
    existing_projects = {p.name: p for p in repository.list_projects(conn)}
    projects_created = 0
    todos_created = 0

    for seed in BOOTSTRAP_PROJECTS:
        name = seed["name"]
        if name in existing_projects:
            project = existing_projects[name]
            log.info("[=] project exists: %s (id=%d) — leaving alone", name, project.id)
        else:
            if dry_run:
                log.info("[+] would create project: %s", name)
                continue
            project = repository.create_project(
                conn,
                name=name,
                description=seed.get("description"),
                tags=seed.get("tags", []),
                created_by=identity,
            )
            projects_created += 1
            log.info(
                "[+] created project: %s (id=%d) tags=%s",
                project.name,
                project.id,
                project.tags,
            )

        # Index existing todo titles in this project so we don't double-add.
        # Title match is exact — case and whitespace sensitive — to avoid
        # collapsing things like "Setup" vs "Setup notes".
        existing_titles = {
            t.title for t in repository.list_todos(conn, project_id=project.id)
        }
        for todo_title in seed.get("todos", []):
            if todo_title in existing_titles:
                log.info(
                    "[=] todo exists: %s / %s — leaving alone",
                    project.name,
                    todo_title,
                )
                continue
            if dry_run:
                log.info("[+] would create todo: %s / %s", project.name, todo_title)
                continue
            todo = repository.create_todo(
                conn,
                project_id=project.id,
                title=todo_title,
                created_by=identity,
            )
            todos_created += 1
            log.info("[+] created todo: %s / %s (id=%d)", project.name, todo.title, todo.id)

    return projects_created, todos_created


# -- CLI --------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="squatchtodo-bootstrap",
        description="Seed initial projects and todos from PROJECT.md.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.toml (default resolves /etc/squatchtodo/config.toml).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="Path to the SQLite DB. Overrides the config file.",
    )
    parser.add_argument(
        "--identity",
        default=None,
        help="Actor name for created_by (default: config.identity.default, usually 'ellie').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without writing.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Only print the final summary."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    config = load_config(args.config)
    db_path = Path(args.db) if args.db else config.database.path
    identity = args.identity or config.identity.default

    if not db_path.parent.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure schema is in place — handy for fresh boxes where bootstrap is
    # run before the main service has had a chance to start.
    db.run_migrations(db_path)
    conn = db.connect(db_path)
    try:
        projects_created, todos_created = seed(
            conn, identity=identity, dry_run=args.dry_run
        )
    finally:
        conn.close()

    summary = (
        f"{'dry-run: would create' if args.dry_run else 'created'} "
        f"{projects_created} project(s), {todos_created} todo(s) "
        f"(identity={identity}, db={db_path})"
    )
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
