"""SQLite storage (aiosqlite). Every table the bot uses is created here."""
import os

import aiosqlite

conn: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS seen (
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (kind, item_id)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    due_ts REAL NOT NULL,
    message TEXT NOT NULL,
    created_ts REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'remind',
    confirm_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders (due_ts);
CREATE TABLE IF NOT EXISTS polls (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    options TEXT NOT NULL,
    end_ts REAL NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS poll_votes (
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    option INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS events (
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    start_ts REAL NOT NULL,
    remind INTEGER NOT NULL DEFAULT 1,
    notified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, event_id)
);
"""


async def init(path: str) -> None:
    global conn
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute('PRAGMA foreign_keys = ON')
    await conn.executescript(SCHEMA)
    await _migrate()
    await conn.commit()


# Columns added after the first release: (table, column, definition). Older databases get them on startup.
MIGRATIONS = [
    ('reminders', 'kind', "TEXT NOT NULL DEFAULT 'remind'"),
    ('reminders', 'confirm_message_id', 'INTEGER'),
]


async def _migrate() -> None:
    for table, column, definition in MIGRATIONS:
        async with conn.execute(f'PRAGMA table_info({table})') as cur:
            existing = {row['name'] for row in await cur.fetchall()}
        if column not in existing:
            await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


async def close() -> None:
    if conn:
        await conn.close()


async def get_kv(key: str) -> str | None:
    async with conn.execute('SELECT value FROM kv WHERE key = ?', (key,)) as cur:
        row = await cur.fetchone()
    return row['value'] if row else None


async def set_kv(key: str, value: str) -> None:
    await conn.execute('INSERT INTO kv (key, value) VALUES (?, ?) '
                       'ON CONFLICT (key) DO UPDATE SET value = excluded.value', (key, value))
    await conn.commit()


async def filter_unseen(kind: str, item_ids: list[str]) -> list[str]:
    """Return the ids that have not been marked seen yet (keeps input order)."""
    if not item_ids:
        return []
    marks = ','.join('?' * len(item_ids))
    async with conn.execute(f'SELECT item_id FROM seen WHERE kind = ? AND item_id IN ({marks})',
                            (kind, *item_ids)) as cur:
        seen = {row['item_id'] for row in await cur.fetchall()}
    return [i for i in item_ids if i not in seen]


async def mark_seen(kind: str, item_ids: list[str], ts: float) -> None:
    await conn.executemany('INSERT OR IGNORE INTO seen (kind, item_id, ts) VALUES (?, ?, ?)',
                           [(kind, i, ts) for i in item_ids])
    await conn.commit()
