"""档案业务服务：领域规则全部集中在这里。

设计原则
--------
* 说明文字、剧种归类只追加版本，从不就地覆盖；每条版本必须附资料出处。
* 名称只作解析线索：重名/疑似同一人不自动合并，只生成待人工判断的候选关系。
* 田野回传按「拍摄批次 + 客户端记录键」幂等，文件指纹跨批次查重。
* 授权撤回只终止未来使用；已登记的出版/展出留痕不可变，并快照当时授权结论。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .models import (
    CANDIDATE_CONFIRMED,
    CANDIDATE_PENDING,
    CANDIDATE_REJECTED,
    CERTAINTY,
    IDENTITY_CONFIRMED,
    IDENTITY_MERGED,
    IDENTITY_PROVISIONAL,
    LICENSE_ACTIVE,
    LICENSE_WITHDRAWN,
    ORGANIZATION,
    PERSON,
    PUBLISHER,
    PURPOSES,
    TROUPE,
    normalize_name,
)
from .store import Conflict, FINGERPRINT_RE, NotFound, Store, ValidationError

COLL_BY_KIND = {
    "person": "persons",
    "organization": "organizations",
    "place": "places",
    "genre": "genres",
    "play": "plays",
}


def today() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class ArchiveService:
    def __init__(self, store: Store):
        self.store = store

    # ============================================================== 基础实体

    def register_person(
        self,
        name: str,
        *,
        aliases: list[str] | None = None,
        source: str,
        note: str = "",
        person_id: str | None = None,
    ) -> str:
        """登记人物。已存在的同名人物不自动合并（改名/同名常见）。"""
        return self._register_entity(
            "persons",
            PERSON,
            name,
            aliases or [],
            source,
            note,
            person_id,
        )

    def register_organization(
        self,
        name: str,
        *,
        org_type: str = TROUPE,
        aliases: list[str] | None = None,
        source: str,
        note: str = "",
        org_id: str | None = None,
    ) -> str:
        if org_type not in (TROUPE, PUBLISHER, "other"):
            raise ValidationError(f"未知院团类型: {org_type}")
        return self._register_entity(
            "organizations",
            ORGANIZATION,
            name,
            aliases or [],
            source,
            note,
            org_id,
            extra={"org_type": org_type},
        )

    def register_place(
        self,
        name: str,
        *,
        aliases: list[str] | None = None,
        source: str,
        place_id: str | None = None,
    ) -> str:
        return self._register_entity(
            "places", "place", name, aliases or [], source, "", place_id
        )

    def register_genre(
        self,
        name: str,
        *,
        aliases: list[str] | None = None,
        source: str,
        genre_id: str | None = None,
    ) -> str:
        """登记剧种。山西 38 个剧种的分类本身有版本，见 classify_genre。"""
        return self._register_entity(
            "genres", "genre", name, aliases or [], source, "", genre_id
        )

    def register_play(
        self,
        title: str,
        *,
        aliases: list[str] | None = None,
        source: str,
        play_id: str | None = None,
    ) -> str:
        return self._register_entity(
            "plays", "play", title, aliases or [], source, "", play_id
        )

    def _register_entity(
        self,
        coll: str,
        kind: str,
        name: str,
        aliases: list[str],
        source: str,
        note: str,
        fixed_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        name = (name or "").strip()
        if not name:
            raise ValidationError("名称不能为空")
        if not source:
            raise ValidationError("登记实体必须注明资料出处")
        with self.store.lock:
            record = {
                "id": fixed_id or self.store.next_id(kind),
                "name": name,
                "aliases": list(dict.fromkeys(aliases)),
                "status": IDENTITY_PROVISIONAL,
                "source": source,
                "note": note,
                "registered_at": now_iso(),
            }
            if extra:
                record.update(extra)
            self.store.insert(coll, record)
            self.store.index_name(kind, record)
            self.store.persist()
            return record["id"]

    def add_alias(self, kind: str, record_id: str, alias: str, *, source: str) -> None:
        """补录别名/曾用名（戏班改名、演员跨剧种流动的旧名）。"""
        alias = (alias or "").strip()
        if not alias or not source:
            raise ValidationError("别名与出处均不能为空")
        coll = COLL_BY_KIND[kind]
        with self.store.lock:
            record = self.store.require(coll, record_id)
            if normalize_name(alias) not in [
                normalize_name(a) for a in record["aliases"]
            ] and normalize_name(alias) != normalize_name(record["name"]):
                record["aliases"].append(alias)
                record.setdefault("alias_sources", {})[alias] = source
                self.store.index_name(kind, record)
                self.store.persist()

    def rename_place(self, place_id: str, new_name: str, *, since: str, source: str) -> None:
        """场所沿革：旧名进入曾用名，并在 place_history 留下一条带日期的沿革。"""
        with self.store.lock:
            place = self.store.require("places", place_id)
            old_name = place["name"]
            place["name"] = new_name.strip()
            self.store.index_name("place", place)
            # 先换名再把旧名收为曾用名，否则旧名与现名相同会被别名去重跳过
            self.add_alias("place", place_id, old_name, source=source)
            self.store.insert(
                "place_history",
                {
                    "id": self.store.next_id("ph"),
                    "place_id": place_id,
                    "from_name": old_name,
                    "to_name": new_name.strip(),
                    "since": since,
                    "source": source,
                    "recorded_at": now_iso(),
                },
            )
            self.store.persist()

    def rename_organization(
        self, org_id: str, new_name: str, *, since: str, source: str
    ) -> None:
        """戏班改名：保留曾用名，沿革挂在该团体上。"""
        with self.store.lock:
            org = self.store.require("organizations", org_id)
            old_name = org["name"]
            org["name"] = new_name.strip()
            self.store.index_name("organization", org)
            self.add_alias("organization", org_id, old_name, source=source)
            org.setdefault("name_history", []).append(
                {"from_name": old_name, "to_name": new_name.strip(), "since": since, "source": source}
            )
            self.store.persist()

    # ============================================================== 拍摄批次

    def register_batch(
        self,
        code: str,
        *,
        photographer_id: str | None = None,
        received_at: str | None = None,
        note: str = "",
        source: str,
    ) -> str:
        code = (code or "").strip()
        if not code:
            raise ValidationError("批次编号不能为空")
        with self.store.lock:
            for batch in self.store.all("batches"):
                if batch["code"] == code:
                    # 离线回传重复提交同一批次：幂等返回既有批次
                    return batch["id"]
            record = {
                "id": self.store.next_id("batch"),
                "code": code,
                "photographer_id": photographer_id,
                "received_at": received_at or today(),
                "note": note,
                "source": source,
            }
            self.store.insert("batches", record)
            self.store.persist()
            return record["id"]

    def _require_batch_code(self, batch_code: str) -> str:
        for batch in self.store.all("batches"):
            if batch["code"] == batch_code:
                return batch["id"]
        raise ValidationError(f"未登记的拍摄批次: {batch_code}")

    # ============================================================== 影像幂等导入

    def import_asset(self, payload: dict[str, Any]) -> dict[str, Any]:
        """导入一条田野影像。重复提交返回既有记录，不产生第二条。

        必填：batch_code, client_ref（离线端本地唯一键）, file_fingerprint,
              captured_at, source
        名称字段可给 id 或 name；name 重名无法判定时，影像先入库，
        身份挂起并生成候选关系，等待馆员判断。
        """
        required = ("batch_code", "client_ref", "file_fingerprint", "captured_at", "source")
        for key in required:
            if not payload.get(key):
                raise ValidationError(f"导入缺少必填字段: {key}")
        fingerprint = payload["file_fingerprint"].strip()
        if not FINGERPRINT_RE.match(fingerprint):
            raise ValidationError("文件指纹格式应为 算法:值，例如 sha256:…")

        with self.store.lock:
            batch_id = self._require_batch_code(payload["batch_code"])

            # 1) 批次+客户端键幂等
            existing = self.store.ledger_lookup(payload["batch_code"], payload["client_ref"])
            if existing:
                return {"asset_id": existing, "dedup": "client_ref", "created": False}

            # 2) 文件指纹跨批次查重（同一数字文件/底片重复导入）
            dup = self.store.fingerprint_lookup(fingerprint)
            if dup:
                return {"asset_id": dup, "dedup": "fingerprint", "created": False}

            place_id, place_candidates = self._resolve_ref(
                "place", payload.get("place_id"), payload.get("place_name")
            )
            genre_id, genre_candidates = self._resolve_ref(
                "genre", payload.get("genre_id"), payload.get("genre_name")
            )
            play_id, play_candidates = self._resolve_ref(
                "play", payload.get("play_id"), payload.get("play_title")
            )
            troupe_id, troupe_candidates = self._resolve_ref(
                "organization", payload.get("troupe_id"), payload.get("troupe_name")
            )
            photographer_id, photographer_candidates = self._resolve_ref(
                "person",
                payload.get("photographer_id"),
                payload.get("photographer_name"),
            )

            asset_id = self.store.next_id("asset")
            asset = {
                "id": asset_id,
                "batch_id": batch_id,
                "batch_code": payload["batch_code"],
                "client_ref": payload["client_ref"],
                "file_fingerprint": fingerprint,
                "captured_at": payload["captured_at"],
                "imported_at": now_iso(),
                "source": payload["source"],
                "place_id": place_id,
                "genre_id": genre_id,
                "play_id": play_id,
                "troupe_id": troupe_id,
                "photographer_id": photographer_id,
                "caption_current_version": 0,
                "genre_current_version": 0,
            }
            self.store.insert("assets", asset)
            self.store.ledger_record(
                payload["batch_code"], payload["client_ref"], asset_id, fingerprint
            )

            # 初始说明即第 1 版（胶片卡片/口述原话保留，不做"修正"）
            if payload.get("caption"):
                self._append_caption(
                    asset_id,
                    payload["caption"],
                    source=payload["source"],
                    basis=payload.get("caption_basis", "初次著录"),
                )

            # 初始剧种归类即第 1 版（若给的是明确 id/name）
            if genre_id:
                self._append_genre_version(
                    asset_id, genre_id, source=payload["source"],
                    basis=payload.get("genre_basis", "导入时著录"),
                )

            # 演员：跨剧种流动者按人登记；无法确认者生成候选
            for performer in payload.get("performers", []):
                self._attach_performer(asset_id, performer, payload["source"])

            # 名称重名产生的候选
            for kind, name, ids in place_candidates:
                self._raise_candidate(
                    "place", ids, asset_id=asset_id, field="place", name=name
                )
            for kind, name, ids in genre_candidates:
                self._raise_candidate(
                    "genre", ids, asset_id=asset_id, field="genre", name=name
                )
            for kind, name, ids in play_candidates:
                self._raise_candidate(
                    "play", ids, asset_id=asset_id, field="play", name=name
                )
            for kind, name, ids in troupe_candidates:
                self._raise_candidate(
                    "organization", ids, asset_id=asset_id, field="troupe", name=name
                )
            for kind, name, ids in photographer_candidates:
                self._raise_candidate(
                    "person", ids, asset_id=asset_id, field="photographer", name=name
                )

            self.store.persist()
            return {"asset_id": asset_id, "dedup": None, "created": True}

    # ---- 名称解析：给 id 直接用；给 name 唯一命中才挂；重名挂起并出候选 ----
    def _resolve_ref(
        self, kind: str, fixed_id: str | None, name: str | None
    ) -> tuple[str | None, list[tuple[str, str, list[str]]]]:
        if fixed_id:
            record_id = self.store.canonical(kind, fixed_id)
            if self.store.get(COLL_BY_KIND[kind], record_id) is None:
                raise ValidationError(f"{kind} 编号不存在: {fixed_id}")
            return record_id, []
        if name:
            result = self.store.resolve_name(kind, name)
            if result["state"] == "resolved":
                return result["id"], []
            if result["state"] == "ambiguous":
                return None, [(kind, name, result["ids"])]
            # 未知名：直接按名字建新的临时实体（provisional），不做静默猜测
            new_id = self.store.next_id(kind)
            record = {
                "id": new_id,
                "name": name.strip(),
                "aliases": [],
                "status": IDENTITY_PROVISIONAL,
                "source": "导入时自动占位",
                "note": "",
                "registered_at": now_iso(),
            }
            if kind == "organization":
                record["org_type"] = TROUPE
            self.store.insert(COLL_BY_KIND[kind], record)
            self.store.index_name(kind, record)
            return new_id, []
        return None, []

    def _attach_performer(self, asset_id: str, performer: dict[str, Any], source: str) -> None:
        person_id, candidates = self._resolve_ref(
            "person", performer.get("person_id"), performer.get("name")
        )
        role = (performer.get("role") or "").strip()
        certainty = performer.get("certainty", IDENTITY_PROVISIONAL)
        if certainty not in CERTAINTY:
            raise ValidationError(f"身份确定程度非法: {certainty}")
        participation_id = self.store.next_id("part")
        self.store.insert(
            "participations",
            {
                "id": participation_id,
                "asset_id": asset_id,
                "person_id": person_id,
                "role": role,
                "certainty": certainty,
                "source": source,
            },
        )
        for kind, name, ids in candidates:
            self._raise_candidate(
                "person",
                ids,
                asset_id=asset_id,
                field="performer",
                name=name,
                participation_id=participation_id,
            )

    # ============================================================== 说明与剧种勘误

    def revise_caption(
        self, asset_id: str, text: str, *, source: str, basis: str
    ) -> int:
        """勘误说明：追加新版本，旧版（含早年胶片卡片原话）全部保留。"""
        if not text or not source or not basis:
            raise ValidationError("说明文字、资料出处、勘误依据均不能为空")
        with self.store.lock:
            self.store.require("assets", asset_id)
            version = self._append_caption(asset_id, text, source=source, basis=basis)
            self.store.persist()
            return version

    def _append_caption(self, asset_id: str, text: str, *, source: str, basis: str) -> int:
        asset = self.store.require("assets", asset_id)
        version = asset["caption_current_version"] + 1
        self.store.insert(
            "caption_versions",
            {
                "id": f"{asset_id}#cap-v{version}",
                "asset_id": asset_id,
                "version": version,
                "text": text,
                "source": source,
                "basis": basis,
                "recorded_at": now_iso(),
            },
        )
        asset["caption_current_version"] = version
        return version

    def classify_genre(
        self, asset_id: str, genre_id: str, *, source: str, basis: str
    ) -> int:
        """剧种归类勘误：38 个剧种的分类口径有时代版本，逐版留痕。"""
        if not source or not basis:
            raise ValidationError("剧种勘误必须给出资料出处与依据")
        with self.store.lock:
            self.store.require("assets", asset_id)
            genre_id = self.store.canonical("genre", genre_id)
            self.store.require("genres", genre_id)
            version = self._append_genre_version(
                asset_id, genre_id, source=source, basis=basis
            )
            self.store.persist()
            return version

    def _append_genre_version(
        self, asset_id: str, genre_id: str, *, source: str, basis: str
    ) -> int:
        asset = self.store.require("assets", asset_id)
        version = asset["genre_current_version"] + 1
        self.store.insert(
            "genre_versions",
            {
                "id": f"{asset_id}#genre-v{version}",
                "asset_id": asset_id,
                "version": version,
                "genre_id": genre_id,
                "source": source,
                "basis": basis,
                "recorded_at": now_iso(),
            },
        )
        asset["genre_current_version"] = version
        asset["genre_id"] = genre_id
        return version

    # ============================================================== 候选关系

    def _raise_candidate(
        self,
        kind: str,
        candidate_ids: list[str],
        *,
        asset_id: str,
        field: str,
        name: str,
        participation_id: str | None = None,
    ) -> str:
        # 同一影像同一字段同一组候选只保留一条 pending
        for cand in self.store.all("candidates"):
            if (
                cand["status"] == CANDIDATE_PENDING
                and cand["asset_id"] == asset_id
                and cand["field"] == field
                and sorted(cand["candidate_ids"]) == sorted(candidate_ids)
            ):
                return cand["id"]
        record = {
            "id": self.store.next_id("cand"),
            "kind": kind,
            "candidate_ids": candidate_ids,
            "asset_id": asset_id,
            "field": field,
            "name": name,
            "participation_id": participation_id,
            "status": CANDIDATE_PENDING,
            "raised_at": now_iso(),
            "resolution": None,
        }
        self.store.insert("candidates", record)
        return record["id"]

    def suggest_identity(self, kind: str, id_a: str, id_b: str, *, reason: str) -> str:
        """馆员/比对流程提交「疑似同一人/戏班/场所」，只生成待判候选。"""
        coll = COLL_BY_KIND[kind]
        with self.store.lock:
            self.store.require(coll, id_a)
            self.store.require(coll, id_b)
            if id_a == id_b:
                raise ValidationError("不能对同一记录建立候选关系")
            cid = self._raise_candidate(
                kind, sorted([id_a, id_b]), asset_id="", field="identity", name=""
            )
            cand = self.store.require("candidates", cid)
            cand["reason"] = reason
            self.store.persist()
            return cid

    def list_candidates(self, *, status: str = CANDIDATE_PENDING) -> list[dict[str, Any]]:
        return [c for c in self.store.all("candidates") if c["status"] == status]

    def resolve_candidate(
        self, candidate_id: str, *, decision: str, keep_id: str | None = None, reviewer: str
    ) -> dict[str, Any]:
        """人工判定候选。

        decision=confirmed：两个实体确为同一对象，合并到 keep_id（旧记录标 merged
        并保留别名），相关影像/参演/授权全部改挂；
        decision=rejected：确认为不同对象，候选关闭，不再重复提示。
        """
        if decision not in (CANDIDATE_CONFIRMED, CANDIDATE_REJECTED):
            raise ValidationError("decision 只能是 confirmed / rejected")
        with self.store.lock:
            cand = self.store.require("candidates", candidate_id)
            if cand["status"] != CANDIDATE_PENDING:
                raise Conflict(f"候选已处理: {candidate_id}")
            ids = list(cand["candidate_ids"])
            if decision == CANDIDATE_CONFIRMED:
                target = keep_id or cand.get("winner_id")
                if not target:
                    raise ValidationError("确认合并必须指定保留记录 keep_id")
                if target not in ids:
                    raise ValidationError("keep_id 必须是候选之一")
                for old_id in ids:
                    if old_id == target:
                        continue
                    self._merge_entities(cand["kind"], old_id, target, candidate_id)
                # 若候选由某条影像的字段触发，直接把字段改挂到保留记录
                if cand.get("asset_id"):
                    self._reattach_asset_field(cand, target)
                winner = target
            else:
                winner = None
            cand["status"] = decision
            cand["resolution"] = {
                "reviewer": reviewer,
                "decided_at": now_iso(),
                "winner_id": winner,
            }
            self.store.persist()
            return cand

    def _merge_entities(self, kind: str, old_id: str, new_id: str, candidate_id: str) -> None:
        coll = COLL_BY_KIND[kind]
        old = self.store.require(coll, old_id)
        new = self.store.require(coll, new_id)
        # 名称与别名全部并入保留记录（沿革可溯）
        for term in [old["name"], *old.get("aliases", [])]:
            if normalize_name(term) not in [normalize_name(a) for a in new["aliases"]] and normalize_name(
                term
            ) != normalize_name(new["name"]):
                new["aliases"].append(term)
        self.store.index_name(kind, new)

        # 改挂引用
        asset_field = {
            "person": [],  # 人物在参演/摄影字段
            "organization": ["troupe_id"],
            "place": ["place_id"],
            "genre": ["genre_id"],
            "play": ["play_id"],
        }[kind]
        for field in asset_field:
            for asset in self.store.all("assets"):
                if asset.get(field) == old_id:
                    asset[field] = new_id
        if kind == "person":
            for asset in self.store.all("assets"):
                if asset.get("photographer_id") == old_id:
                    asset["photographer_id"] = new_id
            for part in self.store.all("participations"):
                if part["person_id"] == old_id:
                    part["person_id"] = new_id
        # 授权改挂
        for lic in self.store.all("licenses"):
            if lic["holder_kind"] == kind and lic["holder_id"] == old_id:
                lic["holder_id"] = new_id
                lic.setdefault("merged_from", []).append(old_id)

        old["status"] = IDENTITY_MERGED
        old["merged_into"] = new_id
        old["merged_via_candidate"] = candidate_id
        old["merged_at"] = now_iso()
        new["status"] = IDENTITY_CONFIRMED

    def _reattach_asset_field(self, cand: dict[str, Any], target: str) -> None:
        asset = self.store.get("assets", cand["asset_id"])
        if asset is None:
            return
        field = cand["field"]
        if field == "place":
            asset["place_id"] = target
        elif field == "genre":
            asset["genre_id"] = target
        elif field == "play":
            asset["play_id"] = target
        elif field == "troupe":
            asset["troupe_id"] = target
        elif field == "photographer":
            asset["photographer_id"] = target
        elif field == "performer":
            for part in self.store.all("participations"):
                if part["id"] == cand.get("participation_id"):
                    part["person_id"] = target
                    part["certainty"] = IDENTITY_CONFIRMED

    def confirm_identity(self, kind: str, record_id: str) -> None:
        """馆员确认某临时实体身份无疑（非合并路径）。"""
        with self.store.lock:
            self.store.require(COLL_BY_KIND[kind], record_id)["status"] = IDENTITY_CONFIRMED
            self.store.persist()

    # ============================================================== 授权与撤回

    def grant_license(
        self,
        asset_id: str,
        *,
        holder_kind: str,
        holder_id: str,
        purposes: list[str],
        scope_note: str = "",
        source: str,
        granted_at: str | None = None,
    ) -> str:
        """登记一项许可。holder 为作者(摄影者)/演员/院团/出版方之一。

        同一资产同一权利方重复登记视为补充用途：并集合并，保持幂等。
        """
        if holder_kind not in (PERSON, ORGANIZATION):
            raise ValidationError("holder_kind 只能是 person / organization")
        bad = [p for p in purposes if p not in PURPOSES]
        if bad:
            raise ValidationError(f"未知用途: {bad}")
        if not purposes:
            raise ValidationError("许可至少包含一种用途")
        with self.store.lock:
            asset = self.store.require("assets", asset_id)
            holder_id = self.store.canonical(
                holder_kind, holder_id
            )
            self.store.require(COLL_BY_KIND[holder_kind], holder_id)

            for lic in self.store.all("licenses"):
                if (
                    lic["asset_id"] == asset_id
                    and lic["holder_kind"] == holder_kind
                    and lic["holder_id"] == holder_id
                    and lic["status"] == LICENSE_ACTIVE
                ):
                    merged = sorted(set(lic["purposes"]) | set(purposes))
                    if merged != lic["purposes"]:
                        lic["purposes"] = merged
                        lic.setdefault("events", []).append(
                            {
                                "at": now_iso(),
                                "action": "amend_purposes",
                                "purposes": list(purposes),
                                "source": source,
                            }
                        )
                        self.store.persist()
                    return lic["id"]

            lic_id = self.store.next_id("lic")
            self.store.insert(
                "licenses",
                {
                    "id": lic_id,
                    "asset_id": asset_id,
                    "holder_kind": holder_kind,
                    "holder_id": holder_id,
                    "purposes": sorted(set(purposes)),
                    "status": LICENSE_ACTIVE,
                    "scope_note": scope_note,
                    "source": source,
                    "granted_at": granted_at or today(),
                    "events": [
                        {
                            "at": now_iso(),
                            "action": "grant",
                            "purposes": sorted(set(purposes)),
                            "source": source,
                        }
                    ],
                },
            )
            self.store.persist()
            return lic_id

    def withdraw_license(
        self, license_id: str, *, purposes: list[str] | None = None, source: str, reason: str
    ) -> None:
        """撤回许可：只影响撤回之后的使用核对。

        purposes 给定时仅撤回这些用途（部分撤回），否则整项撤回。
        历史使用记录保持不变——已发生的出版与展出继续留痕。
        """
        with self.store.lock:
            lic = self.store.require("licenses", license_id)
            if lic["status"] != LICENSE_ACTIVE:
                raise Conflict("该许可已处于撤回状态")
            targets = purposes or lic["purposes"]
            bad = [p for p in targets if p not in PURPOSES]
            if bad:
                raise ValidationError(f"未知用途: {bad}")
            remaining = [p for p in lic["purposes"] if p not in targets]
            event = {
                "at": now_iso(),
                "action": "withdraw",
                "purposes": list(targets),
                "source": source,
                "reason": reason,
            }
            lic.setdefault("events", []).append(event)
            if remaining:
                lic["purposes"] = remaining
            else:
                lic["status"] = LICENSE_WITHDRAWN
                lic["withdrawn_at"] = today()
                lic["withdraw_reason"] = reason
            self.store.persist()

    def _license_rows(self, asset_id: str) -> list[dict[str, Any]]:
        return [
            lic
            for lic in self.store.all("licenses")
            if lic["asset_id"] == asset_id
        ]

    def check_permission(
        self,
        asset_id: str,
        purpose: str,
        *,
        on_date: str | None = None,
        publisher_id: str | None = None,
    ) -> dict[str, Any]:
        """按用途核对许可：列出每个权利方当前是否授权。

        权利方 = 摄影者(作者)、参演演员、院团；登记出版使用时还应传入出版方核对。
        任一方未授权或已撤回即不得使用。撤回即时影响「未来」核对，
        历史使用以登记时快照为准。
        """
        if purpose not in PURPOSES:
            raise ValidationError(f"未知用途: {purpose}")
        with self.store.lock:
            asset = self.store.require("assets", asset_id)
            if publisher_id:
                publisher_id = self.store.canonical("organization", publisher_id)
                self.store.require("organizations", publisher_id)
            rows = self._license_rows(asset_id)
            active = [
                lic for lic in rows
                if lic["status"] == LICENSE_ACTIVE and purpose in lic["purposes"]
            ]
            holders: dict[tuple[str, str], dict[str, Any]] = {}
            for lic in active:
                key = (lic["holder_kind"], lic["holder_id"])
                holders[key] = {"holder_kind": lic["holder_kind"], "holder_id": lic["holder_id"]}

            required: list[dict[str, str]] = []
            if asset.get("photographer_id"):
                required.append(
                    {"holder_kind": PERSON, "holder_id": asset["photographer_id"], "role": "作者"}
                )
            if asset.get("troupe_id"):
                required.append(
                    {"holder_kind": ORGANIZATION, "holder_id": asset["troupe_id"], "role": "院团"}
                )
            for part in self.store.all("participations"):
                if part["asset_id"] == asset_id and part["person_id"]:
                    required.append(
                        {"holder_kind": PERSON, "holder_id": part["person_id"], "role": "演员"}
                    )
            if publisher_id:
                required.append(
                    {"holder_kind": ORGANIZATION, "holder_id": publisher_id, "role": "出版方"}
                )

            missing, covered = [], []
            for req in required:
                key = (req["holder_kind"], req["holder_id"])
                if key in holders:
                    covered.append({**req, "license_id": next(
                        lic["id"] for lic in active
                        if lic["holder_kind"] == req["holder_kind"]
                        and lic["holder_id"] == req["holder_id"]
                    )})
                else:
                    missing.append(req)

            return {
                "asset_id": asset_id,
                "purpose": purpose,
                "permitted": not missing,
                "authorized_holders": covered,
                "missing_holders": missing,
                "checked_at": now_iso(),
            }

    def register_usage(
        self,
        asset_id: str,
        *,
        purpose: str,
        publisher_id: str | None = None,
        venue: str = "",
        published_at: str | None = None,
        source: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """登记一次实际使用（出版/展览/网络发布/研究引用）。

        使用发生即不可变留痕；登记时快照核对结论，即便日后撤回也不抹除。
        未通过核对默认拒绝；force 仅用于补录撤回前已发生的使用，必须注明来源。
        """
        if purpose not in PURPOSES:
            raise ValidationError(f"未知用途: {purpose}")
        with self.store.lock:
            self.store.require("assets", asset_id)
            if publisher_id:
                publisher_id = self.store.canonical("organization", publisher_id)
                pub = self.store.require("organizations", publisher_id)
                if pub.get("org_type") not in (PUBLISHER, "other"):
                    raise ValidationError("出版方必须是 publisher 类型机构")
            check = self.check_permission(asset_id, purpose, publisher_id=publisher_id)
            if not check["permitted"] and not force:
                raise Conflict(
                    f"许可核对未通过，缺少权利方授权: "
                    f"{[m['role'] + ':' + m['holder_id'] for m in check['missing_holders']]}"
                )
            record = {
                "id": self.store.next_id("use"),
                "asset_id": asset_id,
                "purpose": purpose,
                "publisher_id": publisher_id,
                "venue": venue,
                "used_at": published_at or today(),
                "source": source,
                "recorded_at": now_iso(),
                "immutable": True,
                "permission_snapshot": {
                    "permitted": check["permitted"],
                    "authorized_holders": check["authorized_holders"],
                    "missing_holders": check["missing_holders"],
                },
                "forced_backfill": force and not check["permitted"],
            }
            self.store.insert("usages", record)
            self.store.persist()
            return record

    # ============================================================== 检索（研究者视图）

    def search_by_genre(
        self, genre_ref: str, *, purpose: str = "research"
    ) -> dict[str, Any]:
        """研究者按剧种检索。

        * 获准内容：返回完整元数据与当前说明；
        * 受限影像（该用途许可不全）：只返回指纹/日期等最小信息并标注「受限」；
        * 未确认身份的人物/团体显式标注 provisional，不伪装成定论；
        * 剧种归类按当前版本过滤，但其勘误版本数一并显示。
        """
        with self.store.lock:
            resolved = self.store.resolve_name("genre", genre_ref)
            if resolved["state"] == "resolved":
                genre_id = resolved["id"]
            elif self.store.get("genres", genre_ref):
                genre_id = self.store.canonical("genre", genre_ref)
            else:
                if resolved["state"] == "ambiguous":
                    raise Conflict(f"剧种名称「{genre_ref}」指向多个分类版本，请用 id 检索")
                raise NotFound("genre", genre_ref)

            genre = self.store.require("genres", genre_id)
            items: list[dict[str, Any]] = []
            for asset in self.store.all("assets"):
                if asset.get("genre_id") != genre_id:
                    continue
                check = self.check_permission(asset["id"], purpose)
                item: dict[str, Any] = {
                    "asset_id": asset["id"],
                    "captured_at": asset["captured_at"],
                    "genre": {"id": genre_id, "name": genre["name"]},
                    "genre_classification_version": asset["genre_current_version"],
                }
                if check["permitted"]:
                    item["access"] = "granted"
                    item.update(self._asset_detail(asset))
                else:
                    item["access"] = "restricted"
                    item["restriction"] = {
                        "reason": "该影像当前用途许可不完整或已撤回",
                        "missing": [
                            {"role": m["role"], "holder_id": m["holder_id"]}
                            for m in check["missing_holders"]
                        ],
                    }
                    item["file_fingerprint"] = asset["file_fingerprint"]
                    item["batch_code"] = asset["batch_code"]
                items.append(item)
            return {
                "genre": {"id": genre_id, "name": genre["name"]},
                "purpose": purpose,
                "count": len(items),
                "items": items,
            }

    def _asset_detail(self, asset: dict[str, Any]) -> dict[str, Any]:
        detail: dict[str, Any] = {}
        cap = self.store.get(
            "caption_versions", f"{asset['id']}#cap-v{asset['caption_current_version']}"
        )
        detail["caption"] = cap["text"] if cap else None
        detail["caption_version"] = asset["caption_current_version"]

        place = self.store.get("places", asset.get("place_id") or "") if asset.get("place_id") else None
        if place:
            detail["place"] = self._entity_view("place", place)
        troupe = self.store.get("organizations", asset.get("troupe_id") or "") if asset.get("troupe_id") else None
        if troupe:
            detail["troupe"] = self._entity_view("organization", troupe)
        play = self.store.get("plays", asset.get("play_id") or "") if asset.get("play_id") else None
        if play:
            detail["play"] = self._entity_view("play", play)
        photographer = (
            self.store.get("persons", asset["photographer_id"])
            if asset.get("photographer_id")
            else None
        )
        if photographer:
            detail["photographer"] = self._entity_view("person", photographer)

        performers = []
        for part in sorted(
            (p for p in self.store.all("participations") if p["asset_id"] == asset["id"]),
            key=lambda p: p["id"],
        ):
            person = self.store.get("persons", part.get("person_id") or "")
            # 该片中的身份确认程度以参演关系为准；人物本身仍可能是临时登记
            performers.append(
                {
                    "person": self._entity_view("person", person) if person else None,
                    "role": part["role"],
                    "identity": part["certainty"],
                    "unconfirmed": part["certainty"] == IDENTITY_PROVISIONAL,
                }
            )
        detail["performers"] = performers
        pending = [
            c["id"]
            for c in self.store.all("candidates")
            if c["asset_id"] == asset["id"] and c["status"] == CANDIDATE_PENDING
        ]
        detail["pending_candidates"] = pending
        return detail

    def _entity_view(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"],
            "name": record["name"],
            "aliases": record.get("aliases", []),
            "identity_status": record["status"],
            "unconfirmed": record["status"] == IDENTITY_PROVISIONAL,
            "merged_into": record.get("merged_into"),
        }

    # ============================================================== 溯源

    def provenance(self, record_kind: str, record_id: str) -> dict[str, Any]:
        """从任一条记录回到最初说明、每次勘误依据与关联沿革。

        record_kind: asset / person / organization / place / genre / play / license
        """
        with self.store.lock:
            result: dict[str, Any] = {"record_kind": record_kind, "record_id": record_id}
            if record_kind == "asset":
                asset = self.store.require("assets", record_id)
                caps = sorted(
                    (c for c in self.store.all("caption_versions") if c["asset_id"] == record_id),
                    key=lambda c: c["version"],
                )
                gvs = sorted(
                    (g for g in self.store.all("genre_versions") if g["asset_id"] == record_id),
                    key=lambda g: g["version"],
                )
                result["import_source"] = asset["source"]
                result["batch_code"] = asset["batch_code"]
                result["file_fingerprint"] = asset["file_fingerprint"]
                result["caption_versions"] = [
                    {
                        "version": c["version"],
                        "text": c["text"],
                        "source": c["source"],
                        "basis": c["basis"],
                        "recorded_at": c["recorded_at"],
                    }
                    for c in caps
                ]
                result["genre_versions"] = [
                    {
                        "version": g["version"],
                        "genre_id": g["genre_id"],
                        "genre_name": self.store.require("genres", g["genre_id"])["name"],
                        "source": g["source"],
                        "basis": g["basis"],
                        "recorded_at": g["recorded_at"],
                    }
                    for g in gvs
                ]
                result["usages"] = [
                    {
                        "id": u["id"],
                        "purpose": u["purpose"],
                        "used_at": u["used_at"],
                        "venue": u["venue"],
                        "permission_snapshot": u["permission_snapshot"],
                    }
                    for u in sorted(
                        (u for u in self.store.all("usages") if u["asset_id"] == record_id),
                        key=lambda u: u["used_at"],
                    )
                ]
            elif record_kind in COLL_BY_KIND:
                record = self.store.require(COLL_BY_KIND[record_kind], record_id)
                result["name"] = record["name"]
                result["aliases"] = record.get("aliases", [])
                result["status"] = record["status"]
                result["registered_source"] = record["source"]
                if record_kind == "place":
                    result["history"] = [
                        {
                            "from_name": h["from_name"],
                            "to_name": h["to_name"],
                            "since": h["since"],
                            "source": h["source"],
                        }
                        for h in self.store.all("place_history")
                        if h["place_id"] == record_id
                    ]
                if record_kind == "organization":
                    result["name_history"] = record.get("name_history", [])
                if record.get("status") == IDENTITY_MERGED:
                    result["merged_into"] = record["merged_into"]
                    result["merged_via_candidate"] = record["merged_via_candidate"]
            elif record_kind == "license":
                lic = self.store.require("licenses", record_id)
                result["status"] = lic["status"]
                result["purposes"] = lic["purposes"]
                result["events"] = lic.get("events", [])
            else:
                raise ValidationError(f"未知记录类型: {record_kind}")
            return result
