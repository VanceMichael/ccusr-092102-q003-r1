"""JSON 文档存储：原子写入、稳定编号、名称索引与导入台账。

所有记录都以普通 dict 保存，便于直接序列化为 JSON。
写操作先改内存再原子落盘（临时文件 + os.replace），避免离线回传中途断电产生半截文件。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import (
    IDENTITY_CONFIRMED,
    IDENTITY_MERGED,
    normalize_name,
)

COLLECTIONS = (
    "batches",
    "assets",
    "persons",
    "organizations",
    "places",
    "genres",
    "plays",
    "participations",
    "caption_versions",
    "genre_versions",
    "place_history",
    "candidates",
    "licenses",
    "license_events",
    "usages",
)


class Store:
    """持有全部档案记录的线程安全 JSON 仓储。"""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"seq": 0}
        for name in COLLECTIONS:
            self.data[name] = {}
        # 导入台账：(批次编号, 客户端键) -> asset_id
        self.data["import_ledger"] = {}
        # 文件指纹 -> asset_id，跨批次查重
        self.data["fingerprint_index"] = {}
        self._name_index: dict[tuple[str, str], set[str]] = {}
        if self.path.exists():
            self._load()
            self._rebuild_name_index()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value in loaded.items():
            self.data[key] = value
        for name in COLLECTIONS:
            self.data.setdefault(name, {})
        self.data.setdefault("import_ledger", {})
        self.data.setdefault("fingerprint_index", {})

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ------------------------------------------------------------------ 编号

    def next_id(self, prefix: str) -> str:
        self.data["seq"] += 1
        return f"{prefix}-{self.data['seq']:05d}"

    # ------------------------------------------------------------------ 通用访问

    def collection(self, name: str) -> dict[str, dict[str, Any]]:
        return self.data[name]

    def all(self, name: str) -> Iterable[dict[str, Any]]:
        return self.data[name].values()

    def get(self, name: str, record_id: str) -> dict[str, Any] | None:
        return self.data[name].get(record_id)

    def require(self, name: str, record_id: str) -> dict[str, Any]:
        record = self.data[name].get(record_id)
        if record is None:
            raise NotFound(name, record_id)
        return record

    def insert(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        if record["id"] in self.data[name]:
            raise Conflict(f"{name} 编号已存在: {record['id']}")
        self.data[name][record["id"]] = record
        return record

    # ------------------------------------------------------------------ 名称索引

    def _entity_terms(self, record: dict[str, Any]) -> list[str]:
        terms = [record["name"]]
        terms.extend(record.get("aliases", []))
        return [normalize_name(t) for t in terms if t]

    def _rebuild_name_index(self) -> None:
        self._name_index.clear()
        for kind, coll in (
            ("person", "persons"),
            ("organization", "organizations"),
            ("place", "places"),
            ("genre", "genres"),
            ("play", "plays"),
        ):
            for record in self.all(coll):
                for term in self._entity_terms(record):
                    self._name_index.setdefault((kind, term), set()).add(record["id"])

    def index_name(self, kind: str, record: dict[str, Any]) -> None:
        for term in self._entity_terms(record):
            self._name_index.setdefault((kind, term), set()).add(record["id"])

    def canonical(self, kind: str, record_id: str) -> str:
        """沿 merged_into 链解析到当前有效记录。"""
        coll = {
            "person": "persons",
            "organization": "organizations",
            "place": "places",
            "genre": "genres",
            "play": "plays",
        }[kind]
        seen: set[str] = set()
        current = record_id
        while True:
            record = self.get(coll, current)
            if record is None or record.get("status") != IDENTITY_MERGED:
                return current
            if current in seen:
                return current
            seen.add(current)
            current = record["merged_into"]

    def resolve_name(self, kind: str, name: str) -> dict[str, Any]:
        """按名称（含别名、曾用名）解析实体。

        返回：
          {"state": "resolved", "id"}            唯一匹配
          {"state": "ambiguous", "ids": [...]}   多名重名，需人工候选
          {"state": "unresolved"}                无匹配
        """
        term = normalize_name(name)
        raw_ids = self._name_index.get((kind, term), set())
        ids = sorted({self.canonical(kind, rid) for rid in raw_ids})
        if len(ids) == 1:
            return {"state": "resolved", "id": ids[0]}
        if len(ids) > 1:
            return {"state": "ambiguous", "ids": ids}
        return {"state": "unresolved"}

    # ------------------------------------------------------------------ 导入台账

    def ledger_key(self, batch_code: str, client_ref: str) -> str:
        return f"{batch_code}||{client_ref}"

    def ledger_lookup(self, batch_code: str, client_ref: str) -> str | None:
        return self.data["import_ledger"].get(self.ledger_key(batch_code, client_ref))

    def ledger_record(
        self, batch_code: str, client_ref: str, asset_id: str, fingerprint: str
    ) -> None:
        self.data["import_ledger"][self.ledger_key(batch_code, client_ref)] = asset_id
        self.data["fingerprint_index"].setdefault(fingerprint, asset_id)

    def fingerprint_lookup(self, fingerprint: str) -> str | None:
        return self.data["fingerprint_index"].get(fingerprint)


class ArchiveError(Exception):
    """档案业务错误基类。"""


class NotFound(ArchiveError):
    def __init__(self, kind: str, record_id: str):
        super().__init__(f"未找到 {kind}: {record_id}")
        self.kind = kind
        self.record_id = record_id


class Conflict(ArchiveError):
    """数据冲突，例如重复编号或同一名称解析出多个对象。"""


class ValidationError(ArchiveError):
    """缺少必填字段或字段不合法。"""


FINGERPRINT_RE = re.compile(r"^[a-zA-Z0-9]+:[A-Za-z0-9._=-]+$")
