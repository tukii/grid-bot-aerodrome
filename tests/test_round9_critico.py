"""
Tests para los 3 fixes CRÍTICOS del robustness review round 9.
Cubre: WAL mode, atomic config writes, corrupt DB recovery.
"""
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fix 1: SQLite WAL mode
# ---------------------------------------------------------------------------
class TestWALMode:
    """Store.__init__ debe habilitar WAL después de connect()."""

    def test_wal_enabled_on_fresh_db(self, tmp_path):
        db = str(tmp_path / "test.db")
        from paperbot.paper.store import Store
        s = Store(db)
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"Expected WAL, got {mode}"
        s.close()

    def test_wal_enabled_on_existing_db(self, tmp_path):
        db = str(tmp_path / "test.db")
        from paperbot.paper.store import Store
        s1 = Store(db)
        s1.record_tick("t1", 100.0, "buy", 10.0, 0.01, 0.001, 90.0, 10.0, 100.0)
        s1.close()
        # Reabrir: WAL debe persistir
        s2 = Store(db)
        mode = s2._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"Expected WAL after reopen, got {mode}"
        s2.close()


# ---------------------------------------------------------------------------
# Fix 2: Atomic config writes
# ---------------------------------------------------------------------------
class TestAtomicConfigWrite:
    """reanchor_config debe escribir a .tmp y renombrar atómicamente."""

    def _make_config(self, tmp_path) -> Path:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "grid:\n"
            "  anchor_price: 2000.0\n"
            "  spacing_pct: 2.0\n"
            "live:\n"
            "  stop_loss_pct: 10\n"
        )
        return cfg

    def _make_db(self, tmp_path) -> Path:
        db = tmp_path / "live.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.commit()
        con.close()
        return db

    def test_atomic_write_no_tmp_left_behind(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path)
        db = self._make_db(tmp_path)
        tmp_path_shim = tmp_path / "config.yaml.tmp"

        import supervisor
        monkeypatch.setattr(supervisor, "CONFIG", cfg)
        monkeypatch.setattr(supervisor, "DB", db)
        monkeypatch.setattr(supervisor, "restart_bot", lambda: True)

        ok = supervisor.reanchor_config(2500.0, "test")

        assert ok is True
        # .tmp must NOT survive
        assert not tmp_path_shim.exists(), "tmp file should not survive atomic rename"
        # content must have the new anchor
        content = cfg.read_text()
        assert "2500.0" in content
        assert "2000.0" not in content

    def test_no_corrupt_on_rename_failure(self, tmp_path, monkeypatch):
        """Simular fallo en rename: .tmp existe pero config original intacto."""
        cfg = self._make_config(tmp_path)
        db = self._make_db(tmp_path)

        import supervisor
        monkeypatch.setattr(supervisor, "CONFIG", cfg)
        monkeypatch.setattr(supervisor, "DB", db)
        monkeypatch.setattr(supervisor, "restart_bot", lambda: True)

        # Patch os.rename to raise
        original_rename = os.rename
        def fail_rename(src, dst):
            raise OSError("simulated rename failure")
        monkeypatch.setattr(os, "rename", fail_rename)

        with pytest.raises(OSError):
            supervisor.reanchor_config(2500.0, "test")

        # Original config must be UNTOUCHED
        content = cfg.read_text()
        assert "2000.0" in content, "config.yaml should not be corrupted on rename failure"
        # .tmp may or may not exist (depends on when rename failed), that's fine

    def test_file_consistent_after_write(self, tmp_path, monkeypatch):
        """El rename es atómico: el lector nunca ve contenido parcial."""
        cfg = self._make_config(tmp_path)
        db = self._make_db(tmp_path)

        import supervisor
        monkeypatch.setattr(supervisor, "CONFIG", cfg)
        monkeypatch.setattr(supervisor, "DB", db)
        monkeypatch.setattr(supervisor, "restart_bot", lambda: True)

        ok = supervisor.reanchor_config(3000.0, "test")

        assert ok is True
        content = cfg.read_text()
        # Must be well-formed: anchor_price changed
        assert "3000.0" in content
        assert "2000.0" not in content

    def test_backup_created_before_write(self, tmp_path, monkeypatch):
        cfg = self._make_config(tmp_path)
        db = self._make_db(tmp_path)
        bak = tmp_path / "data" / "config.backup.yaml"
        bak.parent.mkdir(parents=True, exist_ok=True)

        import supervisor
        monkeypatch.setattr(supervisor, "CONFIG", cfg)
        monkeypatch.setattr(supervisor, "DB", db)
        monkeypatch.setattr(supervisor, "BAK", bak)
        monkeypatch.setattr(supervisor, "restart_bot", lambda: True)

        ok = supervisor.reanchor_config(3500.0, "test")

        assert ok is True
        assert bak.exists(), "backup should exist after reanchor"
        assert "2000.0" in bak.read_text(), "backup should have original anchor"


# ---------------------------------------------------------------------------
# Fix 3: Store corrupt DB recovery
# ---------------------------------------------------------------------------
class TestCorruptDBRecovery:
    """Si live.db está corrupto, Store debe renombrar a .corrupt.{ts} y crear nueva."""

    def test_corrupt_db_renamed_and_new_created(self, tmp_path):
        db = tmp_path / "live.db"
        # Escribir bytes basura
        db.write_bytes(b"NOT A VALID SQLITE DATABASE FILE\x00\x00\x00")

        from paperbot.paper.store import Store
        s = Store(str(db))

        # DB original debe ser renombrada
        corrupt_files = list(tmp_path.glob("live.db.corrupt.*"))
        assert len(corrupt_files) == 1, f"Expected 1 corrupt file, got {corrupt_files}"

        # Nueva DB debe funcionar
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        s.record_tick("t1", 100.0, "buy", 10.0, 0.01, 0.001, 90.0, 10.0, 100.0)
        tick = s.last_tick()
        assert tick is not None
        assert tick["price"] == 100.0
        s.close()

    def test_valid_db_not_touched(self, tmp_path):
        db = tmp_path / "live.db"
        from paperbot.paper.store import Store

        s1 = Store(str(db))
        s1.record_tick("t1", 100.0, "buy", 10.0, 0.01, 0.001, 90.0, 10.0, 100.0)
        s1.close()

        s2 = Store(str(db))
        tick = s2.last_tick()
        assert tick is not None
        assert tick["price"] == 100.0

        corrupt_files = list(tmp_path.glob("live.db.corrupt.*"))
        assert len(corrupt_files) == 0, "Valid DB should not be renamed"
        s2.close()

    def test_half_written_header_corrupt(self, tmp_path):
        """Simula crash a mitad de escribir el header de SQLite."""
        db = tmp_path / "live.db"
        # SQLite header starts with "SQLite format 3\000" (16 bytes)
        db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 100)

        from paperbot.paper.store import Store
        s = Store(str(db))

        corrupt_files = list(tmp_path.glob("live.db.corrupt.*"))
        assert len(corrupt_files) == 1
        # DB must be operational
        s.set_meta("test", "value")
        assert s.get_meta("test") == "value"
        s.close()

    def test_corrupt_timestamp_format(self, tmp_path):
        """Nombre del archivo corrupto debe tener formato live.db.corrupt.{unix_ts}."""
        db = tmp_path / "live.db"
        db.write_bytes(b"GARBAGE")

        before = int(time.time())
        from paperbot.paper.store import Store
        s = Store(str(db))
        after = int(time.time())
        s.close()

        corrupt_files = list(tmp_path.glob("live.db.corrupt.*"))
        assert len(corrupt_files) == 1
        name = corrupt_files[0].name
        ts_str = name.split("corrupt.")[1]
        ts_val = int(ts_str)
        assert before <= ts_val <= after, f"Timestamp {ts_val} not in [{before}, {after}]"

    def test_old_data_lost_but_bot_operational(self, tmp_path):
        """Después de recovery, el bot puede operar pero los datos previos se pierden."""
        db = tmp_path / "live.db"
        # Create valid DB, write data, then corrupt it
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE ticks (ts TEXT PRIMARY KEY, price REAL)")
        con.execute("INSERT INTO ticks VALUES ('old_tick', 999.0)")
        con.commit()
        con.close()

        # Now corrupt the file (overwrite header)
        with open(db, "r+b") as f:
            f.write(b"CORRUPTED")

        from paperbot.paper.store import Store
        s = Store(str(db))
        # Old data is gone
        assert s.last_tick() is None
        # But bot is operational
        s.record_tick("new_tick", 1500.0, "buy", 10.0, 0.01, 0.001, 90.0, 10.0, 1500.0)
        tick = s.last_tick()
        assert tick["price"] == 1500.0
        s.close()
