import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("liquidity_history.db")


def connect():
    return sqlite3.connect(DB_PATH, timeout=10)


def initialize_database():
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                sampled_at TEXT NOT NULL,
                mid_price REAL NOT NULL,
                levels_json TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time
            ON snapshots(symbol, sampled_at)
        """)


def save_snapshot(symbol, sampled_at, mid_price, levels):
    with connect() as con:
        con.execute(
            """
            INSERT INTO snapshots
            (symbol, sampled_at, mid_price, levels_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                symbol,
                sampled_at.isoformat(),
                float(mid_price),
                json.dumps({
                    str(float(price)): float(quantity)
                    for price, quantity in levels.items()
                }),
            ),
        )


def load_recent_snapshots(symbol, limit=60):
    with connect() as con:
        rows = con.execute(
            """
            SELECT sampled_at, mid_price, levels_json
            FROM snapshots
            WHERE symbol = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()

    # Return chronological order for the heatmap.
    rows.reverse()

    snapshots = []

    for sampled_at, mid_price, levels_json in rows:
        snapshots.append({
            "time": sampled_at,
            "mid": mid_price,
            "levels": {
                float(price): float(quantity)
                for price, quantity in json.loads(levels_json).items()
            },
        })

    return snapshots
