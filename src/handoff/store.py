"""SQLite 存储：事件日志（事实源）+ 读模型投影。

事件表保证两条不变量：
- event_id 唯一 —— 同一事件重复接入不产生第二次状态变更；
- (aggregate_type, aggregate_id, version) 唯一 —— 聚合内事件序号严格递增。

投影表由服务层在写入事件的同一事务内更新，供视图直接查询。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    UNIQUE (aggregate_type, aggregate_id, version)
);

CREATE TABLE IF NOT EXISTS institutions (
    institution_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    level TEXT NOT NULL,
    capabilities TEXT NOT NULL DEFAULT '[]',
    phone TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    patient_ref TEXT NOT NULL,
    source_institution TEXT NOT NULL,
    receiving_institution TEXT,
    current_offer_id TEXT,
    responsibility_id TEXT,
    expected_arrival_start TEXT,
    expected_arrival_end TEXT,
    plan_version INTEGER NOT NULL,
    medication_version TEXT NOT NULL,
    recheck_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    offer_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    institution TEXT NOT NULL,
    status TEXT NOT NULL,
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    responded_at TEXT
);

CREATE TABLE IF NOT EXISTS responsibilities (
    responsibility_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    institution TEXT NOT NULL,
    person TEXT NOT NULL,
    serviceable_from TEXT NOT NULL,
    serviceable_to TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);

-- 责任台账：每一段时间由谁负责，用于责任空档识别
CREATE TABLE IF NOT EXISTS resp_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    institution TEXT NOT NULL,
    person TEXT,
    reason TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    serviceable_to TEXT
);

CREATE TABLE IF NOT EXISTS disruptions (
    disruption_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    resolved_at TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS followups (
    followup_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

-- 计划版本分发台账：哪个机构持有哪一版计划/用药/复查
CREATE TABLE IF NOT EXISTS distributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    institution TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    medication_version TEXT NOT NULL,
    recheck_version TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);

-- 计划修订通知：送达前旧版持有者不得视为已知悉
CREATE TABLE IF NOT EXISTS notices (
    notice_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    institution TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS reescalations (
    reescalation_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    target_institution TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""


class Store:
    """事件与投影的 SQLite 实现；path 传 ":memory:" 可用于测试。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # ---- 基础读写 ----

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params)]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ---- 事件日志 ----

    def event_exists(self, event_id: str) -> bool:
        return (
            self.one("SELECT event_id FROM events WHERE event_id = ?", (event_id,))
            is not None
        )

    def next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        row = self.one(
            "SELECT MAX(version) AS v FROM events "
            "WHERE aggregate_type = ? AND aggregate_id = ?",
            (aggregate_type, aggregate_id),
        )
        return (row["v"] or 0) + 1

    def append_event(self, envelope: dict) -> None:
        self.execute(
            "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,"
            " occurred_at, version, summary, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                envelope["event_id"],
                envelope["event_type"],
                envelope["aggregate_type"],
                envelope["aggregate_id"],
                envelope["occurred_at"],
                envelope["version"],
                envelope["summary"],
                json.dumps(envelope.get("payload", {}), ensure_ascii=False),
            ),
        )

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM events WHERE aggregate_type = ? AND aggregate_id = ?"
            " ORDER BY version",
            (aggregate_type, aggregate_id),
        )
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows


def decode(row: dict[str, Any] | None, *json_fields: str) -> dict[str, Any] | None:
    """把投影行里的 JSON 列还原为对象，便于服务层使用。"""
    if row is None:
        return None
    for field in json_fields:
        row[field] = json.loads(row[field])
    return row
