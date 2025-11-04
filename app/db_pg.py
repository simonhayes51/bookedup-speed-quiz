# app/db_pg.py
import os
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from contextlib import contextmanager

# Railway Postgres plugin provides DATABASE_URL automatically
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set. Add Railway Postgres and redeploy.")

_pool: SimpleConnectionPool | None = None

def init_db():
    """Initialize the global pool and ensure schema exists."""
    global _pool
    if _pool is None:
        # minconn=1, maxconn=10 is fine for a small app; tune if needed
        _pool = SimpleConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)

    # Create schema if missing
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','host'))
        );
        CREATE TABLE IF NOT EXISTS venues(
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            logo_url TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS hosts_venues(
            host_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
            UNIQUE(host_id, venue_id)
        );
        CREATE TABLE IF NOT EXISTS quizzes(
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        """)
        conn.commit()
    return True

@contextmanager
def get_conn():
    """Yields a pooled connection; returns it to the pool automatically."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized. Call init_db() first.")
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)
