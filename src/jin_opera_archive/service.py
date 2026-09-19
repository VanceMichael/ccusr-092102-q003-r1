"""晋戏影像沿革档案的核心服务逻辑。

覆盖六条档案纪律：
1. 指纹、批次、地点沿革、剧种分类版本、剧目、别名、说明、出处分别保存；
2. 田野离线回传与重复导入幂等——import_key 台账 + 内容寻址主键；
3. 疑似同一人物、戏班或场所只生成候选关系，是否合并由馆员人工判断；
4. 许可按作者、演员、院团、出版方与用途核对；撤回仅限制未来使用，
   已经发生的出版与展出继续留痕；
5. 研究者按剧种检索时只能看到获准内容，但能识别未确认身份与受限影像；
6. 馆员可从任一条记录回到最初说明，并查看每次勘误的依据。
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from .store import Store, content_id, new_id, utcnow

# 许可用途：展览、研究、网络传播、出版
PURPOSES = ("exhibition", "research", "online", "publication")
# 许可主体：作者、演员、院团、出版方
SUBJECT_TYPES = ("author", "performer", "troupe", "publisher")
# 可生成候选关系的实体类型
ENTITY_TYPES = ("person", "troupe", "place")
ENTITY_TYPE_LABELS = {"person": "人物", "troupe": "戏班/团体", "place": "场所"}
# 视为“作者”的出镜角色
AUTHOR_ROLES = ("摄影", "作者", "photographer", "author")
PERFORMER_ROLES = ("演员", "performer")
VIEWERS = ("researcher", "archivist")

DEFAULT_SCHEME_ID = "scheme-original"
DEFAULT_SCHEME_TITLE = "原始著录（未指定分类方案）"


class ArchiveError(Exception):
    """服务层错误基类。"""


class NotFoundError(ArchiveError):
    """记录不存在。"""


class ValidationError(ArchiveError):
    """请求参数不合法。"""


class NotAuthorizedError(ArchiveError):
    """许可不足，拒绝登记使用。"""

    def __init__(self, missing: list[dict[str, Any]]) -> None:
        super().__init__("许可不足")
        self.missing = missing


def normalize_name(text: str) -> str:
    """名称归一化：全半角统一、去空白、小写，用于别名比对。"""
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(normalized.split()).casefold()


class ArchiveService:
    """档案服务门面：所有读写都经过它，保证审计与幂等。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.store = Store(db_path)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 通用小工具
    # ------------------------------------------------------------------

    def _get(self, table: str, key: str, value: str, label: str) -> dict[str, Any]:
        row = self.store.one(f"SELECT * FROM {table} WHERE {key}=?", (value,))
        if row is None:
            raise NotFoundError(f"{label}不存在: {value}")
        return row

    def _require_active(self, row: dict[str, Any], label: str) -> None:
        if row.get("merged_into"):
            raise ValidationError(f"{label}已并入 {row['merged_into']}，请操作并入后的记录")

    # ------------------------------------------------------------------
    # 资料出处
    # ------------------------------------------------------------------

    def register_source(
        self,
        kind: str,
        citation: str,
        actor: str,
        collector: str = "",
        collected_at: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """登记资料出处（胶片卡片、口述、出版物等）。同一出处重复登记幂等。"""
        if not citation:
            raise ValidationError("出处引文不能为空")
        source_id = content_id("src", kind, citation)
        with self.store.tx():
            row = self.store.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
            if row is None:
                self.store.run(
                    "INSERT INTO sources(source_id, kind, citation, collector, collected_at)"
                    " VALUES(?,?,?,?,?)",
                    (source_id, kind, citation, collector, collected_at),
                )
                self.store.event(
                    actor, "source.register", "source", source_id,
                    {"kind": kind, "citation": citation}, at=at,
                )
                row = self.store.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
        return row

    # ------------------------------------------------------------------
    # 田野批次幂等导入
    # ------------------------------------------------------------------

    def import_field_batch(
        self,
        payload: dict[str, Any],
        *,
        import_key: str,
        actor: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """导入一批田野资料。

        幂等保证：
        - import_key 已登记 → 直接返回首次导入的结果，不产生任何新记录；
        - 文件指纹已存在 → 复用原影像记录，不重复建档；
        - 说明文字与已存版本不一致 → 仅提示冲突，不擅自生成新版本
          （勘误必须走 correct_caption 并写明依据）。
        """
        if not import_key:
            raise ValidationError("import_key 不能为空")
        at = at or utcnow()
        payload_hash = content_id("payload", json.dumps(payload, ensure_ascii=False, sort_keys=True))

        with self.store.tx():
            seen = self.store.one("SELECT result FROM imports WHERE import_key=?", (import_key,))
            if seen is not None:
                result = json.loads(seen["result"])
                result["replayed"] = True
                return result

            source_ids = [
                self._upsert_source_tx(s, actor, at) for s in payload.get("sources", [])
            ]
            batch_id = self._upsert_batch_tx(payload.get("batch", {}), import_key, actor, at)
            scheme_id = payload.get("scheme_id") or self._ensure_default_scheme_tx(actor, at)
            if self.store.one(
                "SELECT scheme_id FROM genre_schemes WHERE scheme_id=?", (scheme_id,)
            ) is None:
                scheme_id = self._ensure_default_scheme_tx(actor, at)

            items: list[dict[str, Any]] = []
            mention_ids: list[str] = []
            candidate_ids: list[str] = []
            for index, item in enumerate(payload.get("items", [])):
                outcome = self._import_item_tx(
                    item, index, batch_id, scheme_id, source_ids, import_key, actor, at
                )
                items.append(outcome["item"])
                mention_ids.extend(outcome["mentions"])
                candidate_ids.extend(outcome["candidates"])

            result = {
                "import_key": import_key,
                "batch_id": batch_id,
                "items": items,
                "mentions": mention_ids,
                "candidates": sorted(set(candidate_ids)),
                "replayed": False,
            }
            self.store.run(
                "INSERT INTO imports(import_key, actor, received_at, payload_hash, result)"
                " VALUES(?,?,?,?,?)",
                (import_key, actor, at, payload_hash, json.dumps(result, ensure_ascii=False)),
            )
            self.store.event(
                actor, "batch.import", "batch", batch_id,
                {"import_key": import_key, "item_count": len(items)},
                source_ids=source_ids, at=at,
            )
            return result

    def get_import(self, import_key: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM imports WHERE import_key=?", (import_key,))
        if row is None:
            raise NotFoundError(f"导入批次不存在: {import_key}")
        return {
            "import_key": row["import_key"],
            "actor": row["actor"],
            "received_at": row["received_at"],
            "payload_hash": row["payload_hash"],
            "result": json.loads(row["result"]),
        }

    def _upsert_source_tx(self, spec: dict[str, Any], actor: str, at: str) -> str:
        source_id = content_id("src", spec.get("kind", ""), spec.get("citation", ""))
        if self.store.one("SELECT source_id FROM sources WHERE source_id=?", (source_id,)) is None:
            self.store.run(
                "INSERT INTO sources(source_id, kind, citation, collector, collected_at)"
                " VALUES(?,?,?,?,?)",
                (
                    source_id,
                    spec.get("kind", ""),
                    spec.get("citation", ""),
                    spec.get("collector", ""),
                    spec.get("collected_at"),
                ),
            )
            self.store.event(
                actor, "source.register", "source", source_id,
                {"kind": spec.get("kind", ""), "citation": spec.get("citation", "")}, at=at,
            )
        return source_id

    def _upsert_batch_tx(
        self, spec: dict[str, Any], import_key: str, actor: str, at: str
    ) -> str:
        batch_id = spec.get("batch_id") or content_id("batch", import_key)
        if self.store.one("SELECT batch_id FROM batches WHERE batch_id=?", (batch_id,)) is None:
            self.store.run(
                "INSERT INTO batches(batch_id, title, photographer, started_at, ended_at,"
                " notes, import_key) VALUES(?,?,?,?,?,?,?)",
                (
                    batch_id,
                    spec.get("title", ""),
                    spec.get("photographer", ""),
                    spec.get("started_at"),
                    spec.get("ended_at"),
                    spec.get("notes", ""),
                    import_key,
                ),
            )
            self.store.event(
                actor, "batch.create", "batch", batch_id, dict(spec), at=at
            )
        return batch_id

    def _ensure_default_scheme_tx(self, actor: str, at: str) -> str:
        if self.store.one(
            "SELECT scheme_id FROM genre_schemes WHERE scheme_id=?", (DEFAULT_SCHEME_ID,)
        ) is None:
            self.store.run(
                "INSERT INTO genre_schemes(scheme_id, title, issued_at, note) VALUES(?,?,?,?)",
                (DEFAULT_SCHEME_ID, DEFAULT_SCHEME_TITLE, "", "田野原始著录所使用的剧种称谓"),
            )
            self.store.event(
                actor, "scheme.create", "genre_scheme", DEFAULT_SCHEME_ID,
                {"title": DEFAULT_SCHEME_TITLE}, at=at,
            )
        return DEFAULT_SCHEME_ID

    def _import_item_tx(
        self,
        item: dict[str, Any],
        index: int,
        batch_id: str,
        scheme_id: str,
        source_ids: list[str],
        import_key: str,
        actor: str,
        at: str,
    ) -> dict[str, Any]:
        fingerprint = item.get("file_fingerprint")
        if not fingerprint:
            raise ValidationError(f"第 {index} 条缺少 file_fingerprint")
        asset_id = content_id("asset", fingerprint)
        warnings: list[str] = []
        created = False

        existing = self.store.one("SELECT asset_id FROM assets WHERE fingerprint=?", (fingerprint,))
        if existing is not None:
            asset_id = existing["asset_id"]
        else:
            created = True
            self.store.run(
                "INSERT INTO assets(asset_id, fingerprint, media_kind, batch_id, captured_at)"
                " VALUES(?,?,?,?,?)",
                (
                    asset_id,
                    fingerprint,
                    item.get("media_kind", "digital"),
                    batch_id,
                    item.get("captured_at"),
                ),
            )
            self.store.event(
                actor, "asset.create", "asset", asset_id,
                {"fingerprint": fingerprint, "media_kind": item.get("media_kind", "digital")},
                source_ids=source_ids, asset_id=asset_id, at=at,
            )

        caption = item.get("caption")
        if caption and created:
            self._insert_caption_tx(
                asset_id,
                1,
                caption.get("text", ""),
                caption.get("basis", "原始著录"),
                caption.get("author", actor),
                source_ids,
                actor,
                at,
            )
        elif caption and not created:
            current = self._latest_caption(asset_id)
            if current is not None and current["text"] != caption.get("text", ""):
                warnings.append("caption_conflict: 与已存说明不一致，请走勘误流程并注明依据")

        genre_label = item.get("genre_label")
        if genre_label:
            genre = self._find_genre(genre_label)
            if genre is None:
                warnings.append(f"genre_unresolved: 未登记的剧种称谓「{genre_label}」")
            else:
                self._assign_genre_tx(
                    asset_id, genre["genre_id"], scheme_id, actor, source_ids, at
                )

        mention_ids: list[str] = []
        candidate_ids: list[str] = []
        if item.get("place_name"):
            mid, cids = self._add_mention_tx(
                asset_id, "place", item["place_name"], None,
                source_ids, import_key, index, actor, at,
            )
            mention_ids.append(mid)
            candidate_ids.extend(cids)
        for person in item.get("persons", []):
            mid, cids = self._add_mention_tx(
                asset_id, "person", person["name"], person.get("role"),
                source_ids, import_key, index, actor, at,
            )
            mention_ids.append(mid)
            candidate_ids.extend(cids)
        for troupe in item.get("troupes", []):
            mid, cids = self._add_mention_tx(
                asset_id, "troupe", troupe["name"], None,
                source_ids, import_key, index, actor, at,
            )
            mention_ids.append(mid)
            candidate_ids.extend(cids)

        for title in item.get("plays", []):
            play_id = self._upsert_play_tx(title, actor, source_ids, at)
            self.store.run(
                "INSERT OR IGNORE INTO asset_plays(asset_id, play_id, source_id) VALUES(?,?,?)",
                (asset_id, play_id, source_ids[0] if source_ids else None),
            )

        return {
            "item": {
                "asset_id": asset_id,
                "created": created,
                "warnings": warnings,
            },
            "mentions": mention_ids,
            "candidates": candidate_ids,
        }

    # ------------------------------------------------------------------
    # 说明文本与勘误
    # ------------------------------------------------------------------

    def _insert_caption_tx(
        self,
        asset_id: str,
        version: int,
        text: str,
        basis: str,
        author: str,
        source_ids: list[str],
        actor: str,
        at: str,
    ) -> str:
        caption_id = content_id("cap", asset_id, str(version))
        self.store.run(
            "INSERT INTO captions(caption_id, asset_id, version, text, basis, author, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (caption_id, asset_id, version, text, basis, author, at),
        )
        for sid in source_ids:
            self.store.run(
                "INSERT OR IGNORE INTO caption_sources(caption_id, source_id) VALUES(?,?)",
                (caption_id, sid),
            )
        self.store.event(
            actor,
            "caption.create" if version == 1 else "caption.correct",
            "caption",
            caption_id,
            {"asset_id": asset_id, "version": version, "text": text},
            rationale=basis,
            source_ids=source_ids,
            asset_id=asset_id,
            at=at,
        )
        return caption_id

    def _latest_caption(self, asset_id: str) -> dict[str, Any] | None:
        return self.store.one(
            "SELECT * FROM captions WHERE asset_id=? ORDER BY version DESC LIMIT 1", (asset_id,)
        )

    def correct_caption(
        self,
        asset_id: str,
        text: str,
        basis: str,
        actor: str,
        source_ids: list[str] | tuple = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        """登记一次说明勘误：生成新版本，旧版本与各自依据全部保留。"""
        self._get("assets", "asset_id", asset_id, "影像记录")
        if not basis.strip():
            raise ValidationError("勘误必须填写依据（胶片卡片、口述、出版物等）")
        if not text.strip():
            raise ValidationError("说明文本不能为空")
        at = at or utcnow()
        with self.store.tx():
            for sid in source_ids:
                self._get("sources", "source_id", sid, "资料出处")
            latest = self._latest_caption(asset_id)
            version = (latest["version"] if latest else 0) + 1
            caption_id = self._insert_caption_tx(
                asset_id, version, text, basis, actor, list(source_ids), actor, at
            )
        return self._caption_detail(self.store.one(
            "SELECT * FROM captions WHERE caption_id=?", (caption_id,)
        ))

    def _caption_detail(self, row: dict[str, Any]) -> dict[str, Any]:
        sources = self.store.all(
            "SELECT s.* FROM caption_sources cs JOIN sources s ON s.source_id=cs.source_id"
            " WHERE cs.caption_id=? ORDER BY s.source_id",
            (row["caption_id"],),
        )
        return {**row, "sources": sources}

    def caption_history(self, asset_id: str) -> list[dict[str, Any]]:
        """说明文本的全部版本：v1 即最初说明，每版都带勘误依据与出处。"""
        self._get("assets", "asset_id", asset_id, "影像记录")
        rows = self.store.all(
            "SELECT * FROM captions WHERE asset_id=? ORDER BY version", (asset_id,)
        )
        return [self._caption_detail(r) for r in rows]

    # ------------------------------------------------------------------
    # 提及与候选关系（疑似同一，只生成候选，人工判断）
    # ------------------------------------------------------------------

    def _add_mention_tx(
        self,
        asset_id: str,
        entity_type: str,
        alias_text: str,
        role: str | None,
        source_ids: list[str],
        import_key: str,
        index: int,
        actor: str,
        at: str,
    ) -> tuple[str, list[str]]:
        normalized = normalize_name(alias_text)
        # 提及按内容寻址：同一影像上同一称谓的重复回传得到同一 ID，天然幂等
        mention_id = content_id("mention", asset_id, entity_type, normalized, role or "")
        if self.store.one(
            "SELECT mention_id FROM mentions WHERE mention_id=?", (mention_id,)
        ) is None:
            self.store.run(
                "INSERT INTO mentions(mention_id, asset_id, entity_type, alias_text, normalized,"
                " role, source_id, import_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    mention_id,
                    asset_id,
                    entity_type,
                    alias_text,
                    normalized,
                    role,
                    source_ids[0] if source_ids else None,
                    import_key,
                ),
            )
            self.store.event(
                actor, "mention.record", "mention", mention_id,
                {"asset_id": asset_id, "entity_type": entity_type, "alias_text": alias_text},
                source_ids=source_ids, asset_id=asset_id, at=at,
            )
        candidate_ids = self._candidates_for_mention_tx(mention_id, actor, at)
        return mention_id, candidate_ids

    def _candidates_for_mention_tx(self, mention_id: str, actor: str, at: str) -> list[str]:
        """为一条提及寻找疑似同一的实体或其他提及，只生成候选关系。"""
        mention = self.store.one("SELECT * FROM mentions WHERE mention_id=?", (mention_id,))
        entity_type = mention["entity_type"]
        pairs: list[tuple[str, str]] = []
        if entity_type == "person":
            rows = self.store.all(
                "SELECT person_id AS eid FROM person_aliases WHERE normalized=?",
                (mention["normalized"],),
            )
        elif entity_type == "troupe":
            rows = self.store.all(
                "SELECT troupe_id AS eid FROM troupe_aliases WHERE normalized=?",
                (mention["normalized"],),
            )
        else:
            rows = self.store.all(
                "SELECT place_id AS eid FROM place_names WHERE normalized=?",
                (mention["normalized"],),
            )
        for row in rows:
            pairs.append((entity_type, row["eid"]))
        for row in self.store.all(
            "SELECT mention_id FROM mentions WHERE entity_type=? AND normalized=?"
            " AND mention_id<>? AND status='unconfirmed'",
            (entity_type, mention["normalized"], mention_id),
        ):
            pairs.append(("mention", row["mention_id"]))

        candidate_ids = []
        label = ENTITY_TYPE_LABELS[entity_type]
        for right_type, right_id in pairs:
            cid = self._propose_candidate_tx(
                entity_type,
                ("mention", mention_id),
                (right_type, right_id),
                reason=f"名称「{mention['alias_text']}」相同，疑似同一{label}",
                actor=actor,
                at=at,
            )
            candidate_ids.append(cid)
        return candidate_ids

    @staticmethod
    def _sort_sides(
        left: tuple[str, str], right: tuple[str, str]
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        return (left, right) if left <= right else (right, left)

    def _propose_candidate_tx(
        self,
        entity_type: str,
        left: tuple[str, str],
        right: tuple[str, str],
        reason: str,
        actor: str,
        at: str,
    ) -> str:
        if left == right:
            raise ValidationError("候选关系两侧不能是同一记录")
        (lt, li), (rt, ri) = self._sort_sides(left, right)
        candidate_id = content_id("cand", entity_type, lt, li, rt, ri)
        if self.store.one(
            "SELECT candidate_id FROM candidates WHERE candidate_id=?", (candidate_id,)
        ) is None:
            self.store.run(
                "INSERT INTO candidates(candidate_id, entity_type, left_type, left_id,"
                " right_type, right_id, reason) VALUES(?,?,?,?,?,?,?)",
                (candidate_id, entity_type, lt, li, rt, ri, reason),
            )
            self.store.event(
                actor, "candidate.propose", "candidate", candidate_id,
                {"entity_type": entity_type, "left": [lt, li], "right": [rt, ri]},
                rationale=reason, at=at,
            )
        return candidate_id

    def propose_candidate(
        self,
        entity_type: str,
        left: dict[str, str],
        right: dict[str, str],
        reason: str,
        actor: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """馆员手工提出疑似同一关系。重复提出同一对关系幂等。"""
        if entity_type not in ENTITY_TYPES:
            raise ValidationError(f"候选关系仅支持: {', '.join(ENTITY_TYPES)}")
        at = at or utcnow()
        with self.store.tx():
            for side in (left, right):
                if side["type"] not in ("mention", entity_type):
                    raise ValidationError(
                        f"候选关系一侧只能是 mention 或 {entity_type}: {side['type']}"
                    )
                self._check_side(side["type"], side["id"])
                if side["type"] == "mention":
                    mention = self.store.one(
                        "SELECT entity_type FROM mentions WHERE mention_id=?", (side["id"],)
                    )
                    if mention["entity_type"] != entity_type:
                        raise ValidationError("提及的实体类型与候选关系类型不符")
            candidate_id = self._propose_candidate_tx(
                entity_type,
                (left["type"], left["id"]),
                (right["type"], right["id"]),
                reason,
                actor,
                at,
            )
        return self.get_candidate(candidate_id)

    def _check_side(self, side_type: str, side_id: str) -> None:
        if side_type == "mention":
            self._get("mentions", "mention_id", side_id, "提及")
        elif side_type == "person":
            self._get("persons", "person_id", side_id, "人物")
        elif side_type == "troupe":
            self._get("troupes", "troupe_id", side_id, "团体")
        elif side_type == "place":
            self._get("places", "place_id", side_id, "场所")
        else:
            raise ValidationError(f"未知的候选关系一侧类型: {side_type}")

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        return self._get("candidates", "candidate_id", candidate_id, "候选关系")

    def list_candidates(
        self, status: str | None = "pending", entity_type: str | None = None
    ) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM candidates WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if entity_type:
            sql += " AND entity_type=?"
            params.append(entity_type)
        return self.store.all(sql + " ORDER BY candidate_id", tuple(params))

    def resolve_candidate(
        self,
        candidate_id: str,
        decision: str,
        actor: str,
        rationale: str = "",
        survivor_id: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """人工判断候选关系：confirm 确认同一（链接或合并），reject 否决。

        已判断过的候选关系再次提交返回原结果，不重复执行。
        """
        if decision not in ("confirm", "reject"):
            raise ValidationError("decision 只能是 confirm 或 reject")
        at = at or utcnow()
        with self.store.tx():
            cand = self._get("candidates", "candidate_id", candidate_id, "候选关系")
            if cand["status"] != "pending":
                return cand
            if decision == "reject":
                self.store.run(
                    "UPDATE candidates SET status='rejected', decision_note=? WHERE candidate_id=?",
                    (rationale, candidate_id),
                )
            else:
                self._confirm_candidate_tx(cand, actor, rationale, survivor_id, at)
                self.store.run(
                    "UPDATE candidates SET status='confirmed', decision_note=?"
                    " WHERE candidate_id=?",
                    (rationale, candidate_id),
                )
            self.store.event(
                actor, f"candidate.{decision}", "candidate", candidate_id,
                {"entity_type": cand["entity_type"]}, rationale=rationale, at=at,
            )
        return self.get_candidate(candidate_id)

    def _confirm_candidate_tx(
        self,
        cand: dict[str, Any],
        actor: str,
        rationale: str,
        survivor_id: str | None,
        at: str,
    ) -> None:
        sides = [(cand["left_type"], cand["left_id"]), (cand["right_type"], cand["right_id"])]
        mentions = [s for s in sides if s[0] == "mention"]
        entities = [s for s in sides if s[0] != "mention"]

        if len(entities) == 2:
            winner = survivor_id or entities[0][1]
            loser = entities[0][1] if winner == entities[1][1] else entities[1][1]
            self._merge_entities_tx(cand["entity_type"], winner, loser, actor, rationale, at)
            return

        if len(mentions) == 2:
            entity_id = self._entity_from_mention_tx(mentions[0][1], actor, at)
            self._link_mention_tx(mentions[0][1], entity_id, actor, at)
            self._link_mention_tx(mentions[1][1], entity_id, actor, at)
            return

        mention_id = mentions[0][1]
        entity_id = entities[0][1]
        self._link_mention_tx(mention_id, entity_id, actor, at)

    def _entity_from_mention_tx(self, mention_id: str, actor: str, at: str) -> str:
        mention = self.store.one("SELECT * FROM mentions WHERE mention_id=?", (mention_id,))
        entity_type = mention["entity_type"]
        if entity_type == "person":
            return self._create_person_tx(mention["alias_text"], [], [], actor, at)
        if entity_type == "troupe":
            return self._create_troupe_tx(mention["alias_text"], "troupe", [], [], actor, at)
        return self._create_place_tx(mention["alias_text"], None, None, [], actor, at)

    def _link_mention_tx(self, mention_id: str, entity_id: str, actor: str, at: str) -> None:
        mention = self.store.one("SELECT * FROM mentions WHERE mention_id=?", (mention_id,))
        asset_id = mention["asset_id"]
        entity_type = mention["entity_type"]
        source_id = mention["source_id"]
        if entity_type == "person":
            self.store.run(
                "INSERT OR IGNORE INTO asset_persons(asset_id, person_id, role, source_id)"
                " VALUES(?,?,?,?)",
                (asset_id, entity_id, mention["role"] or "出镜", source_id),
            )
        elif entity_type == "troupe":
            self.store.run(
                "INSERT OR IGNORE INTO asset_troupes(asset_id, troupe_id, source_id)"
                " VALUES(?,?,?)",
                (asset_id, entity_id, source_id),
            )
        else:
            self.store.run(
                "INSERT OR IGNORE INTO asset_places(asset_id, place_id, source_id) VALUES(?,?,?)",
                (asset_id, entity_id, source_id),
            )
        self.store.run(
            "UPDATE mentions SET status='confirmed', resolved_entity=? WHERE mention_id=?",
            (entity_id, mention_id),
        )
        self.store.event(
            actor, "mention.confirm", "mention", mention_id,
            {"entity_id": entity_id, "entity_type": entity_type},
            asset_id=asset_id, at=at,
        )

    def link_mention(
        self, mention_id: str, entity_id: str, actor: str, at: str | None = None
    ) -> dict[str, Any]:
        """馆员直接把一条提及挂到权威实体上（不走候选时）。"""
        at = at or utcnow()
        with self.store.tx():
            mention = self._get("mentions", "mention_id", mention_id, "提及")
            self._check_side(mention["entity_type"], entity_id)
            if mention["status"] != "confirmed":
                self._link_mention_tx(mention_id, entity_id, actor, at)
        return self.store.one("SELECT * FROM mentions WHERE mention_id=?", (mention_id,))

    def _merge_entities_tx(
        self,
        entity_type: str,
        winner: str,
        loser: str,
        actor: str,
        rationale: str,
        at: str,
    ) -> None:
        """合并两条权威实体：loser 的全部别名、链接、许可改指 winner，
        loser 本身保留并标记 merged_into，历史仍可追溯。"""
        if winner == loser:
            raise ValidationError("不能合并到自身")
        table = {"person": "persons", "troupe": "troupes", "place": "places"}[entity_type]
        key = f"{entity_type}_id"
        loser_row = self._get(table, key, loser, "待合并记录")
        self._require_active(loser_row, "待合并记录")
        self._get(table, key, winner, "合并目标")

        def repoint(table_name: str, column: str) -> None:
            self.store.run(
                f"UPDATE OR IGNORE {table_name} SET {column}=? WHERE {column}=?",
                (winner, loser),
            )
            self.store.run(f"DELETE FROM {table_name} WHERE {column}=?", (loser,))

        if entity_type == "person":
            repoint("person_aliases", "person_id")
            repoint("asset_persons", "person_id")
            repoint("person_troupes", "person_id")
            repoint("licenses", "subject_id")
        elif entity_type == "troupe":
            repoint("troupe_aliases", "troupe_id")
            repoint("asset_troupes", "troupe_id")
            repoint("person_troupes", "troupe_id")
            repoint("licenses", "subject_id")
            self.store.run(
                "UPDATE assets SET publisher_id=? WHERE publisher_id=?", (winner, loser)
            )
        else:
            repoint("place_names", "place_id")
            repoint("asset_places", "place_id")
        self.store.run(
            "UPDATE mentions SET resolved_entity=? WHERE resolved_entity=?", (winner, loser)
        )
        self.store.run(
            f"UPDATE {table} SET merged_into=? WHERE {key}=?", (winner, loser)
        )
        self.store.event(
            actor, f"{entity_type}.merge", entity_type, winner,
            {"merged": loser}, rationale=rationale, at=at,
        )
        self.store.event(
            actor, f"{entity_type}.merged_into", entity_type, loser,
            {"survivor": winner}, rationale=rationale, at=at,
        )

    # ------------------------------------------------------------------
    # 权威实体：人物、团体、场所、剧目、剧种
    # ------------------------------------------------------------------

    def _create_person_tx(
        self,
        display_name: str,
        aliases: list[str],
        source_ids: list[str],
        actor: str,
        at: str,
    ) -> str:
        person_id = new_id("person")
        self.store.run(
            "INSERT INTO persons(person_id, display_name) VALUES(?,?)",
            (person_id, display_name),
        )
        self.store.event(
            actor, "person.create", "person", person_id,
            {"display_name": display_name}, source_ids=source_ids, at=at,
        )
        for alias in [display_name, *aliases]:
            self._add_alias_tx("person", person_id, alias, None, None,
                               source_ids[0] if source_ids else None, actor, at)
        return person_id

    def create_person(
        self,
        display_name: str,
        actor: str,
        aliases: list[str] | tuple = (),
        source_ids: list[str] | tuple = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        at = at or utcnow()
        with self.store.tx():
            person_id = self._create_person_tx(
                display_name, list(aliases), list(source_ids), actor, at
            )
        return self.get_person(person_id)

    def get_person(self, person_id: str) -> dict[str, Any]:
        row = self._get("persons", "person_id", person_id, "人物")
        row["aliases"] = self.store.all(
            "SELECT alias, source_id FROM person_aliases WHERE person_id=? ORDER BY alias",
            (person_id,),
        )
        row["troupes"] = self.store.all(
            "SELECT * FROM person_troupes WHERE person_id=?", (person_id,)
        )
        return row

    def _create_troupe_tx(
        self,
        display_name: str,
        kind: str,
        aliases: list[str],
        source_ids: list[str],
        actor: str,
        at: str,
    ) -> str:
        troupe_id = new_id("troupe")
        self.store.run(
            "INSERT INTO troupes(troupe_id, display_name, kind) VALUES(?,?,?)",
            (troupe_id, display_name, kind),
        )
        self.store.event(
            actor, "troupe.create", "troupe", troupe_id,
            {"display_name": display_name, "kind": kind}, source_ids=source_ids, at=at,
        )
        for alias in [display_name, *aliases]:
            self._add_alias_tx("troupe", troupe_id, alias, None, None,
                               source_ids[0] if source_ids else None, actor, at)
        return troupe_id

    def create_troupe(
        self,
        display_name: str,
        actor: str,
        kind: str = "troupe",
        aliases: list[str] | tuple = (),
        source_ids: list[str] | tuple = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        at = at or utcnow()
        with self.store.tx():
            troupe_id = self._create_troupe_tx(
                display_name, kind, list(aliases), list(source_ids), actor, at
            )
        return self.get_troupe(troupe_id)

    def get_troupe(self, troupe_id: str) -> dict[str, Any]:
        row = self._get("troupes", "troupe_id", troupe_id, "团体")
        row["aliases"] = self.store.all(
            "SELECT alias, valid_from, valid_to, source_id FROM troupe_aliases"
            " WHERE troupe_id=? ORDER BY alias",
            (troupe_id,),
        )
        return row

    def rename_troupe(
        self,
        troupe_id: str,
        new_name: str,
        valid_from: str,
        actor: str,
        old_name_valid_to: str | None = None,
        source_id: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """戏班改名：旧名保留为带有效期的别名，新名自 valid_from 起生效。"""
        at = at or utcnow()
        with self.store.tx():
            row = self._get("troupes", "troupe_id", troupe_id, "团体")
            self._require_active(row, "团体")
            if old_name_valid_to is not None:
                self.store.run(
                    "UPDATE troupe_aliases SET valid_to=? WHERE troupe_id=? AND normalized=?",
                    (old_name_valid_to, troupe_id, normalize_name(row["display_name"])),
                )
            self._add_alias_tx("troupe", troupe_id, new_name, valid_from, None,
                               source_id, actor, at)
            self.store.run(
                "UPDATE troupes SET display_name=? WHERE troupe_id=?", (new_name, troupe_id)
            )
            self.store.event(
                actor, "troupe.rename", "troupe", troupe_id,
                {"new_name": new_name, "valid_from": valid_from},
                source_ids=[source_id] if source_id else [], at=at,
            )
        return self.get_troupe(troupe_id)

    def _create_place_tx(
        self,
        name: str,
        valid_from: str | None,
        valid_to: str | None,
        source_ids: list[str],
        actor: str,
        at: str,
    ) -> str:
        place_id = new_id("place")
        self.store.run("INSERT INTO places(place_id) VALUES(?)", (place_id,))
        self.store.event(
            actor, "place.create", "place", place_id, {"name": name},
            source_ids=source_ids, at=at,
        )
        self._add_alias_tx("place", place_id, name, valid_from, valid_to,
                           source_ids[0] if source_ids else None, actor, at)
        return place_id

    def create_place(
        self,
        name: str,
        actor: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        source_ids: list[str] | tuple = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        at = at or utcnow()
        with self.store.tx():
            place_id = self._create_place_tx(
                name, valid_from, valid_to, list(source_ids), actor, at
            )
        return self.get_place(place_id)

    def get_place(self, place_id: str) -> dict[str, Any]:
        row = self._get("places", "place_id", place_id, "场所")
        row["names"] = self.store.all(
            "SELECT name, valid_from, valid_to, source_id FROM place_names"
            " WHERE place_id=? ORDER BY valid_from",
            (place_id,),
        )
        return row

    def add_place_name(
        self,
        place_id: str,
        name: str,
        actor: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        source_id: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """登记地点沿革中的一段名称（如行政区划调整、戏台重修更名）。"""
        at = at or utcnow()
        with self.store.tx():
            row = self._get("places", "place_id", place_id, "场所")
            self._require_active(row, "场所")
            self._add_alias_tx("place", place_id, name, valid_from, valid_to,
                               source_id, actor, at)
        return self.get_place(place_id)

    def place_name_at(self, place_id: str, date: str | None) -> str | None:
        """地点在某一天使用的名称（地点沿革查询）。"""
        names = self.store.all(
            "SELECT * FROM place_names WHERE place_id=? ORDER BY valid_from", (place_id,)
        )
        if not names:
            return None
        if date is None:
            return names[-1]["name"]
        for entry in names:
            after_start = entry["valid_from"] is None or entry["valid_from"] <= date
            before_end = entry["valid_to"] is None or date < entry["valid_to"]
            if after_start and before_end:
                return entry["name"]
        return names[-1]["name"]

    def _add_alias_tx(
        self,
        entity_type: str,
        entity_id: str,
        alias: str,
        valid_from: str | None,
        valid_to: str | None,
        source_id: str | None,
        actor: str,
        at: str,
    ) -> None:
        normalized = normalize_name(alias)
        if entity_type == "person":
            self.store.run(
                "INSERT OR IGNORE INTO person_aliases(person_id, alias, normalized, source_id)"
                " VALUES(?,?,?,?)",
                (entity_id, alias, normalized, source_id),
            )
        elif entity_type == "troupe":
            self.store.run(
                "INSERT OR IGNORE INTO troupe_aliases(troupe_id, alias, normalized,"
                " valid_from, valid_to, source_id) VALUES(?,?,?,?,?,?)",
                (entity_id, alias, normalized, valid_from, valid_to, source_id),
            )
        else:
            self.store.run(
                "INSERT OR IGNORE INTO place_names(place_id, name, normalized,"
                " valid_from, valid_to, source_id) VALUES(?,?,?,?,?,?)",
                (entity_id, alias, normalized, valid_from, valid_to, source_id),
            )
        self.store.event(
            actor, f"{entity_type}.alias", entity_type, entity_id,
            {"alias": alias, "valid_from": valid_from, "valid_to": valid_to},
            source_ids=[source_id] if source_id else [], at=at,
        )
        # 新别名可能解开既有提及的疑似关系——仍只生成候选，留待人工判断
        for row in self.store.all(
            "SELECT mention_id FROM mentions WHERE entity_type=? AND normalized=?"
            " AND status='unconfirmed'",
            (entity_type, normalized),
        ):
            label = ENTITY_TYPE_LABELS[entity_type]
            self._propose_candidate_tx(
                entity_type,
                ("mention", row["mention_id"]),
                (entity_type, entity_id),
                reason=f"名称「{alias}」相同，疑似同一{label}",
                actor=actor,
                at=at,
            )

    def add_alias(
        self,
        entity_type: str,
        entity_id: str,
        alias: str,
        actor: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        source_id: str | None = None,
        at: str | None = None,
    ) -> None:
        if entity_type not in ENTITY_TYPES:
            raise ValidationError(f"别名仅支持: {', '.join(ENTITY_TYPES)}")
        at = at or utcnow()
        with self.store.tx():
            self._check_side(entity_type, entity_id)
            self._add_alias_tx(
                entity_type, entity_id, alias, valid_from, valid_to, source_id, actor, at
            )

    def _upsert_play_tx(
        self, title: str, actor: str, source_ids: list[str], at: str
    ) -> str:
        normalized = normalize_name(title)
        play_id = content_id("play", normalized)
        if self.store.one("SELECT play_id FROM plays WHERE play_id=?", (play_id,)) is None:
            self.store.run(
                "INSERT INTO plays(play_id, title) VALUES(?,?)", (play_id, title)
            )
            self.store.run(
                "INSERT OR IGNORE INTO play_aliases(play_id, alias, normalized) VALUES(?,?,?)",
                (play_id, title, normalized),
            )
            self.store.event(
                actor, "play.create", "play", play_id, {"title": title},
                source_ids=source_ids, at=at,
            )
        return play_id

    def create_genre(
        self,
        name: str,
        actor: str,
        aliases: list[str] | tuple = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        """登记剧种（受控词表）。同一称谓重复登记幂等。"""
        at = at or utcnow()
        normalized = normalize_name(name)
        genre_id = content_id("genre", normalized)
        with self.store.tx():
            if self.store.one("SELECT genre_id FROM genres WHERE genre_id=?", (genre_id,)) is None:
                self.store.run(
                    "INSERT INTO genres(genre_id, name, normalized) VALUES(?,?,?)",
                    (genre_id, name, normalized),
                )
                self.store.event(
                    actor, "genre.create", "genre", genre_id, {"name": name}, at=at
                )
            for alias in aliases:
                self.store.run(
                    "INSERT OR IGNORE INTO genre_aliases(genre_id, alias, normalized)"
                    " VALUES(?,?,?)",
                    (genre_id, alias, normalize_name(alias)),
                )
        return self.store.one("SELECT * FROM genres WHERE genre_id=?", (genre_id,))

    def _find_genre(self, label: str) -> dict[str, Any] | None:
        normalized = normalize_name(label)
        row = self.store.one(
            "SELECT * FROM genres WHERE normalized=? AND merged_into IS NULL", (normalized,)
        )
        if row is None:
            row = self.store.one(
                "SELECT g.* FROM genre_aliases a JOIN genres g ON g.genre_id=a.genre_id"
                " WHERE a.normalized=? AND g.merged_into IS NULL",
                (normalized,),
            )
        return row

    def create_scheme(
        self,
        scheme_id: str,
        title: str,
        actor: str,
        issued_at: str = "",
        entries: list[dict[str, Any]] | tuple = (),
        note: str = "",
        at: str | None = None,
    ) -> dict[str, Any]:
        """登记一版剧种分类方案（如某年调查分类、非遗名录）。"""
        at = at or utcnow()
        with self.store.tx():
            if self.store.one(
                "SELECT scheme_id FROM genre_schemes WHERE scheme_id=?", (scheme_id,)
            ) is None:
                self.store.run(
                    "INSERT INTO genre_schemes(scheme_id, title, issued_at, note)"
                    " VALUES(?,?,?,?)",
                    (scheme_id, title, issued_at, note),
                )
                self.store.event(
                    actor, "scheme.create", "genre_scheme", scheme_id,
                    {"title": title, "issued_at": issued_at}, at=at,
                )
            for entry in entries:
                self.store.run(
                    "INSERT OR IGNORE INTO genre_scheme_entries(scheme_id, genre_id,"
                    " parent_genre_id, note) VALUES(?,?,?,?)",
                    (scheme_id, entry["genre_id"], entry.get("parent_genre_id"),
                     entry.get("note", "")),
                )
        return self.store.one("SELECT * FROM genre_schemes WHERE scheme_id=?", (scheme_id,))

    def _assign_genre_tx(
        self,
        asset_id: str,
        genre_id: str,
        scheme_id: str,
        actor: str,
        source_ids: list[str],
        at: str,
        supersede: bool = False,
    ) -> None:
        if supersede:
            self.store.run(
                "UPDATE asset_genres SET status='superseded' WHERE asset_id=? AND scheme_id=?"
                " AND status='active' AND genre_id<>?",
                (asset_id, scheme_id, genre_id),
            )
        self.store.run(
            "INSERT OR IGNORE INTO asset_genres(asset_id, genre_id, scheme_id, source_id)"
            " VALUES(?,?,?,?)",
            (asset_id, genre_id, scheme_id, source_ids[0] if source_ids else None),
        )
        self.store.event(
            actor, "asset.genre_assign", "asset", asset_id,
            {"genre_id": genre_id, "scheme_id": scheme_id},
            source_ids=source_ids, asset_id=asset_id, at=at,
        )

    def assign_genre(
        self,
        asset_id: str,
        genre_id: str,
        scheme_id: str,
        actor: str,
        source_id: str | None = None,
        supersede: bool = False,
        at: str | None = None,
    ) -> None:
        """在某版分类方案下给影像标注剧种；supersede=True 表示订正旧标注。"""
        at = at or utcnow()
        with self.store.tx():
            self._get("assets", "asset_id", asset_id, "影像记录")
            self._get("genres", "genre_id", genre_id, "剧种")
            self._get("genre_schemes", "scheme_id", scheme_id, "分类方案")
            self._assign_genre_tx(
                asset_id, genre_id, scheme_id, actor,
                [source_id] if source_id else [], at, supersede=supersede,
            )

    def set_publisher(
        self,
        asset_id: str,
        troupe_id: str,
        actor: str,
        source_id: str | None = None,
        at: str | None = None,
    ) -> None:
        """登记影像的出版方（影响许可核对）。"""
        at = at or utcnow()
        with self.store.tx():
            self._get("assets", "asset_id", asset_id, "影像记录")
            self._get("troupes", "troupe_id", troupe_id, "出版方")
            self.store.run(
                "UPDATE assets SET publisher_id=? WHERE asset_id=?", (troupe_id, asset_id)
            )
            self.store.event(
                actor, "asset.set_publisher", "asset", asset_id,
                {"publisher_id": troupe_id},
                source_ids=[source_id] if source_id else [], asset_id=asset_id, at=at,
            )

    # ------------------------------------------------------------------
    # 许可与使用留痕
    # ------------------------------------------------------------------

    def grant_license(
        self,
        subject_type: str,
        subject_id: str,
        purpose: str,
        actor: str,
        asset_id: str | None = None,
        granted_at: str | None = None,
        note: str = "",
        at: str | None = None,
    ) -> dict[str, Any]:
        """授予许可：按主体（作者/演员/院团/出版方）× 用途核对。

        asset_id 为空表示该主体名下全部影像。同一有效许可重复授予幂等。
        """
        if subject_type not in SUBJECT_TYPES:
            raise ValidationError(f"许可主体仅支持: {', '.join(SUBJECT_TYPES)}")
        if purpose not in PURPOSES:
            raise ValidationError(f"用途仅支持: {', '.join(PURPOSES)}")
        granted_at = granted_at or at or utcnow()
        with self.store.tx():
            if subject_type in ("author", "performer"):
                self._get("persons", "person_id", subject_id, "许可主体")
            else:
                self._get("troupes", "troupe_id", subject_id, "许可主体")
            if asset_id is not None:
                self._get("assets", "asset_id", asset_id, "影像记录")
            existing = self.store.one(
                "SELECT * FROM licenses WHERE subject_type=? AND subject_id=? AND purpose=?"
                " AND IFNULL(asset_id,'')=IFNULL(?,'') AND withdrawn_at IS NULL",
                (subject_type, subject_id, purpose, asset_id),
            )
            if existing is not None:
                return existing
            license_id = new_id("lic")
            self.store.run(
                "INSERT INTO licenses(license_id, subject_type, subject_id, purpose,"
                " asset_id, granted_at, note) VALUES(?,?,?,?,?,?,?)",
                (license_id, subject_type, subject_id, purpose, asset_id, granted_at, note),
            )
            self.store.event(
                actor, "license.grant", "license", license_id,
                {"subject_type": subject_type, "subject_id": subject_id, "purpose": purpose,
                 "asset_id": asset_id},
                rationale=note, asset_id=asset_id, at=at,
            )
        return self.store.one("SELECT * FROM licenses WHERE license_id=?", (license_id,))

    def withdraw_license(
        self,
        license_id: str,
        actor: str,
        at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """撤回许可：只写入撤回时间，限制未来使用；
        已登记的出版与展出记录不受影响，继续留痕。"""
        at = at or utcnow()
        with self.store.tx():
            row = self._get("licenses", "license_id", license_id, "许可")
            if row["withdrawn_at"] is not None:
                return row
            self.store.run(
                "UPDATE licenses SET withdrawn_at=? WHERE license_id=?", (at, license_id)
            )
            self.store.event(
                actor, "license.withdraw", "license", license_id,
                {"withdrawn_at": at}, rationale=note, asset_id=row["asset_id"], at=at,
            )
        return self.store.one("SELECT * FROM licenses WHERE license_id=?", (license_id,))

    def _required_subjects(self, asset_id: str) -> list[dict[str, Any]]:
        """一条影像在授权前必须覆盖的主体清单。"""
        subjects: list[dict[str, Any]] = []
        authors = self.store.all(
            "SELECT person_id FROM asset_persons WHERE asset_id=? AND role IN (%s)"
            % ",".join("?" * len(AUTHOR_ROLES)),
            (asset_id, *AUTHOR_ROLES),
        )
        if authors:
            subjects += [{"subject_type": "author", "subject_id": r["person_id"]} for r in authors]
        else:
            subjects.append({"subject_type": "author", "subject_id": None})
        performers = self.store.all(
            "SELECT person_id FROM asset_persons WHERE asset_id=? AND role IN (%s)"
            % ",".join("?" * len(PERFORMER_ROLES)),
            (asset_id, *PERFORMER_ROLES),
        )
        subjects += [
            {"subject_type": "performer", "subject_id": r["person_id"]} for r in performers
        ]
        troupes = self.store.all(
            "SELECT troupe_id FROM asset_troupes WHERE asset_id=?", (asset_id,)
        )
        subjects += [
            {"subject_type": "troupe", "subject_id": r["troupe_id"]} for r in troupes
        ]
        asset = self.store.one("SELECT publisher_id FROM assets WHERE asset_id=?", (asset_id,))
        if asset and asset["publisher_id"]:
            subjects.append({"subject_type": "publisher", "subject_id": asset["publisher_id"]})
        return subjects

    def authorize(
        self, asset_id: str, purpose: str, at: str | None = None
    ) -> dict[str, Any]:
        """核对某用途在某时刻是否被许可覆盖。"""
        if purpose not in PURPOSES:
            raise ValidationError(f"用途仅支持: {', '.join(PURPOSES)}")
        self._get("assets", "asset_id", asset_id, "影像记录")
        at = at or utcnow()
        missing: list[dict[str, Any]] = []
        used: list[str] = []
        for subject in self._required_subjects(asset_id):
            if subject["subject_id"] is None:
                missing.append({"subject_type": subject["subject_type"], "unidentified": True})
                continue
            row = self.store.one(
                "SELECT license_id FROM licenses WHERE subject_type=? AND subject_id=?"
                " AND purpose=? AND (asset_id IS NULL OR asset_id=?)"
                " AND granted_at<=? AND (withdrawn_at IS NULL OR withdrawn_at>?)"
                " ORDER BY granted_at LIMIT 1",
                (subject["subject_type"], subject["subject_id"], purpose, asset_id, at, at),
            )
            if row is None:
                missing.append(subject)
            else:
                used.append(row["license_id"])
        return {"ok": not missing, "missing": missing, "licenses": sorted(set(used))}

    def record_usage(
        self,
        asset_id: str,
        purpose: str,
        venue: str,
        actor: str,
        at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """登记一次使用（出版、展出、网络发布……）。

        登记时核对许可并快照所依据的许可编号；许可日后被撤回，
        这条使用记录仍然保留。同一使用重复登记幂等。
        """
        at = at or utcnow()
        usage_id = content_id("use", asset_id, purpose, venue, at)
        with self.store.tx():
            existing = self.store.one("SELECT * FROM usages WHERE usage_id=?", (usage_id,))
            if existing is not None:
                existing["license_ids"] = json.loads(existing["license_ids"])
                existing["replayed"] = True
                return existing
            auth = self.authorize(asset_id, purpose, at)
            if not auth["ok"]:
                raise NotAuthorizedError(auth["missing"])
            self.store.run(
                "INSERT INTO usages(usage_id, asset_id, purpose, venue, used_at, actor,"
                " license_ids, note) VALUES(?,?,?,?,?,?,?,?)",
                (usage_id, asset_id, purpose, venue, at, actor,
                 json.dumps(auth["licenses"]), note),
            )
            self.store.event(
                actor, "usage.record", "usage", usage_id,
                {"purpose": purpose, "venue": venue, "licenses": auth["licenses"]},
                asset_id=asset_id, at=at,
            )
        row = self.store.one("SELECT * FROM usages WHERE usage_id=?", (usage_id,))
        row["license_ids"] = json.loads(row["license_ids"])
        row["replayed"] = False
        return row

    def list_usages(self, asset_id: str) -> list[dict[str, Any]]:
        self._get("assets", "asset_id", asset_id, "影像记录")
        rows = self.store.all(
            "SELECT * FROM usages WHERE asset_id=? ORDER BY used_at", (asset_id,)
        )
        for row in rows:
            row["license_ids"] = json.loads(row["license_ids"])
        return rows

    # ------------------------------------------------------------------
    # 检索与可见性
    # ------------------------------------------------------------------

    def _identity_unconfirmed(self, asset_id: str) -> bool:
        mention = self.store.one(
            "SELECT mention_id FROM mentions WHERE asset_id=? AND status='unconfirmed'"
            " LIMIT 1",
            (asset_id,),
        )
        if mention is not None:
            return True
        pending = self.store.one(
            "SELECT c.candidate_id FROM candidates c"
            " JOIN mentions m ON (c.left_type='mention' AND c.left_id=m.mention_id)"
            "    OR (c.right_type='mention' AND c.right_id=m.mention_id)"
            " WHERE m.asset_id=? AND c.status='pending' LIMIT 1",
            (asset_id,),
        )
        return pending is not None

    def _asset_public_view(self, asset_id: str, include_content: bool) -> dict[str, Any]:
        asset = self.store.one("SELECT * FROM assets WHERE asset_id=?", (asset_id,))
        batch = self.store.one(
            "SELECT title, photographer FROM batches WHERE batch_id=?", (asset["batch_id"],)
        ) or {}
        view: dict[str, Any] = {
            "asset_id": asset_id,
            "captured_at": asset["captured_at"],
            "batch_title": batch.get("title", ""),
        }
        if not include_content:
            return view
        view["fingerprint"] = asset["fingerprint"]
        view["media_kind"] = asset["media_kind"]
        latest = self._latest_caption(asset_id)
        view["caption"] = self._caption_detail(latest) if latest else None
        view["persons"] = self.store.all(
            "SELECT p.person_id, p.display_name, ap.role FROM asset_persons ap"
            " JOIN persons p ON p.person_id=ap.person_id WHERE ap.asset_id=?",
            (asset_id,),
        )
        view["troupes"] = self.store.all(
            "SELECT t.troupe_id, t.display_name FROM asset_troupes at2"
            " JOIN troupes t ON t.troupe_id=at2.troupe_id WHERE at2.asset_id=?",
            (asset_id,),
        )
        view["places"] = [
            {"place_id": r["place_id"], "name": self.place_name_at(r["place_id"], asset["captured_at"])}
            for r in self.store.all("SELECT place_id FROM asset_places WHERE asset_id=?", (asset_id,))
        ]
        view["plays"] = self.store.all(
            "SELECT p.play_id, p.title FROM asset_plays ap JOIN plays p ON p.play_id=ap.play_id"
            " WHERE ap.asset_id=?",
            (asset_id,),
        )
        view["mentions"] = self.store.all(
            "SELECT mention_id, entity_type, alias_text, role, status FROM mentions"
            " WHERE asset_id=?",
            (asset_id,),
        )
        return view

    def search_by_genre(
        self,
        genre_label: str,
        purpose: str = "research",
        viewer: str = "researcher",
        scheme_id: str | None = None,
        at: str | None = None,
    ) -> list[dict[str, Any]]:
        """按剧种检索。

        研究者只能看到获准内容：未获许可的影像只返回可识别的占位信息
        （编号、年代、批次、受限原因），不返回说明文本与文件指纹；
        未确认身份以 identity_unconfirmed 标记呈现。馆员视图不受限。
        """
        if viewer not in VIEWERS:
            raise ValidationError(f"viewer 仅支持: {', '.join(VIEWERS)}")
        genre = self._find_genre(genre_label)
        if genre is None:
            raise NotFoundError(f"剧种不存在: {genre_label}")
        if scheme_id is None:
            scheme = self.store.one(
                "SELECT scheme_id FROM genre_schemes ORDER BY issued_at DESC, scheme_id DESC"
                " LIMIT 1"
            )
            scheme_id = scheme["scheme_id"] if scheme else DEFAULT_SCHEME_ID
        rows = self.store.all(
            "SELECT asset_id FROM asset_genres WHERE genre_id=? AND scheme_id=?"
            " AND status='active' ORDER BY asset_id",
            (genre["genre_id"], scheme_id),
        )
        at = at or utcnow()
        results = []
        for row in rows:
            asset_id = row["asset_id"]
            auth = self.authorize(asset_id, purpose, at)
            restricted = not auth["ok"]
            if viewer == "archivist":
                item = self._asset_public_view(asset_id, include_content=True)
                item["genre_assignments"] = self.store.all(
                    "SELECT * FROM asset_genres WHERE asset_id=?", (asset_id,)
                )
                item["pending_candidates"] = self.store.all(
                    "SELECT c.candidate_id, c.entity_type, c.reason FROM candidates c"
                    " JOIN mentions m ON (c.left_type='mention' AND c.left_id=m.mention_id)"
                    "    OR (c.right_type='mention' AND c.right_id=m.mention_id)"
                    " WHERE m.asset_id=? AND c.status='pending'",
                    (asset_id,),
                )
            else:
                item = self._asset_public_view(asset_id, include_content=not restricted)
            item["restricted"] = restricted
            item["identity_unconfirmed"] = self._identity_unconfirmed(asset_id)
            if restricted:
                item["missing"] = auth["missing"]
            results.append(item)
        return results

    # ------------------------------------------------------------------
    # 溯源
    # ------------------------------------------------------------------

    def asset_history(self, asset_id: str) -> dict[str, Any]:
        """一条影像的完整沿革：事件流 + 说明全部版本（含每次勘误依据）。"""
        asset = self._get("assets", "asset_id", asset_id, "影像记录")
        events = self.store.all(
            "SELECT * FROM events WHERE asset_id=? ORDER BY ts, event_id", (asset_id,)
        )
        for event in events:
            event["payload"] = json.loads(event["payload"])
            event["source_ids"] = json.loads(event["source_ids"])
        return {
            "asset": asset,
            "captions": self.caption_history(asset_id),
            "events": events,
            "usages": self.list_usages(asset_id),
        }

    def lineage(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        """任意记录的事件链：从建档到当前，每次变更的操作者与依据。"""
        rows = self.store.all(
            "SELECT * FROM events WHERE entity_type=? AND entity_id=? ORDER BY ts, event_id",
            (entity_type, entity_id),
        )
        if not rows:
            raise NotFoundError(f"无此记录的事件: {entity_type}/{entity_id}")
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            row["source_ids"] = json.loads(row["source_ids"])
        return rows

    def place_history(self, place_id: str) -> dict[str, Any]:
        """地点沿革：全部名称时段 + 事件链。"""
        place = self.get_place(place_id)
        return {**place, "events": self.lineage("place", place_id)}
