import os, sqlite3
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")
def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    conn = connect()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','host'))
    );
    CREATE TABLE IF NOT EXISTS venues(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        logo_url TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS hosts_venues(
        host_id INTEGER NOT NULL,
        venue_id INTEGER NOT NULL,
        UNIQUE(host_id, venue_id),
        FOREIGN KEY(host_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(venue_id) REFERENCES venues(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS quizzes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        data_json TEXT NOT NULL
    );
    """)
    conn.commit()
    return conn
