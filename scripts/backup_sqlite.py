from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Iterable

DEFAULT_RETENTION_DAYS = 35
FULL_MODE = "full"
WAL_MODE = "wal"


@dataclass(frozen=True)
class BackupFile:
    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BackupResult:
    backup_dir: Path
    manifest_path: Path
    backup_type: str
    created_at: str


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _read_journal_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else "delete"


def _hash_file(path: Path) -> BackupFile:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return BackupFile(name=path.name, size_bytes=path.stat().st_size, sha256=digest.hexdigest())


def _write_manifest(
    backup_dir: Path,
    *,
    created_at: str,
    backup_type: str,
    source_db: Path,
    journal_mode: str,
    retention_days: int,
    files: Iterable[BackupFile],
) -> Path:
    manifest_path = backup_dir / "manifest.json"
    payload = {
        "format_version": 1,
        "created_at": created_at,
        "backup_type": backup_type,
        "source_db": str(source_db),
        "journal_mode": journal_mode,
        "retention_days": retention_days,
        "files": [
            {"name": item.name, "size_bytes": item.size_bytes, "sha256": item.sha256}
            for item in files
        ],
    }
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _backup_full(source_db: Path, target_db: Path) -> None:
    with sqlite3.connect(source_db) as source_connection:
        with sqlite3.connect(target_db) as target_connection:
            source_connection.backup(target_connection)


def _backup_wal(source_db: Path, backup_dir: Path) -> None:
    destination_db = backup_dir / source_db.name
    with sqlite3.connect(source_db, timeout=30, isolation_level=None) as connection:
        journal_mode = _read_journal_mode(connection)
        if journal_mode != "wal":
            raise RuntimeError("WAL backups require the database to use PRAGMA journal_mode=WAL.")
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for candidate in (
                source_db,
                source_db.with_name(f"{source_db.name}-wal"),
                source_db.with_name(f"{source_db.name}-shm"),
            ):
                if candidate.exists():
                    shutil.copy2(candidate, backup_dir / candidate.name)
        finally:
            connection.execute("COMMIT")
    if not destination_db.exists():
        raise RuntimeError("WAL backup did not copy the primary database file.")


def _prune_old_backups(output_dir: Path, *, retention_days: int) -> list[Path]:
    if retention_days < 0:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    removed: list[Path] = []
    for candidate in output_dir.iterdir() if output_dir.exists() else []:
        manifest_path = candidate / "manifest.json"
        if not candidate.is_dir() or not manifest_path.exists():
            continue
        modified_at = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
        if modified_at < cutoff:
            shutil.rmtree(candidate)
            removed.append(candidate)
    return removed


def create_backup(
    db_path: Path,
    output_dir: Path,
    *,
    backup_type: str,
    retention_days: int,
) -> BackupResult:
    if backup_type not in {FULL_MODE, WAL_MODE}:
        raise ValueError(f"Unsupported backup type: {backup_type}")
    if not db_path.exists():
        raise FileNotFoundError(f"Database file does not exist: {db_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    created_at = _utc_timestamp()
    backup_dir = output_dir / f"{created_at}-{backup_type}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    with sqlite3.connect(db_path) as connection:
        journal_mode = _read_journal_mode(connection)

    if backup_type == FULL_MODE:
        target_db = backup_dir / f"{db_path.stem}.sqlite3"
        _backup_full(db_path, target_db)
    else:
        _backup_wal(db_path, backup_dir)

    backup_files = [_hash_file(path) for path in sorted(backup_dir.iterdir()) if path.is_file()]
    manifest_path = _write_manifest(
        backup_dir,
        created_at=created_at,
        backup_type=backup_type,
        source_db=db_path,
        journal_mode=journal_mode,
        retention_days=retention_days,
        files=backup_files,
    )
    _prune_old_backups(output_dir, retention_days=retention_days)
    return BackupResult(
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        backup_type=backup_type,
        created_at=created_at,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create consistent SQLite backup snapshots.")
    parser.add_argument(
        "--db",
        default=os.environ.get("BOT_DB_PATH", "funding_bot.db"),
        help="Path to the SQLite database file.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("BACKUP_DIRECTORY", "backups"),
        help="Directory that stores backup snapshots.",
    )
    parser.add_argument(
        "--mode",
        default=FULL_MODE,
        choices=[FULL_MODE, WAL_MODE],
        help="Backup mode: full copy or WAL sidecar snapshot.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.environ.get("BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
        help="Delete backup snapshot directories older than this many days.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    result = create_backup(
        Path(args.db).resolve(),
        Path(args.output_dir).resolve(),
        backup_type=args.mode,
        retention_days=args.retention_days,
    )
    print(f"Created {result.backup_type} backup at {result.backup_dir}")
    print(f"Manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
