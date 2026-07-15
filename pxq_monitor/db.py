"""SQLite 持久化：监控项、场次状态与票档状态快照。"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id       TEXT NOT NULL,
    show_name     TEXT DEFAULT '',
    price_filter  TEXT DEFAULT '',
    interval_sec  INTEGER DEFAULT 60,
    status        TEXT DEFAULT 'active',
    session_id    TEXT DEFAULT '',
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS session_state (
    watch_id       INTEGER,
    session_id     TEXT,
    session_name   TEXT DEFAULT '',
    session_status TEXT DEFAULT '',
    updated_at     REAL,
    PRIMARY KEY (watch_id, session_id)
);
CREATE TABLE IF NOT EXISTS plan_state (
    watch_id      INTEGER,
    session_id    TEXT,
    seat_plan_id  TEXT,
    plan_name     TEXT DEFAULT '',
    price         REAL DEFAULT 0,
    can_buy_count INTEGER DEFAULT 0,
    sale_started  INTEGER DEFAULT 0,
    updated_at    REAL,
    PRIMARY KEY (watch_id, seat_plan_id)
);
"""

SESSION_UPSERT = (
    "INSERT INTO session_state "
    "(watch_id, session_id, session_name, session_status, updated_at) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(watch_id, session_id) DO UPDATE SET "
    "session_name = CASE WHEN excluded.session_name != '' "
    "THEN excluded.session_name ELSE session_name END, "
    "session_status = excluded.session_status, updated_at = excluded.updated_at"
)

PLAN_UPSERT = (
    "INSERT INTO plan_state "
    "(watch_id, session_id, seat_plan_id, plan_name, price, can_buy_count, "
    "sale_started, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(watch_id, seat_plan_id) DO UPDATE SET "
    "session_id = excluded.session_id, plan_name = excluded.plan_name, "
    "price = excluded.price, can_buy_count = excluded.can_buy_count, "
    "sale_started = excluded.sale_started, updated_at = excluded.updated_at"
)


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # CLI 与守护进程会同时读写，写锁冲突时等待而不是直接报错
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    # ---- 监控项 ----

    def add_watch(
        self,
        show_id: str,
        show_name: str,
        interval_sec: int,
        price_filter: str,
        session_id: str,
    ) -> tuple[int, bool]:
        """原子地添加监控；同一演出场次已存在时返回现有 ID。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._conn.execute(
                "SELECT id FROM watches WHERE show_id = ? AND session_id = ? "
                "ORDER BY id LIMIT 1",
                (show_id, session_id),
            ).fetchone()
            if existing:
                self._conn.commit()
                return existing["id"], False

            cursor = self._conn.execute(
                "INSERT INTO watches "
                "(show_id, show_name, interval_sec, price_filter, session_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    show_id,
                    show_name,
                    interval_sec,
                    price_filter,
                    session_id,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("插入监控项后未返回 ID")
            self._conn.commit()
            return cursor.lastrowid, True
        except BaseException:
            self._conn.rollback()
            raise

    def list_watches(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self._conn.execute(
                "SELECT * FROM watches WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        return self._conn.execute("SELECT * FROM watches ORDER BY id").fetchall()

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM watches WHERE id = ?", (watch_id,)
        ).fetchone()

    def set_watch_status(self, watch_id: int, status: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE watches SET status = ? WHERE id = ?", (status, watch_id)
            )
        return cursor.rowcount > 0

    def set_watch_filter(self, watch_id: int, price_filter: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE watches SET price_filter = ? WHERE id = ?",
                (price_filter, watch_id),
            )
        return cursor.rowcount > 0

    def set_watch_interval(self, watch_id: int, interval_sec: int) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE watches SET interval_sec = ? WHERE id = ?",
                (interval_sec, watch_id),
            )
        return cursor.rowcount > 0

    def remove_watch(self, watch_id: int) -> bool:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
            if cursor.rowcount == 0:
                return False
            self._conn.execute("DELETE FROM plan_state WHERE watch_id = ?", (watch_id,))
            self._conn.execute(
                "DELETE FROM session_state WHERE watch_id = ?", (watch_id,)
            )
        return True

    # ---- 状态快照 ----

    def get_session_state(self, watch_id: int, session_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM session_state WHERE watch_id = ? AND session_id = ?",
            (watch_id, session_id),
        ).fetchone()

    def get_plan_states(self, watch_id: int) -> dict[str, sqlite3.Row]:
        rows = self._conn.execute(
            # rowid 即首次入库顺序 = 接口返回顺序，与 add/pick 展示的编号一致
            "SELECT * FROM plan_state WHERE watch_id = ? ORDER BY rowid",
            (watch_id,),
        ).fetchall()
        return {r["seat_plan_id"]: r for r in rows}

    def apply_snapshot(
        self,
        watch_id: int,
        session_id: str,
        session_name: str,
        session_status: str,
        plans: list[tuple[str, str, float, int, bool]],
    ) -> bool:
        """在一个短事务中写入完整场次快照；监控已停用时拒绝写入。"""
        updated_at = time.time()
        with self._conn:
            # 先获取写锁并确认仍为 active，避免网络等待期间删除/暂停后写回幽灵状态。
            guard = self._conn.execute(
                "UPDATE watches SET status = status WHERE id = ? AND status = 'active'",
                (watch_id,),
            )
            if guard.rowcount == 0:
                return False
            self._conn.execute(
                SESSION_UPSERT,
                (watch_id, session_id, session_name, session_status, updated_at),
            )
            self._conn.executemany(
                PLAN_UPSERT,
                [
                    (
                        watch_id,
                        session_id,
                        seat_plan_id,
                        plan_name,
                        price,
                        can_buy_count,
                        int(sale_started),
                        updated_at,
                    )
                    for seat_plan_id, plan_name, price, can_buy_count, sale_started in plans
                ],
            )
        return True
