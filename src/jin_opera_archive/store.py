"""SQLite 存储层：追加式事件日志与分实体表。

设计原则：
- 每类档案事实独立成表——文件指纹、拍摄批次、地点沿革、剧种分类版本、
  剧目、人物与团体别名、说明文本、资料出处、许可、使用留痕、候选关系。
- 一切修改先写 events 事件日志（操作者、时间、依据、出处），再落实体表；
  事件只增不改，保证从任一条记录都能回到最初说明与每次勘误依据。
- 说明文本、别名、地名等均保存全部历史版本，当前答案只是最新一行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    asset_id    TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',
    rationale   TEXT NOT NULL DEFAULT '',
    source_ids  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_events_asset ON events(asset_id);

CREATE TABLE IF NOT EXISTS imports (
    import_key  TEXT PRIMARY KEY,
    actor       TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    photographer TEXT NOT NULL DEFAULT '',
    started_at   TEXT,
    ended_at     TEXT,
    notes        TEXT NOT NULL DEFAULT '',
    import_key   TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id     TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL UNIQUE,
    media_kind   TEXT NOT NULL DEFAULT 'digital',
    batch_id     TEXT,
    captured_at  TEXT,
    publisher_id TEXT
);

CREATE TABLE IF NOT EXISTS places (
    place_id   TEXT PRIMARY KEY,
    merged_into TEXT
);
CREATE TABLE IF NOT EXISTS place_names (
    place_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    normalized TEXT NOT NULL,
    valid_from TEXT,
    valid_to   TEXT,
    source_id  TEXT,
    PRIMARY KEY (place_id, normalized, valid_from)
);
CREATE TABLE IF NOT EXISTS asset_places (
    asset_id  TEXT NOT NULL,
    place_id  TEXT NOT NULL,
    source_id TEXT,
    PRIMARY KEY (asset_id, place_id)
);

CREATE TABLE IF NOT EXISTS genres (
    genre_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    normalized TEXT NOT NULL,
    merged_into TEXT
);
CREATE TABLE IF NOT EXISTS genre_aliases (
    genre_id   TEXT NOT NULL,
    alias      TEXT NOT NULL,
    normalized TEXT NOT NULL,
    PRIMARY KEY (genre_id, normalized)
);
CREATE TABLE IF NOT EXISTS genre_schemes (
    scheme_id TEXT PRIMARY KEY,
    title     TEXT NOT NULL,
    issued_at TEXT NOT NULL DEFAULT '',
    note      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS genre_scheme_entries (
    scheme_id       TEXT NOT NULL,
    genre_id        TEXT NOT NULL,
    parent_genre_id TEXT,
    note            TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scheme_id, genre_id)
);
CREATE TABLE IF NOT EXISTS asset_genres (
    asset_id  TEXT NOT NULL,
    genre_id  TEXT NOT NULL,
    scheme_id TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'active',
    source_id TEXT,
    PRIMARY KEY (asset_id, genre_id, scheme_id)
);

CREATE TABLE IF NOT EXISTS plays (
    play_id    TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    merged_into TEXT
);
CREATE TABLE IF NOT EXISTS play_aliases (
    play_id    TEXT NOT NULL,
    alias      TEXT NOT NULL,
    normalized TEXT NOT NULL,
    PRIMARY KEY (play_id, normalized)
);
CREATE TABLE IF NOT EXISTS asset_plays (
    asset_id  TEXT NOT NULL,
    play_id   TEXT NOT NULL,
    source_id TEXT,
    PRIMARY KEY (asset_id, play_id)
);

CREATE TABLE IF NOT EXISTS persons (
    person_id    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    merged_into  TEXT
);
CREATE TABLE IF NOT EXISTS person_aliases (
    person_id  TEXT NOT NULL,
    alias      TEXT NOT NULL,
    normalized TEXT NOT NULL,
    source_id  TEXT,
    PRIMARY KEY (person_id, normalized)
);
CREATE TABLE IF NOT EXISTS troupes (
    troupe_id    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'troupe',
    merged_into  TEXT
);
CREATE TABLE IF NOT EXISTS troupe_aliases (
    troupe_id  TEXT NOT NULL,
    alias      TEXT NOT NULL,
    normalized TEXT NOT NULL,
    valid_from TEXT,
    valid_to   TEXT,
    source_id  TEXT,
    PRIMARY KEY (troupe_id, normalized)
);
CREATE TABLE IF NOT EXISTS person_troupes (
    person_id  TEXT NOT NULL,
    troupe_id  TEXT NOT NULL,
    genre_id   TEXT,
    valid_from TEXT,
    valid_to   TEXT,
    source_id  TEXT,
    PRIMARY KEY (person_id, troupe_id, valid_from)
);

CREATE TABLE IF NOT EXISTS asset_persons (
    asset_id  TEXT NOT NULL,
    person_id TEXT NOT NULL,
    role      TEXT NOT NULL,
    source_id TEXT,
    PRIMARY KEY (asset_id, person_id, role)
);
CREATE TABLE IF NOT EXISTS asset_troupes (
    asset_id  TEXT NOT NULL,
    troupe_id TEXT NOT NULL,
    source_id TEXT,
    PRIMARY KEY (asset_id, troupe_id)
);

CREATE TABLE IF NOT EXISTS mentions (
    mention_id      TEXT PRIMARY KEY,
    asset_id        TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    alias_text      TEXT NOT NULL,
    normalized      TEXT NOT NULL,
    role            TEXT,
    status          TEXT NOT NULL DEFAULT 'unconfirmed',
    resolved_entity TEXT,
    source_id       TEXT,
    import_key      TEXT
);
CREATE INDEX IF NOT EXISTS idx_mentions_asset ON mentions(asset_id);
CREATE INDEX IF NOT EXISTS idx_mentions_norm ON mentions(normalized);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id  TEXT PRIMARY KEY,
    entity_type   TEXT NOT NULL,
    left_type     TEXT NOT NULL,
    left_id       TEXT NOT NULL,
    right_type    TEXT NOT NULL,
    right_id      TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    decision_note TEXT NOT NULL DEFAULT '',
    UNIQUE (entity_type, left_type, left_id, right_type, right_id)
);

CREATE TABLE IF NOT EXISTS captions (
    caption_id TEXT PRIMARY KEY,
    asset_id   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    basis      TEXT NOT NULL DEFAULT '',
    author     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (asset_id, version)
);
CREATE TABLE IF NOT EXISTS caption_sources (
    caption_id TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    PRIMARY KEY (caption_id, source_id)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    citation     TEXT NOT NULL,
    collector    TEXT NOT NULL DEFAULT '',
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS licenses (
    license_id   TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    purpose      TEXT NOT NULL,
    asset_id     TEXT,
    granted_at   TEXT NOT NULL,
    withdrawn_at TEXT,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS usages (
    usage_id    TEXT PRIMARY KEY,
    asset_id    TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    venue       TEXT NOT NULL DEFAULT '',
    used_at     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    license_ids TEXT NOT NULL DEFAULT '[]',
    note        TEXT NOT NULL DEFAULT ''
);
"""


def utcnow() -> str:
    """返回 UTC 当前时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成随机主键（人工创建的权威实体使用）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def content_id(prefix: str, *parts: str) -> str:
    """按内容生成确定性主键：同一内容重复导入得到同一 ID，是幂等的基础。"""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class Store:
    """对 sqlite3 的薄封装：事务、查询与事件日志。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """事务上下文：内部任意一步失败都会整体回滚，保证导入可安全重试。"""
        with self._lock:
            try:
                yield
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def event(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
        rationale: str = "",
        source_ids: list[str] | tuple = (),
        asset_id: str | None = None,
        at: str | None = None,
    ) -> str:
        """追加一条审计事件。事件永不更新、永不删除。"""
        event_id = new_id("evt")
        self.run(
            "INSERT INTO events(event_id, ts, actor, action, entity_type, entity_id,"
            " asset_id, payload, rationale, source_ids) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                at or utcnow(),
                actor,
                action,
                entity_type,
                entity_id,
                asset_id,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                rationale,
                json.dumps(list(source_ids), ensure_ascii=False),
            ),
        )
        return event_id
