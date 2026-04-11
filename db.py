"""SQLite database layer using aiosqlite."""

import json
import aiosqlite
from datetime import datetime, timezone

DB_PATH = "parser.db"

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    active     INTEGER DEFAULT 0,
    interval   INTEGER DEFAULT 60
);
"""

CREATE_FILTERS = """
CREATE TABLE IF NOT EXISTS filters (
    user_id             INTEGER PRIMARY KEY,
    keywords            TEXT    DEFAULT '[]',
    categories          TEXT    DEFAULT '[]',
    min_price           REAL    DEFAULT NULL,
    max_price           REAL    DEFAULT NULL,
    max_seller_reg_date TEXT    DEFAULT NULL,
    min_sold            INTEGER DEFAULT NULL,
    max_sold            INTEGER DEFAULT NULL,
    min_purchases       INTEGER DEFAULT NULL,
    max_purchases       INTEGER DEFAULT NULL,
    listing_date_from   TEXT    DEFAULT NULL,
    listing_date_to     TEXT    DEFAULT NULL,
    listing_type        TEXT    DEFAULT NULL,
    condition           TEXT    DEFAULT NULL,
    location            TEXT    DEFAULT NULL,
    delivery            TEXT    DEFAULT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
"""

CREATE_SEEN = """
CREATE TABLE IF NOT EXISTS seen_listings (
    listing_id TEXT,
    user_id    INTEGER,
    seen_at    TEXT,
    PRIMARY KEY (listing_id, user_id)
);
"""

# Columns added in later migrations (column_name, sql_type, default_expr)
_MIGRATION_COLUMNS = [
    ("min_sold",            "INTEGER", "NULL"),
    ("max_sold",            "INTEGER", "NULL"),
    ("min_purchases",       "INTEGER", "NULL"),
    ("max_purchases",       "INTEGER", "NULL"),
    ("listing_date_from",   "TEXT",    "NULL"),
    ("listing_date_to",     "TEXT",    "NULL"),
    ("listing_type",        "TEXT",    "NULL"),
    ("condition",           "TEXT",    "NULL"),
    ("location",            "TEXT",    "NULL"),
    ("delivery",            "TEXT",    "NULL"),
]

# Old columns that existed in earlier schema — we just ignore errors if they
# don't exist or can't be dropped in SQLite.
_OBSOLETE_COLUMNS = [
    "max_listing_age_h",
]


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS)
        await db.execute(CREATE_FILTERS)
        await db.execute(CREATE_SEEN)
        await db.commit()
    await _migrate_db()


async def _migrate_db() -> None:
    """Add new columns to existing databases without breaking fresh installs."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Migrate users table: ensure interval column exists with new default
        try:
            await db.execute("ALTER TABLE users ADD COLUMN interval INTEGER DEFAULT 60")
            await db.commit()
        except Exception:
            pass

        for col, col_type, default in _MIGRATION_COLUMNS:
            try:
                await db.execute(
                    f"ALTER TABLE filters ADD COLUMN {col} {col_type} DEFAULT {default}"
                )
                await db.commit()
            except Exception:
                pass  # column already exists


async def ensure_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await db.execute(
            "INSERT OR IGNORE INTO filters (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()


async def get_filters(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM filters WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return {}
    result = dict(row)
    result["keywords"] = json.loads(result.get("keywords") or "[]")
    result["categories"] = json.loads(result.get("categories") or "[]")
    return result


async def save_filters(user_id: int, data: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE filters SET
                keywords            = ?,
                categories          = ?,
                min_price           = ?,
                max_price           = ?,
                max_seller_reg_date = ?,
                min_sold            = ?,
                max_sold            = ?,
                min_purchases       = ?,
                max_purchases       = ?,
                listing_date_from   = ?,
                listing_date_to     = ?,
                listing_type        = ?,
                condition           = ?,
                location            = ?,
                delivery            = ?
            WHERE user_id = ?
            """,
            (
                json.dumps(data.get("keywords", []), ensure_ascii=False),
                json.dumps(data.get("categories", []), ensure_ascii=False),
                data.get("min_price"),
                data.get("max_price"),
                data.get("max_seller_reg_date"),
                data.get("min_sold"),
                data.get("max_sold"),
                data.get("min_purchases"),
                data.get("max_purchases"),
                data.get("listing_date_from"),
                data.get("listing_date_to"),
                data.get("listing_type"),
                data.get("condition"),
                data.get("location"),
                data.get("delivery"),
                user_id,
            ),
        )
        await db.commit()


async def set_active(user_id: int, active: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET active = ? WHERE user_id = ?",
            (1 if active else 0, user_id),
        )
        await db.commit()


async def get_active_users() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM users WHERE active = 1"
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def is_active(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT active FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0])


async def mark_seen(user_id: int, listing_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_listings VALUES (?, ?, ?)",
            (listing_id, user_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def is_seen(user_id: int, listing_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_listings WHERE listing_id = ? AND user_id = ?",
            (listing_id, user_id),
        ) as cur:
            return await cur.fetchone() is not None


async def cleanup_old_seen(days: int = 30) -> None:
    """Remove seen entries older than *days* days to keep DB small."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM seen_listings WHERE seen_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.commit()


async def get_interval(user_id: int) -> int:
    """Return the polling interval in seconds for a user (default 60)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT interval FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    if row and row[0]:
        return int(row[0])
    return 60
