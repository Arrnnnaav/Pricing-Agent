"""SQLite connection + schema init. The only file that knows the schema
file's location -- callers just get a ready connection."""

import os
import sqlite3

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
