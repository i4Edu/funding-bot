CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee TEXT NOT NULL,
    status TEXT NOT NULL,
    due_date TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    assignee_email TEXT,
    assignee_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee
    ON tasks(assignee);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status);

CREATE INDEX IF NOT EXISTS idx_tasks_external_id
    ON tasks(external_id);

CREATE INDEX IF NOT EXISTS idx_tasks_due_date
    ON tasks(due_date);
