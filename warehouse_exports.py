from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

try:  # pragma: no cover - optional dependency in minimal environments
    import boto3
except ImportError:  # pragma: no cover - exercised when boto3 is unavailable
    boto3 = None

try:  # pragma: no cover - optional dependency in minimal environments
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - exercised when pyarrow is unavailable
    pa = None
    pq = None

if TYPE_CHECKING:
    from funding_bot import FundingBot


SUPPORTED_EXPORT_FORMATS = frozenset({"csv", "json", "parquet"})
SUPPORTED_EXPORT_DATASETS = (
    "applications",
    "donors",
    "matches",
    "opportunities",
    "results",
    "tasks",
)
DEFAULT_EXPORT_DATASETS = ("donors", "tasks", "matches", "results")
DEFAULT_EXPORT_FORMAT = "json"
DEFAULT_EXPORT_OUTPUT_DIR = Path("generated/exports")
DEFAULT_ARCHIVE_DIR = Path("generated/archives")
DATASET_ALIASES = {
    "applications": "results",
    "opportunities": "matches",
}


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def canonical_dataset_name(dataset: str) -> str:
    normalized = str(dataset).strip().lower()
    if normalized in DATASET_ALIASES:
        normalized = DATASET_ALIASES[normalized]
    if normalized not in SUPPORTED_EXPORT_DATASETS:
        raise ValueError(
            f"Unsupported export dataset {dataset!r}. "
            f"Expected one of {list(SUPPORTED_EXPORT_DATASETS)}."
        )
    return normalized


def normalize_datasets(datasets: Iterable[str] | None) -> list[str]:
    values = list(datasets or DEFAULT_EXPORT_DATASETS)
    normalized: list[str] = []
    for dataset in values:
        current = canonical_dataset_name(dataset)
        if current not in normalized:
            normalized.append(current)
    return normalized


def normalize_export_format(export_format: str | None) -> str:
    normalized = str(export_format or DEFAULT_EXPORT_FORMAT).strip().lower()
    if normalized not in SUPPORTED_EXPORT_FORMATS:
        raise ValueError(
            f"Unsupported export format {export_format!r}. "
            f"Expected one of {sorted(SUPPORTED_EXPORT_FORMATS)}."
        )
    return normalized


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            flattened[key] = json.dumps(value, sort_keys=True)
        elif value is None or isinstance(value, (str, int, float, bool)):
            flattened[key] = value
        else:
            flattened[key] = str(value)
    return flattened


class ArchiveManager:
    def __init__(
        self,
        *,
        cold_storage_dir: str | Path | None = DEFAULT_ARCHIVE_DIR,
        s3_bucket: str | None = None,
        s3_prefix: str = "funding-bot",
        s3_client: Any | None = None,
    ) -> None:
        self.cold_storage_dir = Path(cold_storage_dir) if cold_storage_dir else None
        self.s3_bucket = str(s3_bucket).strip() if s3_bucket else None
        self.s3_prefix = str(s3_prefix).strip().strip("/")
        self._s3_client = s3_client

    @classmethod
    def from_env(cls) -> "ArchiveManager":
        return cls(
            cold_storage_dir=os.environ.get(
                "EXPORT_ARCHIVE_DIR",
                str(DEFAULT_ARCHIVE_DIR),
            ),
            s3_bucket=os.environ.get("ARCHIVE_S3_BUCKET"),
            s3_prefix=os.environ.get("ARCHIVE_S3_PREFIX", "funding-bot"),
        )

    def _client(self) -> Any:
        if self._s3_client is not None:
            return self._s3_client
        if boto3 is None:
            raise RuntimeError("boto3 is required for S3 archival support.")
        self._s3_client = boto3.client("s3")
        return self._s3_client

    def archive_file(
        self, path: str | Path, *, archive_name: str | None = None
    ) -> list[dict[str, Any]]:
        source = Path(path)
        # Use os.path.basename to strip directory components and prevent path traversal
        raw_name = archive_name if archive_name is not None else source.name
        name = os.path.basename(raw_name)
        if not name:
            raise ValueError(
                f"archive_name {archive_name!r} resolves to an empty filename after "
                "stripping path components."
            )
        archived_locations: list[dict[str, Any]] = []
        if self.cold_storage_dir is not None:
            target = self.cold_storage_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            archived_locations.append({"type": "cold_storage", "uri": str(target)})
        if self.s3_bucket:
            key = "/".join(part for part in (self.s3_prefix, name) if part)
            self._client().upload_file(str(source), self.s3_bucket, key)
            archived_locations.append({"type": "s3", "uri": f"s3://{self.s3_bucket}/{key}"})
        return archived_locations

    def archive_payload(
        self,
        payload: dict[str, Any],
        *,
        archive_name: str,
    ) -> dict[str, Any]:
        # Use os.path.basename to strip directory components and prevent path traversal
        safe_name = os.path.basename(archive_name)
        if not safe_name:
            raise ValueError(
                f"archive_name {archive_name!r} resolves to an empty filename after "
                "stripping path components."
            )
        target_root = self.cold_storage_dir or DEFAULT_ARCHIVE_DIR
        target_path = Path(target_root) / safe_name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        locations = self.archive_file(target_path, archive_name=safe_name)
        return {
            "path": str(target_path),
            "locations": locations,
        }


class WarehouseExportService:
    def __init__(self, bot: "FundingBot") -> None:
        self.bot = bot

    def export(
        self,
        *,
        datasets: Iterable[str] | None = None,
        export_format: str | None = None,
        output_dir: str | Path | None = None,
        archive: bool = False,
        dry_run: bool = False,
        archive_manager: ArchiveManager | None = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        normalized_datasets = normalize_datasets(datasets)
        normalized_format = normalize_export_format(export_format)
        export_root = Path(output_dir or DEFAULT_EXPORT_OUTPUT_DIR)
        # Prevent path traversal: reject paths containing '..' components
        if ".." in export_root.parts:
            raise ValueError(
                f"output_dir {str(output_dir)!r} must not contain '..' path components."
            )
        exported_at = self.bot._to_iso()
        archive_manager = archive_manager or ArchiveManager.from_env()
        artifacts: list[dict[str, Any]] = []
        total_datasets = len(normalized_datasets)
        if not dry_run:
            export_root.mkdir(parents=True, exist_ok=True)
        if progress_callback is not None:
            progress_callback(
                {
                    "stage": "warehouse-export",
                    "description": (
                        "Previewing warehouse datasets"
                        if dry_run
                        else "Exporting warehouse datasets"
                    ),
                    "completed": 0,
                    "total": total_datasets,
                }
            )
        for dataset in normalized_datasets:
            rows = self._dataset_rows(dataset)
            file_name = f"{dataset}_{exported_at.replace(':', '').replace('+', '_')}.{self._suffix_for(normalized_format)}"
            file_path = export_root / file_name
            row_count = len(rows)
            artifact = {
                "dataset": dataset,
                "format": normalized_format,
                "path": str(file_path),
                "row_count": row_count,
                "will_archive": bool(archive),
            }
            if dry_run:
                artifact["sha256"] = None
            else:
                self._write_dataset(
                    dataset=dataset,
                    rows=rows,
                    export_format=normalized_format,
                    file_path=file_path,
                    exported_at=exported_at,
                )
                artifact["sha256"] = hashlib.sha256(file_path.read_bytes()).hexdigest()
                if archive:
                    artifact["archived_locations"] = archive_manager.archive_file(
                        file_path,
                        archive_name=file_name,
                    )
            artifacts.append(artifact)
            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "warehouse-export",
                        "description": (
                            f"Previewing warehouse datasets ({dataset})"
                            if dry_run
                            else f"Exporting warehouse datasets ({dataset})"
                        ),
                        "current": dataset,
                        "completed": len(artifacts),
                        "total": total_datasets,
                    }
                )

        if not dry_run:
            self.bot._log_action(
                "data_warehouse_exported",
                datasets=normalized_datasets,
                export_format=normalized_format,
                output_dir=str(export_root),
                archive=archive,
                artifacts=[
                    {
                        "dataset": artifact["dataset"],
                        "format": artifact["format"],
                        "path": artifact["path"],
                        "row_count": artifact["row_count"],
                        "archived_locations": artifact.get("archived_locations", []),
                    }
                    for artifact in artifacts
                ],
            )
        return {
            "datasets": normalized_datasets,
            "format": normalized_format,
            "output_dir": str(export_root),
            "archive": archive,
            "dry_run": dry_run,
            "count": len(artifacts),
            "artifacts": artifacts,
            "exported_at": exported_at,
        }

    def _dataset_rows(self, dataset: str) -> list[dict[str, Any]]:
        if dataset == "donors":
            return self.bot.list_donors()
        if dataset == "tasks":
            return self.bot.list_tasks()
        if dataset == "matches":
            rows = self.bot.connection.execute("""
                SELECT
                    o.signature AS match_id,
                    o.signature AS opportunity_signature,
                    o.source,
                    o.donor_name,
                    o.title,
                    o.portal_url,
                    o.summary,
                    o.category,
                    o.discovered_at,
                    o.status AS match_status,
                    CASE WHEN a.id IS NULL THEN 0 ELSE 1 END AS has_application,
                    a.id AS application_id,
                    a.status AS application_status,
                    a.submitted_at,
                    a.next_action,
                    a.submission_reference
                FROM opportunities o
                LEFT JOIN applications a
                    ON a.opportunity_signature = o.signature
                ORDER BY o.discovered_at DESC, o.signature DESC
                """).fetchall()
            return [dict(row) for row in rows]
        if dataset == "results":
            rows = self.bot.connection.execute("""
                SELECT
                    a.id AS application_id,
                    a.opportunity_signature,
                    o.source,
                    o.donor_name,
                    o.title,
                    a.portal_url,
                    a.submitted_at,
                    a.status,
                    a.next_action,
                    a.submission_reference,
                    COUNT(sa.id) AS attempt_count,
                    COALESCE(SUM(CASE WHEN sa.succeeded = 0 THEN 1 ELSE 0 END), 0) AS failed_attempt_count,
                    COALESCE(SUM(CASE WHEN sa.succeeded = 1 THEN 1 ELSE 0 END), 0) AS successful_attempt_count,
                    MAX(sa.happened_at) AS last_attempt_at
                FROM applications a
                LEFT JOIN opportunities o
                    ON o.signature = a.opportunity_signature
                LEFT JOIN submission_attempts sa
                    ON sa.opportunity_signature = a.opportunity_signature
                GROUP BY
                    a.id,
                    a.opportunity_signature,
                    o.source,
                    o.donor_name,
                    o.title,
                    a.portal_url,
                    a.submitted_at,
                    a.status,
                    a.next_action,
                    a.submission_reference
                ORDER BY a.submitted_at DESC, a.id DESC
                """).fetchall()
            return [dict(row) for row in rows]
        raise ValueError(f"Unsupported export dataset {dataset!r}.")

    @staticmethod
    def _suffix_for(export_format: str) -> str:
        return "parquet" if export_format == "parquet" else export_format

    def _write_dataset(
        self,
        *,
        dataset: str,
        rows: list[dict[str, Any]],
        export_format: str,
        file_path: Path,
        exported_at: str,
    ) -> int:
        if export_format == "json":
            payload = {
                "dataset": dataset,
                "exported_at": exported_at,
                "count": len(rows),
                "records": rows,
            }
            file_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
            )
            return len(rows)
        flattened_rows = [_flatten_row(row) for row in rows]
        if export_format == "csv":
            fieldnames: list[str] = []
            for row in flattened_rows:
                for key in row:
                    if key not in fieldnames:
                        fieldnames.append(key)
            with file_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames or ["_empty"])
                writer.writeheader()
                for row in flattened_rows:
                    writer.writerow(row)
            return len(flattened_rows)
        if export_format == "parquet":
            if pa is None or pq is None:
                raise RuntimeError("pyarrow is required for Parquet exports.")
            table = pa.Table.from_pylist(flattened_rows)
            pq.write_table(table, file_path)
            return table.num_rows
        raise ValueError(f"Unsupported export format {export_format!r}.")
