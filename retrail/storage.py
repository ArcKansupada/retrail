"""SQLite storage. One local file, tree-structured from the start.

A fork is a new session row rather than a mutation, so original sessions stay
intact and comparable no matter how many times you branch off them.
"""

import json
import os
import sqlite3
import time
import uuid

from .errors import AmbiguousSha, NotFound
from .sha import compute_sha

DEFAULT_DIR = ".retrail"
DB_NAME = "sessions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    name              TEXT,
    parent_session_id TEXT,
    parent_sha        TEXT,
    forked_at_step    INTEGER,
    edit_json         TEXT,
    created_at        REAL NOT NULL,
    status            TEXT NOT NULL,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS steps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sha          TEXT NOT NULL UNIQUE,
    session_id   TEXT NOT NULL,
    step_number  INTEGER NOT NULL,
    step_type    TEXT NOT NULL,
    input_json   TEXT NOT NULL,
    output_json  TEXT NOT NULL,
    tokens_used  INTEGER,
    cost_usd     REAL,
    duration_ms  REAL,
    created_at   REAL NOT NULL,
    UNIQUE (session_id, step_number),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_steps_sha ON steps(sha);
CREATE INDEX IF NOT EXISTS idx_steps_session ON steps(session_id, step_number);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
"""


def default_db_path(root=None):
    return os.path.join(root or os.getcwd(), DEFAULT_DIR, DB_NAME)


class Store:
    def __init__(self, path=None):
        self.path = path or default_db_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

        # Every step is committed as it happens, so a run that crashes mid-loop
        # still has its trace - that run is the one you most want to inspect.
        # Paying a full fsync per step for that costs ~1.4ms; WAL plus
        # synchronous=NORMAL costs ~0.02ms and loses nothing we care about.
        # The failure it trades away is an OS crash or power cut losing the last
        # few commits. The failure it protects against - the agent process
        # throwing - is unaffected, because committed data survives process
        # death regardless. A debugger is allowed to lose its last step to a
        # power cut; it is not allowed to lose the trace of a crash.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")

        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- sessions ---------------------------------------------------------

    def create_session(
        self,
        name,
        parent_session_id=None,
        parent_sha=None,
        forked_at_step=None,
        edit=None,
        status="running",
    ):
        session_id = "s_" + uuid.uuid4().hex[:10]
        self.conn.execute(
            "INSERT INTO sessions (id, name, parent_session_id, parent_sha, "
            "forked_at_step, edit_json, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                name,
                parent_session_id,
                parent_sha,
                forked_at_step,
                json.dumps(edit) if edit is not None else None,
                time.time(),
                status,
            ),
        )
        self.conn.commit()
        return session_id

    def set_status(self, session_id, status):
        self.conn.execute(
            "UPDATE sessions SET status = ? WHERE id = ?", (status, session_id)
        )
        self.conn.commit()

    def get_session(self, session_id):
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"no session {session_id!r}")
        return dict(row)

    def list_sessions(self):
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- steps ------------------------------------------------------------

    def add_step(
        self,
        session_id,
        step_number,
        step_type,
        input_obj,
        output_obj,
        tokens_used=None,
        cost_usd=None,
        duration_ms=None,
    ):
        from .serialize import canonical_json

        sha = compute_sha(session_id, step_number, step_type, input_obj, output_obj)
        self.conn.execute(
            "INSERT INTO steps (sha, session_id, step_number, step_type, "
            "input_json, output_json, tokens_used, cost_usd, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sha,
                session_id,
                step_number,
                step_type,
                canonical_json(input_obj),
                canonical_json(output_obj),
                tokens_used,
                cost_usd,
                duration_ms,
                time.time(),
            ),
        )
        self.conn.commit()
        return sha

    def steps_for(self, session_id):
        rows = self.conn.execute(
            "SELECT * FROM steps WHERE session_id = ? ORDER BY step_number ASC",
            (session_id,),
        ).fetchall()
        return [_step(r) for r in rows]

    def next_step_number(self, session_id):
        row = self.conn.execute(
            "SELECT MAX(step_number) AS n FROM steps WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return 0 if row["n"] is None else row["n"] + 1

    def resolve_sha(self, prefix):
        """Resolve a (possibly abbreviated) SHA to exactly one step."""
        prefix = prefix.strip().lower()
        if not prefix:
            raise NotFound("empty SHA")
        rows = self.conn.execute(
            "SELECT sha FROM steps WHERE sha LIKE ? || '%'", (prefix,)
        ).fetchall()
        if not rows:
            raise NotFound(f"no step matching SHA {prefix!r}")
        if len(rows) > 1:
            raise AmbiguousSha(prefix, [r["sha"] for r in rows])
        return rows[0]["sha"]

    def get_step(self, sha_or_prefix):
        sha = self.resolve_sha(sha_or_prefix)
        row = self.conn.execute("SELECT * FROM steps WHERE sha = ?", (sha,)).fetchone()
        return _step(row)


def _step(row):
    d = dict(row)
    d["input"] = json.loads(d.pop("input_json"))
    d["output"] = json.loads(d.pop("output_json"))
    return d
