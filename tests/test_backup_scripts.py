from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.backup_sqlite import create_backup
from scripts.verify_backup import verify_backup


def _create_database(db_path: Path, *, wal: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE grants (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    connection.execute("INSERT INTO grants (name) VALUES (?)", ("Community Education Fund",))
    connection.commit()
    return connection


def test_full_backup_and_verification(tmp_path: Path) -> None:
    db_path = tmp_path / "funding_bot.db"
    backup_root = tmp_path / "backups"
    connection = _create_database(db_path)
    connection.close()

    result = create_backup(db_path, backup_root, backup_type="full", retention_days=7)
    verification = verify_backup(result.backup_dir)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.backup_dir.exists()
    assert manifest["backup_type"] == "full"
    assert verification.backup_type == "full"
    assert verification.verified_files >= 1


def test_wal_backup_and_verification(tmp_path: Path) -> None:
    db_path = tmp_path / "funding_bot.db"
    backup_root = tmp_path / "backups"
    connection = _create_database(db_path, wal=True)
    connection.execute("INSERT INTO grants (name) VALUES (?)", ("After-school Matching Program",))
    connection.commit()

    result = create_backup(db_path, backup_root, backup_type="wal", retention_days=7)
    verification = verify_backup(result.backup_dir)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    copied_files = {entry["name"] for entry in manifest["files"]}
    assert manifest["journal_mode"] == "wal"
    assert manifest["backup_type"] == "wal"
    assert db_path.name in copied_files
    assert verification.backup_type == "wal"
    assert verification.verified_files >= 1
    connection.close()
