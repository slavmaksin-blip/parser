"""SQLite database layer using aiosqlite."""

import json
import aiosqlite
from datetime import datetime

DB_PATH = "parser.db"

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    active     INTEGER DEFAULT 0,
    interval   INTEGER DEFAULT 30
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
    max_listing_age_h   INTEGER DEFAULT NULL,
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


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS)
        await db.execute(CREATE_FILTERS)
        await db.execute(CREATE_SEEN)
        await db.commit()


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
    result["keywords"] = json.loads(result["keywords"] or "[]")
    result["categories"] = json.loads(result["categories"] or "[]")
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
                max_listing_age_h   = ?
            WHERE user_id = ?
            """,
            (
                json.dumps(data.get("keywords", []), ensure_ascii=False),
                json.dumps(data.get("categories", []), ensure_ascii=False),
                data.get("min_price"),
                data.get("max_price"),
                data.get("max_seller_reg_date"),
                data.get("max_listing_age_h"),
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
