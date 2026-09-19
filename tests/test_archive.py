"""领域服务测试：覆盖需求中的幂等、候选、勘误、许可、检索、溯源规则。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jin_opera_archive.service import ArchiveService
from jin_opera_archive.store import Conflict, NotFound, Store, ValidationError


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "archive.json"
        self.service = ArchiveService(Store(self.db))
        svc = self.service
        # 两个剧种
        self.genre_jin = svc.register_genre("晋剧", aliases=["中路梆子"], source="《山西戏曲志》")
        self.genre_pu = svc.register_genre("蒲剧", source="《山西戏曲志》")
        # 人物：摄影者与两位演员
        self.photographer = svc.register_person("王馆长", source="工作证")
        self.actor_a = svc.register_person("张三红", aliases=["红生张三"], source="1989 年田野卡片")
        self.actor_b = svc.register_person("李月梅", source="口述记录")
        # 院团（戏班曾改名）与出版方
        self.troupe = svc.register_organization(
            "晋中晋剧团", org_type="troupe", aliases=["晋中地区晋剧团"], source="剧团档案"
        )
        self.publisher = svc.register_organization("山西音像出版社", org_type="publisher", source="版权页")
        self.place = svc.register_place("晋中乡村古戏台", source="1989 年田野卡片")
        self.play = svc.register_play("打金枝", source="传统剧目")
        # 批次
        svc.register_batch("field-1989-04", photographer_id=self.photographer, source="离线回传包")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _asset_payload(self, **over):
        payload = {
            "batch_code": "field-1989-04",
            "client_ref": "cam-a-0007",
            "file_fingerprint": "sha256:abc0007",
            "captured_at": "1989-04-18",
            "source": "胶片卡片 #7",
            "place_name": "晋中乡村古戏台",
            "genre_name": "晋剧",
            "play_title": "打金枝",
            "troupe_name": "晋中晋剧团",
            "photographer_name": "王馆长",
            "caption": "张三红演《打金枝》，晋中乡村戏台。",
            "caption_basis": "胶片卡片原话",
            "performers": [
                {"name": "张三红", "role": "唐王", "certainty": "confirmed"},
                {"name": "李月梅", "role": "公主"},
            ],
        }
        payload.update(over)
        return payload

    # ------------------------------------------------------------ 幂等导入

    def test_import_is_idempotent_by_client_ref(self) -> None:
        r1 = self.service.import_asset(self._asset_payload())
        self.assertTrue(r1["created"])
        r2 = self.service.import_asset(self._asset_payload(caption="不应写入的重复说明"))
        self.assertFalse(r2["created"])
        self.assertEqual(r1["asset_id"], r2["asset_id"])
        self.assertEqual(r2["dedup"], "client_ref")
        # 只有一条影像，说明仍为第一版
        self.assertEqual(len(list(self.service.store.all("assets"))), 1)
        prov = self.service.provenance("asset", r1["asset_id"])
        self.assertEqual(len(prov["caption_versions"]), 1)

    def test_import_dedups_by_fingerprint_across_batches(self) -> None:
        self.service.register_batch("field-1990-06", source="补传")
        r1 = self.service.import_asset(self._asset_payload())
        r2 = self.service.import_asset(
            self._asset_payload(batch_code="field-1990-06", client_ref="other-9")
        )
        self.assertFalse(r2["created"])
        self.assertEqual(r2["dedup"], "fingerprint")
        self.assertEqual(r1["asset_id"], r2["asset_id"])

    def test_bad_fingerprint_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.import_asset(self._asset_payload(file_fingerprint="not-a-hash"))

    def test_persistence_roundtrip(self) -> None:
        r = self.service.import_asset(self._asset_payload())
        revived = ArchiveService(Store(self.db))
        asset = revived.store.require("assets", r["asset_id"])
        self.assertEqual(asset["file_fingerprint"], "sha256:abc0007")
        # 重载后别名解析仍可用
        got = revived.store.resolve_name("genre", "中路梆子")
        self.assertEqual(got["state"], "resolved")
        self.assertEqual(got["id"], self.genre_jin)

    # ------------------------------------------------------------ 候选关系

    def test_ambiguous_name_only_creates_pending_candidate(self) -> None:
        svc = self.service
        # 同名不同人：两位"王秀英"
        w1 = svc.register_person("王秀英", source="甲县资料")
        w2 = svc.register_person("王秀英", source="乙县资料")
        payload = self._asset_payload(client_ref="cam-a-0010",
                                      file_fingerprint="sha256:abc0010",
                                      performers=[{"name": "王秀英", "role": "花旦"}])
        r = svc.import_asset(payload)
        candidates = svc.list_candidates()
        perf_cands = [c for c in candidates if c["field"] == "performer"]
        self.assertEqual(len(perf_cands), 1)
        self.assertEqual(sorted(perf_cands[0]["candidate_ids"]), sorted([w1, w2]))
        # 未判定前不允许静默挂到其中一人：参演人身份挂起（person_id 为空）
        part = next(p for p in svc.store.all("participations") if p["asset_id"] == r["asset_id"])
        self.assertIsNone(part["person_id"])

    def test_same_troupe_rename_resolves_via_alias_without_candidate(self) -> None:
        svc = self.service
        r = svc.import_asset(
            self._asset_payload(client_ref="cam-a-0011", file_fingerprint="sha256:abc0011",
                                troupe_name="晋中地区晋剧团")  # 曾用名/别名
        )
        asset = svc.store.require("assets", r["asset_id"])
        self.assertEqual(asset["troupe_id"], self.troupe)
        self.assertEqual(
            [c for c in svc.list_candidates() if c["field"] == "troupe"], []
        )

    def test_confirm_candidate_merges_entities_and_reattaches(self) -> None:
        svc = self.service
        # 同一演员跨剧种流动，被分别登记
        other = svc.register_person("程玉英", source="祁县口述")
        merged = svc.register_person("程玉英（艺名）", source="剧团花名册")
        cid = svc.suggest_identity("person", other, merged, reason="师承与年份吻合")
        result = svc.resolve_candidate(
            cid, decision="confirmed", keep_id=other, reviewer="馆员李"
        )
        self.assertEqual(result["status"], "confirmed")
        gone = svc.store.require("persons", merged)
        self.assertEqual(gone["status"], "merged")
        self.assertEqual(gone["merged_into"], other)
        kept = svc.store.require("persons", other)
        self.assertIn("程玉英（艺名）", kept["aliases"])
        # 解析旧名应回到保留记录
        self.assertEqual(svc.store.resolve_name("person", "程玉英（艺名）")["id"], other)
        # 候选不可重复处理
        with self.assertRaises(Conflict):
            svc.resolve_candidate(cid, decision="rejected", reviewer="x")

    def test_reject_candidate_keeps_both(self) -> None:
        svc = self.service
        w1 = svc.register_person("赵同", source="资料甲")
        w2 = svc.register_person("赵同", source="资料乙")
        cid = svc.suggest_identity("person", w1, w2, reason="同名疑似")
        svc.resolve_candidate(cid, decision="rejected", reviewer="馆员李")
        self.assertEqual(svc.store.require("persons", w1)["status"], "provisional")
        self.assertEqual(svc.store.require("persons", w2)["status"], "provisional")
        self.assertEqual(svc.list_candidates(), [])

    # ------------------------------------------------------------ 勘误与溯源

    def test_caption_revisions_keep_every_version_with_basis(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        v2 = svc.revise_caption(
            r["asset_id"], "张三红饰唐王，剧目经核实为《打金枝》。",
            source="1995 年出版说明", basis="与出版方版权页及演员口述互证",
        )
        v3 = svc.revise_caption(
            r["asset_id"], "张三红（时年 42）饰唐王。",
            source="2003 年演员本人回访", basis="演员本人确认年龄",
        )
        self.assertEqual((v2, v3), (2, 3))
        prov = svc.provenance("asset", r["asset_id"])
        texts = [c["text"] for c in prov["caption_versions"]]
        self.assertIn("胶片卡片原话", [c["basis"] for c in prov["caption_versions"]])
        self.assertEqual(texts[0], "张三红演《打金枝》，晋中乡村戏台。")
        self.assertEqual(len(prov["caption_versions"]), 3)
        # 每次勘误依据都在，而不是只剩当前答案
        bases = [c["basis"] for c in prov["caption_versions"]]
        self.assertEqual(bases[1], "与出版方版权页及演员口述互证")
        self.assertEqual(bases[2], "演员本人确认年龄")

    def test_revision_requires_source_and_basis(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        with self.assertRaises(ValidationError):
            svc.revise_caption(r["asset_id"], "新说法", source="", basis="x")
        with self.assertRaises(ValidationError):
            svc.revise_caption(r["asset_id"], "新说法", source="口述", basis="")

    def test_genre_reclassification_versions_filter_search(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        # 后经考证归为蒲剧（举例：分类口径变化）
        svc.classify_genre(r["asset_id"], self.genre_pu,
                           source="2010 年剧种普查", basis="唱腔与曲牌比对")
        jin = svc.search_by_genre("晋剧")
        self.assertEqual(jin["count"], 0)
        pu = svc.search_by_genre("蒲剧")
        self.assertEqual(pu["count"], 1)
        self.assertEqual(pu["items"][0]["genre_classification_version"], 2)
        prov = svc.provenance("asset", r["asset_id"])
        self.assertEqual(len(prov["genre_versions"]), 2)
        self.assertEqual(prov["genre_versions"][0]["genre_name"], "晋剧")
        self.assertEqual(prov["genre_versions"][1]["basis"], "唱腔与曲牌比对")

    def test_place_and_troupe_rename_history(self) -> None:
        svc = self.service
        svc.rename_place(self.place, "榆次老城戏台", since="1992-01-01", source="地名办文件")
        svc.rename_organization(self.troupe, "晋中市晋剧艺术研究院", since="2001-09-01",
                                source="机构编制批复")
        p = svc.provenance("place", self.place)
        self.assertEqual(p["history"][0]["from_name"], "晋中乡村古戏台")
        self.assertIn("晋中乡村古戏台", p["aliases"])
        o = svc.provenance("organization", self.troupe)
        self.assertEqual(o["name_history"][0]["to_name"], "晋中市晋剧艺术研究院")
        # 新名旧名都能解析到同一记录
        self.assertEqual(svc.store.resolve_name("place", "晋中乡村古戏台")["id"], self.place)
        self.assertEqual(svc.store.resolve_name("organization", "晋中晋剧团")["id"], self.troupe)

    # ------------------------------------------------------------ 许可、撤回与留痕

    def _grant_all(self, asset_id, purposes=("research", "exhibition", "web", "publication")):
        svc = self.service
        svc.grant_license(asset_id, holder_kind="person", holder_id=self.photographer,
                          purposes=list(purposes), source="作者授权书 1998")
        svc.grant_license(asset_id, holder_kind="person", holder_id=self.actor_a,
                          purposes=list(purposes), source="演员授权书 1998")
        svc.grant_license(asset_id, holder_kind="person", holder_id=self.actor_b,
                          purposes=list(purposes), source="演员授权书 1998")
        svc.grant_license(asset_id, holder_kind="organization", holder_id=self.troupe,
                          purposes=list(purposes), source="院团授权函 1998")
        svc.grant_license(asset_id, holder_kind="organization", holder_id=self.publisher,
                          purposes=list(purposes), source="出版合作协议")

    def test_permission_requires_every_holder(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        check = svc.check_permission(r["asset_id"], "web")
        self.assertFalse(check["permitted"])
        roles = {m["role"] for m in check["missing_holders"]}
        self.assertEqual(roles, {"作者", "演员", "院团"})
        self._grant_all(r["asset_id"], purposes=["web"])
        self.assertTrue(svc.check_permission(r["asset_id"], "web")["permitted"])
        # 研究用途未授，仍不放行
        self.assertFalse(svc.check_permission(r["asset_id"], "research")["permitted"])

    def test_grant_license_is_idempotent_union(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        l1 = svc.grant_license(r["asset_id"], holder_kind="person",
                               holder_id=self.photographer, purposes=["research"], source="授权书")
        l2 = svc.grant_license(r["asset_id"], holder_kind="person",
                               holder_id=self.photographer, purposes=["web"], source="授权书补充")
        self.assertEqual(l1, l2)
        lic = svc.store.require("licenses", l1)
        self.assertEqual(lic["purposes"], ["research", "web"])

    def test_usage_blocked_then_snapshotted_then_withdrawal_only_future(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        # 未授权不能登记使用
        with self.assertRaises(Conflict):
            svc.register_usage(r["asset_id"], purpose="publication",
                               publisher_id=self.publisher, venue="画册第一版",
                               published_at="2005-05-01", source="版权页")
        self._grant_all(r["asset_id"])
        use = svc.register_usage(
            r["asset_id"], purpose="publication", publisher_id=self.publisher,
            venue="画册第一版", published_at="2005-05-01", source="版权页",
        )
        self.assertTrue(use["permission_snapshot"]["permitted"])

        # 演员撤回网络与出版用途
        actor_lic = next(
            l for l in svc.store.all("licenses")
            if l["asset_id"] == r["asset_id"] and l["holder_id"] == self.actor_a
        )
        svc.withdraw_license(actor_lic["id"], purposes=["web", "publication"],
                             source="2026 年家属声明", reason="家属要求停止传播")

        # 未来使用被拦
        self.assertFalse(svc.check_permission(r["asset_id"], "publication")["permitted"])
        self.assertFalse(svc.check_permission(r["asset_id"], "web")["permitted"])
        # 研究、展览仍可
        self.assertTrue(svc.check_permission(r["asset_id"], "research")["permitted"])
        with self.assertRaises(Conflict):
            svc.register_usage(r["asset_id"], purpose="web", venue="官网", source="撤回复检")
        # 已发生的出版留痕不变，且快照保留当时获准结论
        prov = svc.provenance("asset", r["asset_id"])
        self.assertEqual(len(prov["usages"]), 1)
        self.assertEqual(prov["usages"][0]["venue"], "画册第一版")
        self.assertTrue(prov["usages"][0]["permission_snapshot"]["permitted"])

    def test_publisher_license_required_for_publication(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        # 只授作者/演员/院团，缺出版方：出版核对不放行
        svc.grant_license(r["asset_id"], holder_kind="person", holder_id=self.photographer,
                          purposes=["publication"], source="作者授权书")
        svc.grant_license(r["asset_id"], holder_kind="person", holder_id=self.actor_a,
                          purposes=["publication"], source="演员授权书")
        svc.grant_license(r["asset_id"], holder_kind="person", holder_id=self.actor_b,
                          purposes=["publication"], source="演员授权书")
        svc.grant_license(r["asset_id"], holder_kind="organization", holder_id=self.troupe,
                          purposes=["publication"], source="院团授权函")
        check = svc.check_permission(r["asset_id"], "publication", publisher_id=self.publisher)
        self.assertFalse(check["permitted"])
        self.assertEqual([m["role"] for m in check["missing_holders"]], ["出版方"])
        svc.grant_license(r["asset_id"], holder_kind="organization", holder_id=self.publisher,
                          purposes=["publication"], source="出版合作协议")
        self.assertTrue(
            svc.check_permission(r["asset_id"], "publication",
                                 publisher_id=self.publisher)["permitted"]
        )

    def test_full_withdraw_marks_license_and_partial_keeps_rest(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        lid = svc.grant_license(r["asset_id"], holder_kind="organization",
                                holder_id=self.troupe,
                                purposes=["research", "exhibition"], source="院团函")
        svc.withdraw_license(lid, source="撤销函", reason="合作终止")
        lic = svc.store.require("licenses", lid)
        self.assertEqual(lic["status"], "withdrawn")
        with self.assertRaises(Conflict):
            svc.withdraw_license(lid, source="x", reason="再次撤回")

    # ------------------------------------------------------------ 研究者检索

    def test_search_shows_granted_detail_restricted_marker_and_unconfirmed(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        # 未授权检索：受限影像仍可被识别（知道有这条），但看不到说明等内容
        res = svc.search_by_genre("晋剧")
        self.assertEqual(res["count"], 1)
        item = res["items"][0]
        self.assertEqual(item["access"], "restricted")
        self.assertIn("restriction", item)
        self.assertNotIn("caption", item)
        self.assertEqual(item["file_fingerprint"], "sha256:abc0007")

        # 补齐授权后可见细节，未确认身份（李月梅 provisional 由导入占位/初登即临时）显式标注
        self._grant_all(r["asset_id"], purposes=["research"])
        res2 = svc.search_by_genre("晋剧")
        item2 = res2["items"][0]
        self.assertEqual(item2["access"], "granted")
        self.assertIn("张三红", item2["caption"])
        statuses = {p["person"]["name"]: p["unconfirmed"] for p in item2["performers"]}
        self.assertFalse(statuses["张三红"])
        self.assertTrue(statuses["李月梅"])
        self.assertIn(item2["place"]["name"], "晋中乡村古戏台")

    def test_search_unknown_genre_raises(self) -> None:
        with self.assertRaises(NotFound):
            self.service.search_by_genre("不存在的剧种")

    def test_merge_repoints_licenses(self) -> None:
        svc = self.service
        r = svc.import_asset(self._asset_payload())
        duplicate = svc.register_person("王馆长（曾用署名）", source="老照片签名")
        svc.grant_license(r["asset_id"], holder_kind="person", holder_id=duplicate,
                          purposes=["research"], source="签名比对授权")
        cid = svc.suggest_identity("person", self.photographer, duplicate, reason="笔迹一致")
        svc.resolve_candidate(cid, decision="confirmed", keep_id=self.photographer, reviewer="赵")
        lic = next(l for l in svc.store.all("licenses") if l["asset_id"] == r["asset_id"])
        self.assertEqual(lic["holder_id"], self.photographer)
        self.assertIn(duplicate, lic["merged_from"])


if __name__ == "__main__":
    unittest.main()
