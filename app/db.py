"""SQLite storage: schema, connections and seed data.

The database is the system of record. data/units.json only seeds it on first
run, so occupancy changes live in the `units` table, not in the JSON file.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

SEED_DIR = Path(__file__).parent.parent / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    unit_id TEXT PRIMARY KEY,
    property_name TEXT NOT NULL,
    building_name TEXT NOT NULL,
    label TEXT NOT NULL,
    type TEXT,
    area_sqm REAL,
    parking_bay TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'review',           -- review | active
    unit_id TEXT REFERENCES units(unit_id),          -- set when a human links the lease
    segments TEXT NOT NULL,                          -- JSON: the document as cited
    trace TEXT NOT NULL,                             -- JSON: the agent's tool calls
    summary TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lease_fields (
    lease_id INTEGER NOT NULL REFERENCES leases(id),
    name TEXT NOT NULL,
    value TEXT,                                      -- JSON: what the agent extracted
    override TEXT,                                   -- JSON: what a human corrected it to
    quote TEXT,
    segment TEXT,
    cited INTEGER NOT NULL DEFAULT 0,                -- 1 when the quote was found in the document
    decision TEXT NOT NULL DEFAULT 'pending',        -- pending | accepted | rejected
    PRIMARY KEY (lease_id, name)
);
CREATE TABLE IF NOT EXISTS lease_flags (
    id INTEGER PRIMARY KEY,
    lease_id INTEGER NOT NULL REFERENCES leases(id),
    kind TEXT NOT NULL,
    field TEXT,
    message TEXT NOT NULL,
    quote TEXT,
    segment TEXT,
    raised_by TEXT NOT NULL,                         -- agent | system
    decision TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(unit_id),
    reporter TEXT NOT NULL,
    note TEXT NOT NULL,
    photos TEXT NOT NULL,                            -- JSON: [{file, name}]
    observations TEXT NOT NULL,                      -- JSON: [{photo, item, condition, damage}]
    condition TEXT NOT NULL,
    condition_reason TEXT NOT NULL,
    work_order TEXT NOT NULL,                        -- JSON: {title, description, priority, trade}
    trace TEXT NOT NULL,
    summary TEXT NOT NULL,
    model TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS issues_by_unit ON issues(unit_id);
"""


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "var"))


def uploads_dir() -> Path:
    return data_dir() / "uploads"


@contextmanager
def connect():
    """A connection that commits on success, rolls back on error, and always closes."""
    con = sqlite3.connect(data_dir() / "app.db")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


@lru_cache
def ruleset() -> dict:
    return json.loads((SEED_DIR / "owner_ruleset.json").read_text(encoding="utf-8"))


def init() -> None:
    """Create the schema and seed the unit register if it is empty."""
    uploads_dir().mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        if con.execute("SELECT 1 FROM units LIMIT 1").fetchone():
            return
        register = json.loads((SEED_DIR / "units.json").read_text(encoding="utf-8"))
        for prop in register["properties"]:
            for building in prop["buildings"]:
                for unit in building["units"]:
                    con.execute(
                        "INSERT INTO units VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (unit["unit_id"], prop["name"], building["name"], unit["label"], unit.get("type"),
                         unit.get("area_sqm"), unit.get("parking_bay"), unit["status"]),
                    )
