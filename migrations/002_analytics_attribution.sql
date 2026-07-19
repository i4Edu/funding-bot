ALTER TABLE tasks ADD COLUMN attributed_connector TEXT;
ALTER TABLE tasks ADD COLUMN opportunity_signature TEXT;

ALTER TABLE communications ADD COLUMN attributed_connector TEXT;
ALTER TABLE communications ADD COLUMN related_opportunity_signature TEXT;
ALTER TABLE communications ADD COLUMN related_task_id INTEGER;

ALTER TABLE applications ADD COLUMN attributed_connector TEXT;

CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    connector_name TEXT,
    opportunity_signature TEXT,
    task_id INTEGER,
    communication_id INTEGER,
    event_type TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    happened_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_funnel_events_stage_happened_at
    ON funnel_events(stage, happened_at);

CREATE INDEX IF NOT EXISTS idx_funnel_events_connector_stage
    ON funnel_events(connector_name, stage);

CREATE TABLE IF NOT EXISTS connector_call_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connector_name TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    operation TEXT NOT NULL,
    source_status TEXT NOT NULL DEFAULT 'remote',
    latency_seconds REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    errored INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    happened_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_connector_call_metrics_happened_at
    ON connector_call_metrics(happened_at);

CREATE INDEX IF NOT EXISTS idx_connector_call_metrics_connector_happened_at
    ON connector_call_metrics(connector_name, happened_at);
