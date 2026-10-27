"""Local, explicit SQLite backups for the manual update/rollback flow.

Backups deliberately stay on the local machine.  This module does not upload,
download, unpack, or execute an update and it never includes secrets in its
metadata response.  The SQLite backup API is used so WAL-mode data is captured
consistently without reading rows into application memory.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from . import db

BACKUP_DIR = Path(__file__).resolve().parent / "backups"
BACKUP_PREFIX = "ricardo-ai-webui-"


def _safe_id(value: str) -> str:
    name = Path(value).name
    if not name.startswith(BACKUP_PREFIX) or not name.endswith(".sqlite3"):
        raise ValueError("备份文件无效")
    if name != value:
        raise ValueError("备份路径无效")
    return name[:-len(".sqlite3")]


def _backup_path(backup_id: str) -> Path:
    safe_id = _safe_id(backup_id if backup_id.endswith(".sqlite3") else f"{backup_id}.sqlite3")
    path = (BACKUP_DIR / f"{safe_id}.sqlite3").resolve()
    if path.parent != BACKUP_DIR.resolve():
        raise ValueError("备份路径无效")
    return path


def _integrity_check(path: Path) -> None:
    with sqlite3.connect(str(path), timeout=30) as con:
        result = con.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise ValueError("备份文件完整性检查失败")


def _metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "id": path.stem,
        "created_at": stat.st_mtime,
        "size": stat.st_size,
        "filename": path.name,
        "local_only": True,
    }


def create_backup(*, reason: str = "manual") -> dict[str, Any]:
    """Create a consistent local snapshot and return non-sensitive metadata."""
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    backup_id = f"{BACKUP_PREFIX}{stamp}-{time.time_ns() % 1_000_000:06d}"
    final_path = BACKUP_DIR / f"{backup_id}.sqlite3"
    fd, temp_name = tempfile.mkstemp(prefix=f".{backup_id}-", suffix=".tmp", dir=BACKUP_DIR)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        # sqlite3.Connection.backup handles WAL pages consistently.
        with sqlite3.connect(str(db.DB_PATH), timeout=30) as source, sqlite3.connect(str(temp_path), timeout=30) as target:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target.commit()
        _integrity_check(temp_path)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, final_path)
        metadata = _metadata(final_path)
        metadata["reason"] = str(reason or "manual")[:80]
        return metadata
    finally:
        temp_path.unlink(missing_ok=True)


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.is_dir():
        return []
    items = []
    for path in BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.sqlite3"):
        try:
            if path.is_file():
                items.append(_metadata(path))
        except OSError:
            continue
    return sorted(items, key=lambda item: item["created_at"], reverse=True)


def restore_backup(backup_id: str) -> dict[str, Any]:
    """Restore a local snapshot into the live DB; caller must confirm explicitly."""
    source_path = _backup_path(backup_id)
    if not source_path.is_file():
        raise FileNotFoundError("找不到备份")
    _integrity_check(source_path)
    # Keep a pre-restore snapshot so a mistaken rollback remains recoverable.
    pre_restore = create_backup(reason="before-restore")
    fd, temp_name = tempfile.mkstemp(prefix=".restore-", suffix=".tmp", dir=db.DB_PATH.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(str(source_path), timeout=30) as source, sqlite3.connect(str(temp_path), timeout=30) as target:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target.commit()
        _integrity_check(temp_path)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, db.DB_PATH)
        return {"ok": True, "restored": _metadata(source_path), "pre_restore_backup": pre_restore}
    finally:
        temp_path.unlink(missing_ok=True)
