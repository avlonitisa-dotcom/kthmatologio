"""
SQLite database layer - schema, migrations, CRUD operations.
"""
import sqlite3
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

from config import Config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kaek_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kaek          TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending',
    -- pending | processing | completed | partial | failed
    retry_count   INTEGER NOT NULL DEFAULT 0,
    pdf_perigrafiki_path TEXT,
    pdf_xoriki_path      TEXT,
    pdf_perigrafiki_ok   INTEGER DEFAULT 0,
    pdf_xoriki_ok        INTEGER DEFAULT 0,
    failure_reason       TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kaek        TEXT,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL,  -- success | failure | info | warning
    message     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pdf_parse_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kaek            TEXT NOT NULL,
    pdf_type        TEXT NOT NULL,  -- perigrafiki | xoriki
    raw_text        TEXT,
    area_sqm        REAL,
    area_stremma    REAL,
    has_building    TEXT DEFAULT 'unknown',  -- yes | no | unknown
    has_burdens     TEXT DEFAULT 'unknown',  -- yes | no | unknown
    property_type   TEXT,
    ownership_info  TEXT,
    burdens_detail  TEXT,
    notes           TEXT,
    confidence      REAL DEFAULT 0.0,
    parse_method    TEXT,  -- text | ocr | llm
    parsed_at       TEXT,
    UNIQUE(kaek, pdf_type)
);

CREATE TABLE IF NOT EXISTS search_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    area_name       TEXT,
    map_url         TEXT,
    min_area_sqm    REAL,
    max_area_sqm    REAL,
    has_building    TEXT DEFAULT 'any',
    has_burdens     TEXT DEFAULT 'any',
    kaek_pattern    TEXT,
    status          TEXT DEFAULT 'created',
    kaek_count      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_kaek_jobs_status  ON kaek_jobs(status);
CREATE INDEX IF NOT EXISTS idx_audit_logs_kaek   ON audit_logs(kaek);
CREATE INDEX IF NOT EXISTS idx_parse_kaek        ON pdf_parse_results(kaek);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(Config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(SCHEMA)
    logger.info("Database initialized at %s", Config.DB_PATH)


def now_iso() -> str:
    return datetime.utcnow().isoformat()


# ── KAEK Jobs ──────────────────────────────────────────────────────────────────

def upsert_kaek(kaek: str) -> None:
    t = now_iso()
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO kaek_jobs (kaek, status, created_at, updated_at)
               VALUES (?, 'pending', ?, ?)
               ON CONFLICT(kaek) DO NOTHING""",
            (kaek, t, t),
        )


def upsert_kaek_batch(kaeks: list[str]) -> int:
    t = now_iso()
    with db_conn() as conn:
        cur = conn.executemany(
            """INSERT INTO kaek_jobs (kaek, status, created_at, updated_at)
               VALUES (?, 'pending', ?, ?)
               ON CONFLICT(kaek) DO NOTHING""",
            [(k, t, t) for k in kaeks],
        )
    return cur.rowcount


def update_kaek_status(
    kaek: str,
    status: str,
    failure_reason: Optional[str] = None,
    pdf_perigrafiki_path: Optional[str] = None,
    pdf_xoriki_path: Optional[str] = None,
    pdf_perigrafiki_ok: Optional[bool] = None,
    pdf_xoriki_ok: Optional[bool] = None,
) -> None:
    fields = ["status = ?", "updated_at = ?"]
    values: list = [status, now_iso()]

    if failure_reason is not None:
        fields.append("failure_reason = ?")
        values.append(failure_reason)
    if pdf_perigrafiki_path is not None:
        fields.append("pdf_perigrafiki_path = ?")
        values.append(pdf_perigrafiki_path)
    if pdf_xoriki_path is not None:
        fields.append("pdf_xoriki_path = ?")
        values.append(pdf_xoriki_path)
    if pdf_perigrafiki_ok is not None:
        fields.append("pdf_perigrafiki_ok = ?")
        values.append(1 if pdf_perigrafiki_ok else 0)
    if pdf_xoriki_ok is not None:
        fields.append("pdf_xoriki_ok = ?")
        values.append(1 if pdf_xoriki_ok else 0)

    values.append(kaek)
    with db_conn() as conn:
        conn.execute(
            f"UPDATE kaek_jobs SET {', '.join(fields)} WHERE kaek = ?",
            values,
        )


def increment_retry(kaek: str) -> int:
    with db_conn() as conn:
        conn.execute(
            "UPDATE kaek_jobs SET retry_count = retry_count + 1, updated_at = ? WHERE kaek = ?",
            (now_iso(), kaek),
        )
        row = conn.execute(
            "SELECT retry_count FROM kaek_jobs WHERE kaek = ?", (kaek,)
        ).fetchone()
    return row["retry_count"] if row else 0


def get_pending_kaeks() -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM kaek_jobs WHERE status IN ('pending', 'failed') AND retry_count <= ? ORDER BY created_at",
            (Config.MAX_RETRIES,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_kaeks(page: int = 1, per_page: int = 100) -> tuple[list[dict], int]:
    offset = (page - 1) * per_page
    with db_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM kaek_jobs").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM kaek_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def get_dashboard_stats() -> dict:
    with db_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'pending'    THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) as processing,
                SUM(CASE WHEN status = 'completed'  THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'partial'    THEN 1 ELSE 0 END) as partial,
                SUM(CASE WHEN status = 'failed'     THEN 1 ELSE 0 END) as failed,
                SUM(pdf_perigrafiki_ok)  as pdf_perigrafiki_count,
                SUM(pdf_xoriki_ok)       as pdf_xoriki_count
            FROM kaek_jobs
        """).fetchone()
    return dict(row)


def delete_kaek(kaek: str) -> None:
    with db_conn() as conn:
        conn.execute("DELETE FROM kaek_jobs WHERE kaek = ?", (kaek,))


def reset_kaek_to_pending(kaek: str) -> None:
    with db_conn() as conn:
        conn.execute(
            "UPDATE kaek_jobs SET status='pending', retry_count=0, failure_reason=NULL, updated_at=? WHERE kaek=?",
            (now_iso(), kaek),
        )


# ── Audit Logs ─────────────────────────────────────────────────────────────────

def add_log(kaek: Optional[str], step: str, status: str, message: str = "") -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO audit_logs (kaek, step, status, message, created_at) VALUES (?,?,?,?,?)",
            (kaek, step, status, message, now_iso()),
        )


def get_logs(
    kaek: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    with db_conn() as conn:
        if kaek:
            rows = conn.execute(
                "SELECT * FROM audit_logs WHERE kaek=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (kaek, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


# ── PDF Parse Results ───────────────────────────────────────────────────────────

def upsert_parse_result(
    kaek: str,
    pdf_type: str,
    data: dict,
) -> None:
    t = now_iso()
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO pdf_parse_results
                (kaek, pdf_type, raw_text, area_sqm, area_stremma,
                 has_building, has_burdens, property_type, ownership_info,
                 burdens_detail, notes, confidence, parse_method, parsed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(kaek, pdf_type) DO UPDATE SET
                 raw_text=excluded.raw_text,
                 area_sqm=excluded.area_sqm,
                 area_stremma=excluded.area_stremma,
                 has_building=excluded.has_building,
                 has_burdens=excluded.has_burdens,
                 property_type=excluded.property_type,
                 ownership_info=excluded.ownership_info,
                 burdens_detail=excluded.burdens_detail,
                 notes=excluded.notes,
                 confidence=excluded.confidence,
                 parse_method=excluded.parse_method,
                 parsed_at=excluded.parsed_at""",
            (
                kaek, pdf_type,
                data.get("raw_text", ""),
                data.get("area_sqm"),
                data.get("area_stremma"),
                data.get("has_building", "unknown"),
                data.get("has_burdens", "unknown"),
                data.get("property_type"),
                data.get("ownership_info"),
                data.get("burdens_detail"),
                data.get("notes"),
                data.get("confidence", 0.0),
                data.get("parse_method", "text"),
                t,
            ),
        )


def get_parse_results(kaek: str) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pdf_parse_results WHERE kaek=?", (kaek,)
        ).fetchall()
    return [dict(r) for r in rows]


def search_parse_results(
    area_name: str = "",
    min_area: Optional[float] = None,
    max_area: Optional[float] = None,
    has_building: str = "any",
    has_burdens: str = "any",
    only_completed: bool = True,
    kaek_pattern: str = "",
) -> list[dict]:
    clauses = ["1=1"]
    params: list = []

    if min_area is not None:
        clauses.append("p.area_sqm >= ?")
        params.append(min_area)
    if max_area is not None:
        clauses.append("p.area_sqm <= ?")
        params.append(max_area)
    if has_building != "any":
        clauses.append("p.has_building = ?")
        params.append(has_building)
    if has_burdens != "any":
        clauses.append("p.has_burdens = ?")
        params.append(has_burdens)
    if only_completed:
        clauses.append("j.status IN ('completed', 'partial')")
    if kaek_pattern:
        clauses.append("p.kaek LIKE ?")
        params.append(f"%{kaek_pattern}")

    where = " AND ".join(clauses)
    sql = f"""
        SELECT p.*, j.status as job_status,
               j.pdf_perigrafiki_path, j.pdf_xoriki_path
        FROM pdf_parse_results p
        JOIN kaek_jobs j ON j.kaek = p.kaek
        WHERE p.pdf_type = 'perigrafiki' AND {where}
        ORDER BY p.area_sqm DESC
    """
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Search Sessions ────────────────────────────────────────────────────────────

def create_session(data: dict) -> int:
    t = now_iso()
    with db_conn() as conn:
        cur = conn.execute(
            """INSERT INTO search_sessions
               (area_name, map_url, min_area_sqm, max_area_sqm,
                has_building, has_burdens, kaek_pattern, status, created_at)
               VALUES (?,?,?,?,?,?,?,'created',?)""",
            (
                data.get("area_name"), data.get("map_url"),
                data.get("min_area_sqm"), data.get("max_area_sqm"),
                data.get("has_building", "any"), data.get("has_burdens", "any"),
                data.get("kaek_pattern"), t,
            ),
        )
    return cur.lastrowid
