from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class VerificationResult:
    backup_dir: Path
    backup_type: str
    verified_files: int


class BackupVerificationError(RuntimeError):
    pass


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(backup_dir: Path) -> dict:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise BackupVerificationError(f"manifest.json is missing from {backup_dir}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _verify_checksums(backup_dir: Path, manifest: dict) -> None:
    for file_entry in manifest.get("files", []):
        candidate = backup_dir / file_entry["name"]
        if not candidate.exists():
            raise BackupVerificationError(f"Expected backup file is missing: {candidate}")
        observed_hash = _hash_file(candidate)
        if observed_hash != file_entry["sha256"]:
            raise BackupVerificationError(f"Checksum mismatch for {candidate.name}")


def _run_integrity_check(db_path: Path, *, use_uri: bool) -> None:
    connect_target = f"file:{db_path}?mode=ro" if use_uri else str(db_path)
    with sqlite3.connect(connect_target, uri=use_uri) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise BackupVerificationError(f"SQLite integrity_check failed for {db_path}: {row!r}")


def _prepare_verification_copy(backup_dir: Path, manifest: dict) -> Path:
    workspace = backup_dir / ".verify-work"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir()
    try:
        for file_entry in manifest.get("files", []):
            candidate = backup_dir / file_entry["name"]
            shutil.copy2(candidate, workspace / candidate.name)
        db_candidates = sorted(workspace.glob("*.db")) + sorted(workspace.glob("*.sqlite3"))
        if not db_candidates:
            raise BackupVerificationError("Unable to locate a SQLite database file in the backup.")
        return db_candidates[0]
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def verify_backup(backup_dir: Path) -> VerificationResult:
    manifest = _load_manifest(backup_dir)
    backup_type = manifest.get("backup_type", "unknown")
    _verify_checksums(backup_dir, manifest)

    workspace_dir = None
    try:
        if backup_type == "wal":
            db_path = _prepare_verification_copy(backup_dir, manifest)
            workspace_dir = db_path.parent
            _run_integrity_check(db_path, use_uri=False)
        else:
            db_candidates = sorted(backup_dir.glob("*.db")) + sorted(backup_dir.glob("*.sqlite3"))
            if not db_candidates:
                raise BackupVerificationError("Unable to locate a SQLite database file in the backup.")
            _run_integrity_check(db_candidates[0], use_uri=True)
    finally:
        if workspace_dir and workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)

    return VerificationResult(
        backup_dir=backup_dir,
        backup_type=backup_type,
        verified_files=len(manifest.get("files", [])),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify SQLite backup snapshots.")
    parser.add_argument("backup_dir", help="Backup directory created by scripts/backup_sqlite.py")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = verify_backup(Path(args.backup_dir).resolve())
    print(
        f"Verified {result.backup_type} backup at {result.backup_dir} "
        f"({result.verified_files} file(s) checked)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
