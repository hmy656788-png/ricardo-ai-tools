import sqlite3
from pathlib import Path

import unittest

from webui import backup


def _db(path: Path, value: str) -> None:
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS sample (value TEXT)")
        con.execute("DELETE FROM sample")
        con.execute("INSERT INTO sample VALUES (?)", (value,))


class BackupTests(unittest.TestCase):
  def test_create_list_and_restore_are_local_and_explicit(self):
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
      tmp_path = Path(folder)
      live = tmp_path / "webui.db"
      snapshots = tmp_path / "backups"
      _db(live, "before")
      old_db, old_dir = backup.db.DB_PATH, backup.BACKUP_DIR
      backup.db.DB_PATH, backup.BACKUP_DIR = live, snapshots
      try:
        created = backup.create_backup(reason="before-update")
        self.assertTrue(created["local_only"])
        self.assertEqual(created["reason"], "before-update")
        self.assertGreater(created["size"], 0)
        self.assertEqual(Path(created["filename"]).name, created["filename"])
        self.assertEqual(backup.list_backups()[0]["id"], created["id"])

        _db(live, "after")
        restored = backup.restore_backup(created["id"])
        self.assertTrue(restored["ok"])
        self.assertEqual(restored["pre_restore_backup"]["reason"], "before-restore")
        with sqlite3.connect(live) as con:
          self.assertEqual(con.execute("SELECT value FROM sample").fetchone()[0], "before")
        self.assertEqual(len(backup.list_backups()), 2)
      finally:
        backup.db.DB_PATH, backup.BACKUP_DIR = old_db, old_dir

  def test_backup_ids_cannot_escape_directory(self):
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
      old_dir = backup.BACKUP_DIR
      backup.BACKUP_DIR = Path(folder) / "backups"
      try:
        for value in ["../webui.db", "/tmp/webui.db", "not-a-backup.sqlite3"]:
          with self.assertRaises(ValueError):
            backup._backup_path(value)
      finally:
        backup.BACKUP_DIR = old_dir


if __name__ == "__main__":
  unittest.main()
