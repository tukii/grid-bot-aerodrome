import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from paperbot.config import load_config


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ticks (
                    ts TEXT PRIMARY KEY,
                    price REAL,
                    side TEXT,
                    size_usd REAL,
                    fee_usd REAL,
                    gas_usd REAL,
                    cash REAL,
                    position_usd REAL,
                    total_usd REAL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    side TEXT,
                    price REAL,
                    size_usd REAL,
                    fee_usd REAL,
                    gas_usd REAL,
                    filled INTEGER,
                    tx_hash TEXT,
                    gas_used INTEGER
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
        # Migraciones: añadir columnas nuevas si faltan
        self._migrate("trades", "tx_hash", "TEXT")
        self._migrate("trades", "gas_used", "INTEGER")

    def _migrate(self, table: str, column: str, coltype: str):
        allowed_tables = {"ticks", "trades", "meta"}
        allowed_types = {"TEXT", "INTEGER", "REAL", "BLOB"}
        if table not in allowed_tables:
            raise ValueError(f"unexpected table: {table}")
        if coltype not in allowed_types:
            raise ValueError(f"unexpected coltype: {coltype}")
        cols = [r[1] for r in self._conn.execute(f"PRAGMA table_info('{table}')")]
        if column not in cols:
            with self._conn:
                self._conn.execute(f"ALTER TABLE '{table}' ADD COLUMN '{column}' {coltype}")

    def set_meta(self, key: str, value: str):
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def record_tick(self, ts: str, price: float, side: str, size_usd: float,
                    fee_usd: float, gas_usd: float, cash: float, position_usd: float, total_usd: float):
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ticks (ts, price, side, size_usd, fee_usd, gas_usd, cash, position_usd, total_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, price, side, size_usd, fee_usd, gas_usd, cash, position_usd, total_usd),
            )

    def record_trade(self, ts: str, side: str, price: float, size_usd: float,
                     fee_usd: float, gas_usd: float, filled: int,
                     tx_hash: str | None = None, gas_used: int | None = None):
        with self._conn:
            self._conn.execute(
                "INSERT INTO trades (ts, side, price, size_usd, fee_usd, gas_usd, filled, tx_hash, gas_used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, side, price, size_usd, fee_usd, gas_usd, filled, tx_hash, gas_used),
            )

    def equity_series(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT ts, total_usd FROM ticks ORDER BY ts", self._conn)

    def last_tick(self) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM ticks ORDER BY ts DESC LIMIT 1").fetchone()

    def recent_trades(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM trades WHERE filled = 1 ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS buys, "
            "SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) AS sells, "
            "COALESCE(SUM(fee_usd + gas_usd), 0) AS fees "
            "FROM trades WHERE filled = 1"
        ).fetchone()
        return {"n": row["n"], "buys": row["buys"], "sells": row["sells"], "fees": row["fees"]}

    def close(self):
        self._conn.close()

    @contextmanager
    def session(self):
        yield self
        self.close()
