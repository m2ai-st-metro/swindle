"""Swindle state database manager."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


class SwindleDB:
    """SQLite database for tracking listing packages."""

    def __init__(self, db_path: str = "data/swindle.db"):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Connect to database."""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def init_db(self):
        """Initialize database schema."""
        self.connect()
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT UNIQUE,
                repo_url TEXT,
                title TEXT,
                status TEXT DEFAULT 'staged',
                spec_path TEXT,
                staging_dir TEXT,
                reason TEXT,
                linkedin_draft_path TEXT,
                gumroad_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP
            )
        """)
        self.conn.commit()
        # Migrate existing DBs: add columns if they don't exist
        self._migrate_add_column("linkedin_draft_path", "TEXT")
        self._migrate_add_column("gumroad_url", "TEXT")

    def _migrate_add_column(self, column_name: str, column_type: str):
        """Add a column to listings table if it doesn't already exist."""
        self.connect()
        cursor = self.conn.execute("PRAGMA table_info(listings)")
        existing_columns = {row["name"] for row in cursor.fetchall()}
        if column_name not in existing_columns:
            self.conn.execute(
                f"ALTER TABLE listings ADD COLUMN {column_name} {column_type}"
            )
            self.conn.commit()

    def add_listing(
        self,
        repo_name: str,
        repo_url: str,
        title: str,
        spec_path: Optional[str],
        staging_dir: str,
    ) -> int:
        """Add a new staged listing. Returns the row id."""
        self.connect()
        cursor = self.conn.execute(
            """INSERT OR REPLACE INTO listings
               (repo_name, repo_url, title, status, spec_path, staging_dir, created_at)
               VALUES (?, ?, ?, 'staged', ?, ?, ?)""",
            (repo_name, repo_url, title, spec_path, staging_dir,
             datetime.now().isoformat()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_staged(self) -> list[dict]:
        """Get all listings with status 'staged'."""
        self.connect()
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE status = 'staged' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_all(self) -> list[dict]:
        """Get all listings."""
        self.connect()
        rows = self.conn.execute(
            "SELECT * FROM listings ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_by_name(self, repo_name: str) -> Optional[dict]:
        """Get a listing by repo name."""
        self.connect()
        row = self.conn.execute(
            "SELECT * FROM listings WHERE repo_name = ?", (repo_name,)
        ).fetchone()
        return dict(row) if row else None

    def update_status(
        self, repo_name: str, status: str, reason: str = ""
    ) -> bool:
        """Update listing status. Returns True if a row was updated."""
        self.connect()
        cursor = self.conn.execute(
            """UPDATE listings
               SET status = ?, reason = ?, reviewed_at = ?
               WHERE repo_name = ?""",
            (status, reason, datetime.now().isoformat(), repo_name),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def update_linkedin_draft_path(self, repo_name: str, path: str) -> bool:
        """Store the linkedin draft path for a listing."""
        self.connect()
        cursor = self.conn.execute(
            "UPDATE listings SET linkedin_draft_path = ? WHERE repo_name = ?",
            (path, repo_name),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def update_gumroad_url(self, repo_name: str, url: str) -> bool:
        """Store the Gumroad URL for a listing."""
        self.connect()
        cursor = self.conn.execute(
            "UPDATE listings SET gumroad_url = ? WHERE repo_name = ?",
            (url, repo_name),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_approved_unpublished(self) -> list[dict]:
        """Get listings with status='approved' that have no gumroad_url yet."""
        self.connect()
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE status = 'approved' "
            "AND (gumroad_url IS NULL OR gumroad_url = '') "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict:
        """Get listing counts by status."""
        self.connect()
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as count FROM listings GROUP BY status"
        ).fetchall()
        stats = {row["status"]: row["count"] for row in rows}
        stats["total"] = sum(stats.values())
        return stats
