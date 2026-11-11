"""The radio's database: tracks, chart positions, loudness and what's been played."""
import os
import sqlite3
import threading

from .config import DATA_DIR

DB_FILE = os.path.join(DATA_DIR, "radio.db")
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    folder TEXT NOT NULL,
    size INTEGER, mtime REAL,
    artist TEXT, title TEXT, album TEXT, genre TEXT,
    duration REAL,
    chart_key TEXT,
    gain REAL,                 -- dB to apply for volume levelling (NULL = not measured yet)
    added REAL
);
CREATE INDEX IF NOT EXISTS tracks_folder ON tracks(folder);
CREATE INDEX IF NOT EXISTS tracks_key ON tracks(chart_key);
CREATE TABLE IF NOT EXISTS charts (
    key TEXT PRIMARY KEY,      -- "artist|title", simplified
    country TEXT,              -- UK / US, NULL = not a chart hit
    position INTEGER,
    year INTEGER,
    checked REAL
);
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY,
    track_id INTEGER, artist_key TEXT, chart_key TEXT, played REAL
);
CREATE INDEX IF NOT EXISTS plays_played ON plays(played);
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY, mtime REAL
);
"""


def connect() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        con = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(SCHEMA)
        _local.con = con
    return con
