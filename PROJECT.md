# SquatchTodo

A self-hosted, MCP-accessible todo and project tracking system designed to share state between Ellie, Claude (API and claude.ai), and the SNH instances running across the aqueduct fleet.

## Purpose

SquatchTodo is shared working memory for what's being worked on. SNH clusters store *what happened*; SquatchTodo stores *what we want to happen*. Any AI in the ecosystem — Halo's SNH, Tabby's SNH, Sparky's coder, Claude API sessions, claude.ai conversations — can read and write to the same todo state, so context doesn't fragment between sessions.

## Core Principles

- **Local-first.** Runs on Halo, accessible only on the local network in v1. Cloudflare tunnel and token auth come later.
- **MCP-native.** Primary access is via MCP tools so any AI client can use it.
- **Web UI for the human.** Ellie can see the full state, add items while thinking, check things off.
- **No hard deletes.** Items get archived, never removed. Audit trail is preserved.
- **AI-flagged items.** Items created by an AI are visually distinct from items Ellie created.
- **Append-only notes.** Each todo has a running log of what's been tried/learned, with timestamps and authorship.

## Stack

- **Backend:** Python 3.12, FastAPI
- **Database:** SQLite (single file, easy backup, fits the existing SNH/SCC pattern)
- **MCP:** Python MCP SDK, exposed over HTTP transport
- **Frontend:** HTML + HTMX (server-rendered, no build step, matches the "minimal but functional" aesthetic of SCC)
- **Service:** systemd unit on Halo, port 3100 (suggested — adjust if conflicts)

## Data Model

### Projects
Top-level containers for related work.

```
id              INTEGER PRIMARY KEY
name            TEXT NOT NULL
description     TEXT
status          TEXT (active | paused | archived)  default 'active'
tags            TEXT (JSON array of strings)
created_at      TIMESTAMP
updated_at      TIMESTAMP
created_by      TEXT (ellie | halo | tabby | sparky | claude_opus_4_7 | etc.)
```

### Todos
Work items within a project. Self-referencing for nested subtodos.

```
id              INTEGER PRIMARY KEY
project_id      INTEGER NOT NULL  (FK projects)
parent_todo_id  INTEGER NULL      (FK todos — for nesting)
title           TEXT NOT NULL
description     TEXT
status          TEXT (open | in_progress | blocked | done | archived)  default 'open'
priority        TEXT (low | medium | high | critical)  default 'medium'
created_at      TIMESTAMP
updated_at      TIMESTAMP
completed_at    TIMESTAMP NULL
created_by      TEXT
```

### Notes
Append-only running log per todo.

```
id              INTEGER PRIMARY KEY
todo_id         INTEGER NOT NULL  (FK todos)
content         TEXT NOT NULL
author          TEXT NOT NULL
created_at      TIMESTAMP
```

## MCP Tools

All tools return JSON. No tool deletes data — archive instead.

### Project tools
- `list_projects(status?, tag?)` — list projects, optionally filtered
- `get_project(project_id, include_todos?)` — full project detail, optionally with todo tree
- `create_project(name, description?, tags?)` — new project
- `update_project(project_id, name?, description?, status?, tags?)` — modify

### Todo tools
- `list_todos(project_id, parent_todo_id?, status?)` — todos at a level
- `get_todo(todo_id, include_subtodos?, include_notes?)` — full detail
- `create_todo(project_id, title, parent_todo_id?, description?, priority?)` — new todo
- `update_todo(todo_id, title?, description?, status?, priority?)` — modify
- `complete_todo(todo_id)` — convenience: sets status=done and completed_at=now

### Note tools
- `add_note(todo_id, content)` — append to running log
- `list_notes(todo_id)` — get full log for a todo

### Search
- `search(query, scope?)` — full-text across projects/todos/notes. Scope can be 'projects', 'todos', 'notes', or 'all'.

The `created_by` field is set automatically by the server based on the authenticated client identity, not passed in by the caller.

## Web UI (v1)

Minimal, functional, no JavaScript framework. HTMX for interactivity.

### Pages
- `/` — Project list. Shows active projects with todo counts and recent activity.
- `/projects/{id}` — Project detail. Shows nested todo tree, add forms, recent notes.
- `/todos/{id}` — Todo detail. Title, description, notes log, subtodos, edit forms.
- `/search` — Full-text search results.

### Interactions
- Click a checkbox to mark a todo done (HTMX PATCH)
- Inline edit titles and descriptions
- Add new todos and subtodos directly on the project page
- Add notes inline on todo detail page
- Filter by status and priority
- Visual flag on AI-created items (small bot icon or different color tag)

### Aesthetic
Match the dark, functional aesthetic of MettaSphere/SCC. No pastels, no rounded gradients. Dense information layout — Ellie wants to see the whole picture at once.

## Authentication (v1)

- Local network only (bind to internal interface)
- No auth required for v1 since it's local-only
- Schema includes `created_by` field already so adding token auth later is straightforward
- v2: token-based auth, Cloudflare Tunnel exposure

## Deployment

### Service
- systemd unit: `squatchtodo.service`
- User: dedicated `squatchtodo` system user
- Working dir: `/opt/squatchtodo`
- Logs: journalctl

### Database location
- `/var/lib/squatchtodo/squatchtodo.db`
- Btrfs subvolume so snapshots are independent

### Backups
- Hourly: `sqlite3 .backup` to local snapshot directory
- Daily: rsync snapshot to NAS
- Weekly: rsync to Datto

### Configuration
- `/etc/squatchtodo/config.toml`
- Settings: bind address, port, database path, log level, MCP enabled (yes/no)

## SNH Integration

Each SNH instance gets a config option pointing to the SquatchTodo MCP endpoint. When SNH starts, it connects and registers its identity (halo / tabby / sparky / etc.) so `created_by` fields work.

Config example for SNH:
```toml
[tools.squatchtodo]
enabled = true
url = "http://halo.local:3100/mcp"
identity = "halo"
```

## Bootstrap Data

After deployment, seed with current in-flight projects:

- **SNH** (active) — heartbeat memory persistence, memory archive system, alerts tab, gaming cluster cleanup, uncertain fact extraction notifications, JSON parsing fixes
- **Aqueduct Fleet** (active) — model selection per node, Halo wipe-and-rebuild, Tabby SquatchOS conversion, dev SNH setup
- **SquatchOS** (active) — Fedora Server kickstart, SNH appliance install procedure, Btrfs subvolume layout
- **SCC** (active) — services-not-appearing bug, SNH-stopped-when-running display bug, inter-agent communication, testing agent pipeline step
- **NAS Consolidation** (active) — UGREEN DXP6800 Pro setup, switch 10GbE evaluation, offsite backup strategy
- **Client: Coyote Rock** (active) — camera install scheduling, UISP ladder work
- **Client: LCAC** (active) — Chckvet presentation to Shanna
- **Client: ISH** (active) — UDM Pro Max upgrade decision, Fathoms POS go-live, Blue Iris/UniFi Protect integration
- **AGI Architecture** (active) — per-node SNH design, inter-SNH messaging protocol, shared factual layer spec, Ship of Theseus migration policy
- **Coastal Squatch AI** (active) — Claude API benchmark experiment, English-native model evaluation, MSP product strategy
- **IT Glue Replacement** (planned) — own client documentation system, future project

## Out of Scope (v1)

- Multi-user support (Ellie is the only human user for now)
- External notifications (email, Slack, etc.)
- Time tracking
- Recurring tasks
- File attachments
- Cloudflare exposure (v2)
- Inter-SNH messaging integration (v2)

## Build Order

1. Database schema + migrations
2. Core data model classes (projects, todos, notes)
3. FastAPI REST endpoints
4. MCP server layer wrapping the REST endpoints
5. Web UI (project list, project detail, todo detail, search)
6. systemd service definition
7. Backup scripts
8. Bootstrap data load script
9. SNH client config example
