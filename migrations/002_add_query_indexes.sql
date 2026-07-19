CREATE INDEX IF NOT EXISTS idx_donors_email
    ON donors(email);

CREATE INDEX IF NOT EXISTS idx_donors_name_email
    ON donors(name COLLATE NOCASE, email);

CREATE INDEX IF NOT EXISTS idx_tasks_created_at_status
    ON tasks(created_at DESC, status);

CREATE INDEX IF NOT EXISTS idx_tasks_status_created_at
    ON tasks(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_connector_result_cache_status_fetched_at
    ON connector_result_cache(source_status, fetched_at DESC);
