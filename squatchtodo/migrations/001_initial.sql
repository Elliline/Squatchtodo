-- SquatchTodo initial schema.
--
-- Notes:
--  * No hard deletes — items get archived. Foreign keys use ON DELETE RESTRICT.
--  * `tags` columns store JSON arrays of strings. SQLite has no JSON column type,
--    so we store TEXT and rely on json_each() at query time.
--  * `updated_at` is maintained via triggers since SQLite lacks ON UPDATE.
--  * FTS5 mirrors projects/todos/notes for the search MCP tool. Triggers keep
--    the FTS tables in sync with the source tables.

CREATE TABLE projects (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused', 'archived')),
    tags         TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by   TEXT NOT NULL
);

CREATE INDEX idx_projects_status ON projects(status);

CREATE TABLE todos (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    parent_todo_id  INTEGER REFERENCES todos(id) ON DELETE RESTRICT,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'in_progress', 'blocked', 'done', 'archived')),
    priority        TEXT NOT NULL DEFAULT 'medium'
                     CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at    TEXT,
    created_by      TEXT NOT NULL
);

CREATE INDEX idx_todos_project        ON todos(project_id);
CREATE INDEX idx_todos_parent         ON todos(parent_todo_id);
CREATE INDEX idx_todos_status         ON todos(status);
CREATE INDEX idx_todos_project_status ON todos(project_id, status);

CREATE TABLE notes (
    id          INTEGER PRIMARY KEY,
    todo_id     INTEGER NOT NULL REFERENCES todos(id) ON DELETE RESTRICT,
    content     TEXT NOT NULL,
    author      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_notes_todo ON notes(todo_id);

-- updated_at triggers
CREATE TRIGGER trg_projects_updated_at
AFTER UPDATE ON projects
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE projects
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

CREATE TRIGGER trg_todos_updated_at
AFTER UPDATE ON todos
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE todos
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE id = NEW.id;
END;

-- Full-text search ----------------------------------------------------------
-- External-content FTS5 tables mirror the source rows. We use unicode61 with
-- diacritic stripping so "résumé" matches "resume". Triggers keep the FTS
-- index synchronized.

CREATE VIRTUAL TABLE projects_fts USING fts5(
    name, description, tags,
    content='projects',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER trg_projects_fts_ai AFTER INSERT ON projects BEGIN
    INSERT INTO projects_fts(rowid, name, description, tags)
    VALUES (NEW.id, NEW.name, COALESCE(NEW.description, ''), NEW.tags);
END;

CREATE TRIGGER trg_projects_fts_ad AFTER DELETE ON projects BEGIN
    INSERT INTO projects_fts(projects_fts, rowid, name, description, tags)
    VALUES ('delete', OLD.id, OLD.name, COALESCE(OLD.description, ''), OLD.tags);
END;

CREATE TRIGGER trg_projects_fts_au AFTER UPDATE ON projects BEGIN
    INSERT INTO projects_fts(projects_fts, rowid, name, description, tags)
    VALUES ('delete', OLD.id, OLD.name, COALESCE(OLD.description, ''), OLD.tags);
    INSERT INTO projects_fts(rowid, name, description, tags)
    VALUES (NEW.id, NEW.name, COALESCE(NEW.description, ''), NEW.tags);
END;

CREATE VIRTUAL TABLE todos_fts USING fts5(
    title, description,
    content='todos',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER trg_todos_fts_ai AFTER INSERT ON todos BEGIN
    INSERT INTO todos_fts(rowid, title, description)
    VALUES (NEW.id, NEW.title, COALESCE(NEW.description, ''));
END;

CREATE TRIGGER trg_todos_fts_ad AFTER DELETE ON todos BEGIN
    INSERT INTO todos_fts(todos_fts, rowid, title, description)
    VALUES ('delete', OLD.id, OLD.title, COALESCE(OLD.description, ''));
END;

CREATE TRIGGER trg_todos_fts_au AFTER UPDATE ON todos BEGIN
    INSERT INTO todos_fts(todos_fts, rowid, title, description)
    VALUES ('delete', OLD.id, OLD.title, COALESCE(OLD.description, ''));
    INSERT INTO todos_fts(rowid, title, description)
    VALUES (NEW.id, NEW.title, COALESCE(NEW.description, ''));
END;

CREATE VIRTUAL TABLE notes_fts USING fts5(
    content,
    content='notes',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER trg_notes_fts_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, content) VALUES (NEW.id, NEW.content);
END;

CREATE TRIGGER trg_notes_fts_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content) VALUES ('delete', OLD.id, OLD.content);
END;

CREATE TRIGGER trg_notes_fts_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content) VALUES ('delete', OLD.id, OLD.content);
    INSERT INTO notes_fts(rowid, content) VALUES (NEW.id, NEW.content);
END;
