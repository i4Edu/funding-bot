from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
except ImportError:  # pragma: no cover - dependency is required in production
    OTLPSpanExporter = None

_TRACE_LOCK = threading.Lock()
_TRACE_CONFIGURED = False
_TRACE_EXPORTER_KIND = "none"
_TRACE_EXPORTER_TARGET = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None = None) -> str:
    normalized = value or _utcnow()
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def _resolve_trace_exporter() -> tuple[str, str]:
    explicit = str(os.environ.get("FUNDING_BOT_TRACE_EXPORTER", "")).strip().lower()
    endpoint = str(
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    ).strip()
    if explicit in {"none", "disabled", "off"}:
        return "none", ""
    if explicit == "console":
        return "console", "stdout"
    if explicit == "otlp":
        return "otlp", endpoint
    if endpoint:
        return "otlp", endpoint
    return "none", ""


def configure_tracing() -> None:
    global _TRACE_CONFIGURED, _TRACE_EXPORTER_KIND, _TRACE_EXPORTER_TARGET
    if _TRACE_CONFIGURED:
        return
    with _TRACE_LOCK:
        if _TRACE_CONFIGURED:
            return
        exporter_kind, exporter_target = _resolve_trace_exporter()
        provider = trace.get_tracer_provider()
        if provider.__class__.__name__ == "ProxyTracerProvider":
            resource = Resource.create(
                {
                    "service.name": os.environ.get("OTEL_SERVICE_NAME", "funding-bot"),
                    "service.namespace": os.environ.get("OTEL_SERVICE_NAMESPACE", "funding-bot"),
                    "deployment.environment": os.environ.get(
                        "OTEL_DEPLOYMENT_ENVIRONMENT",
                        os.environ.get("ENVIRONMENT", "development"),
                    ),
                }
            )
            sdk_provider = TracerProvider(resource=resource)
            if exporter_kind == "console":
                sdk_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            elif exporter_kind == "otlp" and exporter_target and OTLPSpanExporter is not None:
                exporter = OTLPSpanExporter(
                    endpoint=exporter_target,
                    headers=dict(
                        _parse_header_pairs(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
                    ),
                    timeout=float(os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "10")),
                )
                sdk_provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(sdk_provider)
        _TRACE_EXPORTER_KIND = exporter_kind
        _TRACE_EXPORTER_TARGET = exporter_target
        _TRACE_CONFIGURED = True


def _parse_header_pairs(raw_headers: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in raw_headers.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            pairs.append((key, value))
    return pairs


def get_tracer(name: str):
    configure_tracing()
    return trace.get_tracer(name)


def extract_context(carrier: dict[str, str] | None = None) -> Any:
    configure_tracing()
    return propagate.extract(carrier or {})


def inject_context(carrier: dict[str, str], *, context: Any | None = None) -> dict[str, str]:
    configure_tracing()
    propagate.inject(carrier, context=context)
    return carrier


def capture_current_context() -> dict[str, str]:
    return inject_context({})


def current_trace_id() -> str | None:
    configure_tracing()
    span = trace.get_current_span()
    if span is None:
        return None
    span_context = span.get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}"


@contextmanager
def start_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
    carrier: dict[str, str] | None = None,
) -> Iterator[Any]:
    tracer = get_tracer("funding_bot.observability")
    context = extract_context(carrier) if carrier is not None else None
    with tracer.start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=attributes,
    ) as span:
        yield span


def set_span_error(span: Any, exc: Exception) -> None:
    if span is None:
        return
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


def tracing_configuration_summary() -> dict[str, Any]:
    configure_tracing()
    return {
        "enabled": _TRACE_EXPORTER_KIND != "none",
        "exporter": _TRACE_EXPORTER_KIND,
        "target": _TRACE_EXPORTER_TARGET,
    }


@dataclass(frozen=True)
class SLODefinition:
    name: str
    label: str
    description: str
    latency_target_seconds: float
    max_error_rate: float
    min_throughput_per_hour: float | None = None
    window_hours: int = 24


SLO_DEFINITIONS: tuple[SLODefinition, ...] = (
    SLODefinition(
        name="connector_latency",
        label="Connector latency",
        description="Connector requests should stay responsive while keeping degraded responses rare.",
        latency_target_seconds=2.0,
        max_error_rate=0.05,
    ),
    SLODefinition(
        name="task_queue_throughput",
        label="Task queue throughput",
        description="Background jobs should complete fast enough to sustain normal operations.",
        latency_target_seconds=60.0,
        max_error_rate=0.02,
        min_throughput_per_hour=5.0,
    ),
    SLODefinition(
        name="dashboard_response_time",
        label="Dashboard response time",
        description="Dashboard pages should remain fast for authenticated operators.",
        latency_target_seconds=0.75,
        max_error_rate=0.01,
        min_throughput_per_hour=5.0,
    ),
)

_SLO_DEFINITION_MAP = {definition.name: definition for definition in SLO_DEFINITIONS}


def _resolve_observability_db_path(db_path: str | None = None) -> str | None:
    resolved = (
        db_path
        or os.environ.get("FUNDING_BOT_OBSERVABILITY_DB_PATH")
        or os.environ.get("BOT_DB_PATH")
    )
    if not resolved or str(resolved).strip() == ":memory:":
        return None
    return str(resolved)


def ensure_slo_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS slo_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slo_name TEXT NOT NULL,
            component TEXT NOT NULL,
            latency_seconds REAL NOT NULL,
            success INTEGER NOT NULL,
            throughput_units REAL NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL
        )
        """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_slo_events_name_recorded_at
            ON slo_events(slo_name, recorded_at DESC)
        """)


def record_slo_event(
    slo_name: str,
    *,
    component: str,
    latency_seconds: float,
    success: bool,
    throughput_units: float = 1.0,
    metadata: dict[str, Any] | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    recorded_at: datetime | None = None,
) -> None:
    if slo_name not in _SLO_DEFINITION_MAP:
        raise ValueError(f"Unknown SLO definition: {slo_name}")
    row = (
        slo_name,
        component,
        max(0.0, float(latency_seconds)),
        1 if success else 0,
        max(0.0, float(throughput_units)),
        json.dumps(metadata or {}, sort_keys=True),
        _to_iso(recorded_at),
    )
    if connection is not None:
        ensure_slo_schema(connection)
        connection.execute(
            """
            INSERT INTO slo_events (
                slo_name, component, latency_seconds, success,
                throughput_units, metadata_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        return
    resolved_db_path = _resolve_observability_db_path(db_path)
    if resolved_db_path is None:
        return
    try:
        standalone = sqlite3.connect(resolved_db_path, timeout=2.0)
        try:
            ensure_slo_schema(standalone)
            standalone.execute(
                """
                INSERT INTO slo_events (
                    slo_name, component, latency_seconds, success,
                    throughput_units, metadata_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            standalone.commit()
        finally:
            standalone.close()
    except sqlite3.Error:
        return


def reset_slo_events(
    *, connection: sqlite3.Connection | None = None, db_path: str | None = None
) -> None:
    if connection is not None:
        ensure_slo_schema(connection)
        connection.execute("DELETE FROM slo_events")
        return
    resolved_db_path = _resolve_observability_db_path(db_path)
    if resolved_db_path is None or not os.path.exists(resolved_db_path):
        return
    standalone = sqlite3.connect(resolved_db_path, timeout=2.0)
    try:
        ensure_slo_schema(standalone)
        standalone.execute("DELETE FROM slo_events")
        standalone.commit()
    finally:
        standalone.close()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summarize_rows(definition: SLODefinition, rows: list[sqlite3.Row]) -> dict[str, Any]:
    latencies = [float(row["latency_seconds"]) for row in rows]
    total = len(rows)
    failures = sum(1 for row in rows if not bool(row["success"]))
    successful_units = sum(float(row["throughput_units"]) for row in rows if bool(row["success"]))
    error_rate = failures / total if total else 0.0
    throughput_per_hour = successful_units / float(definition.window_hours or 1)
    latency_p95 = _percentile(latencies, 0.95)
    latency_p50 = _percentile(latencies, 0.50)
    latency_compliance = (
        sum(1 for latency in latencies if latency <= definition.latency_target_seconds) / total
        if total
        else 0.0
    )
    components: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = components.setdefault(
            str(row["component"]),
            {
                "component": str(row["component"]),
                "samples": 0,
                "failures": 0,
                "latency_seconds_sum": 0.0,
            },
        )
        bucket["samples"] += 1
        bucket["failures"] += 0 if bool(row["success"]) else 1
        bucket["latency_seconds_sum"] += float(row["latency_seconds"])
    component_rows = []
    for bucket in sorted(components.values(), key=lambda item: item["component"]):
        samples = int(bucket["samples"])
        failures = int(bucket["failures"])
        component_rows.append(
            {
                "component": bucket["component"],
                "samples": samples,
                "error_rate": failures / samples if samples else 0.0,
                "average_latency_seconds": (
                    bucket["latency_seconds_sum"] / samples if samples else 0.0
                ),
            }
        )
    latency_met = latency_p95 <= definition.latency_target_seconds if total else False
    error_rate_met = error_rate <= definition.max_error_rate if total else False
    throughput_met = (
        throughput_per_hour >= definition.min_throughput_per_hour
        if definition.min_throughput_per_hour is not None
        else True
    )
    compliant = total > 0 and latency_met and error_rate_met and throughput_met
    return {
        "name": definition.name,
        "label": definition.label,
        "description": definition.description,
        "window_hours": definition.window_hours,
        "samples": total,
        "latency_target_seconds": definition.latency_target_seconds,
        "latency_p50_seconds": latency_p50,
        "latency_p95_seconds": latency_p95,
        "latency_compliance": latency_compliance,
        "max_error_rate": definition.max_error_rate,
        "error_rate": error_rate,
        "success_rate": 1.0 - error_rate if total else 0.0,
        "min_throughput_per_hour": definition.min_throughput_per_hour,
        "throughput_per_hour": throughput_per_hour,
        "latency_met": latency_met,
        "error_rate_met": error_rate_met,
        "throughput_met": throughput_met,
        "compliant": compliant,
        "components": component_rows[:5],
    }


def summarize_slos(
    *,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    own_connection = False
    if connection is None:
        resolved_db_path = _resolve_observability_db_path(db_path)
        if resolved_db_path is None or not os.path.exists(resolved_db_path):
            return [_summarize_rows(definition, []) for definition in SLO_DEFINITIONS]
        connection = sqlite3.connect(resolved_db_path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        own_connection = True
    try:
        ensure_slo_schema(connection)
        summaries: list[dict[str, Any]] = []
        for definition in SLO_DEFINITIONS:
            cutoff = _to_iso(_utcnow() - timedelta(hours=definition.window_hours))
            rows = connection.execute(
                """
                SELECT component, latency_seconds, success, throughput_units
                FROM slo_events
                WHERE slo_name = ? AND recorded_at >= ?
                ORDER BY recorded_at DESC
                """,
                (definition.name, cutoff),
            ).fetchall()
            summaries.append(_summarize_rows(definition, rows))
        return summaries
    finally:
        if own_connection and connection is not None:
            connection.close()


def render_slo_prometheus(
    *,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> list[str]:
    lines = [
        "# HELP funding_bot_slo_latency_p95_seconds Rolling p95 latency for each service-level objective",
        "# TYPE funding_bot_slo_latency_p95_seconds gauge",
        "# HELP funding_bot_slo_error_rate Rolling error rate for each service-level objective",
        "# TYPE funding_bot_slo_error_rate gauge",
        "# HELP funding_bot_slo_success_rate Rolling success rate for each service-level objective",
        "# TYPE funding_bot_slo_success_rate gauge",
        "# HELP funding_bot_slo_throughput_per_hour Rolling successful throughput per hour",
        "# TYPE funding_bot_slo_throughput_per_hour gauge",
        "# HELP funding_bot_slo_compliance Whether the service-level objective is currently meeting all targets",
        "# TYPE funding_bot_slo_compliance gauge",
        "# HELP funding_bot_slo_samples_total Number of recent observations for the service-level objective",
        "# TYPE funding_bot_slo_samples_total gauge",
        "# HELP funding_bot_slo_latency_target_seconds Configured latency target for the service-level objective",
        "# TYPE funding_bot_slo_latency_target_seconds gauge",
        "# HELP funding_bot_slo_error_rate_target Configured error-rate target for the service-level objective",
        "# TYPE funding_bot_slo_error_rate_target gauge",
    ]
    for summary in summarize_slos(connection=connection, db_path=db_path):
        labels = f'operation="{summary["name"]}"'
        lines.extend(
            [
                f'funding_bot_slo_latency_p95_seconds{{{labels}}} {summary["latency_p95_seconds"]:.6f}',
                f'funding_bot_slo_error_rate{{{labels}}} {summary["error_rate"]:.6f}',
                f'funding_bot_slo_success_rate{{{labels}}} {summary["success_rate"]:.6f}',
                f'funding_bot_slo_throughput_per_hour{{{labels}}} {summary["throughput_per_hour"]:.6f}',
                f'funding_bot_slo_compliance{{{labels}}} {1 if summary["compliant"] else 0}',
                f'funding_bot_slo_samples_total{{{labels}}} {summary["samples"]}',
                f'funding_bot_slo_latency_target_seconds{{{labels}}} {summary["latency_target_seconds"]:.6f}',
                f'funding_bot_slo_error_rate_target{{{labels}}} {summary["max_error_rate"]:.6f}',
            ]
        )
        if summary["min_throughput_per_hour"] is not None:
            if not any(
                line.startswith("# HELP funding_bot_slo_throughput_target_per_hour")
                for line in lines
            ):
                lines.extend(
                    [
                        "# HELP funding_bot_slo_throughput_target_per_hour Configured throughput target per hour",
                        "# TYPE funding_bot_slo_throughput_target_per_hour gauge",
                    ]
                )
            lines.append(
                f'funding_bot_slo_throughput_target_per_hour{{{labels}}} {summary["min_throughput_per_hour"]:.6f}'
            )
    return lines
