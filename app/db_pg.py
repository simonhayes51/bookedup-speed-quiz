# app/db_pg.py
import os
from contextlib import contextmanager
from psycopg_pool import ConnectionPool

pool = None  # created in init_db()

def init_db():
    global pool
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set. Add Railway Postgres or let main.py fall back to SQLite.")
    if pool is None:
        pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, timeout=30)
    # ensure schema
    with get_conn() as conn:
        with conn.cursor() as cur:
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
    if pool is None:
        raise RuntimeError("Postgres pool not initialised. Call init_db() first.")
    with pool.connection() as conn:
        yield conn
