"""按剧种检索的可见性：研究者只见获准内容，但能识别受限影像与未确认身份。"""

import unittest

import support


class SearchVisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()
        self.service.create_genre("晋剧", actor="编目员", aliases=["山西梆子"])
        result = support.import_seed(self.service)
        self.asset_id = result["items"][0]["asset_id"]

    def tearDown(self) -> None:
        self.service.close()

    def _license_everything(self, purpose: str = "research") -> None:
        author = self.service.create_person("赵摄", actor="馆员甲")
        performer = self.service.create_person("王爱莲", actor="馆员甲")
        troupe = self.service.create_troupe("晋中红旗剧团", actor="馆员甲")
        self.service.create_place("晋中某村古戏台", actor="馆员甲")
        for cand in self.service.list_candidates(status="pending"):
            self.service.resolve_candidate(
                cand["candidate_id"], "confirm", actor="馆员甲", rationale="底片袋标注"
            )
        for subject_type, subject_id in (
            ("author", author["person_id"]),
            ("performer", performer["person_id"]),
            ("troupe", troupe["troupe_id"]),
        ):
            self.service.grant_license(
                subject_type, subject_id, purpose, actor="版权员", granted_at="2020-01-01"
            )

    def test_restricted_asset_shows_stub_without_content(self) -> None:
        results = self.service.search_by_genre("晋剧", purpose="research", viewer="researcher")
        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertTrue(item["restricted"])
        self.assertTrue(item["identity_unconfirmed"])
        # 受限影像可识别（编号、年代、批次、受限原因），但看不到内容
        self.assertEqual(item["asset_id"], self.asset_id)
        self.assertEqual(item["captured_at"], "1989-04-18")
        self.assertIn("missing", item)
        self.assertNotIn("caption", item)
        self.assertNotIn("fingerprint", item)
        self.assertNotIn("mentions", item)

    def test_licensed_asset_shows_content_and_unconfirmed_flags(self) -> None:
        self._license_everything()
        results = self.service.search_by_genre("晋剧", purpose="research", viewer="researcher")
        item = results[0]
        self.assertFalse(item["restricted"])
        self.assertEqual(item["caption"]["text"], "晋剧《打金枝》演出后台，演员候场")
        self.assertEqual(item["fingerprint"], "sha256:9f2c1e")
        # 身份已全部确认
        self.assertFalse(item["identity_unconfirmed"])
        self.assertEqual(
            sorted(p["display_name"] for p in item["persons"]), ["王爱莲", "赵摄"]
        )

    def test_unconfirmed_identity_is_visible_to_researcher(self) -> None:
        # 只确认摄影作者并授权，演员仍是未确认提及
        author = self.service.create_person("赵摄", actor="馆员甲")
        troupe = self.service.create_troupe("晋中红旗剧团", actor="馆员甲")
        for cand in self.service.list_candidates(status="pending"):
            mention_side = [
                s for s in (cand["left_id"], cand["right_id"])
                if s.startswith("mention-")
            ]
            # 只确认作者与院团相关候选，演员候选保持 pending
            if "王爱莲" in cand["reason"]:
                continue
            self.service.resolve_candidate(
                cand["candidate_id"], "confirm", actor="馆员甲", rationale="底片袋标注"
            )
        for subject_type, subject_id in (
            ("author", author["person_id"]),
            ("troupe", troupe["troupe_id"]),
        ):
            self.service.grant_license(
                subject_type, subject_id, "research", actor="版权员", granted_at="2020-01-01"
            )
        results = self.service.search_by_genre("晋剧", purpose="research", viewer="researcher")
        item = results[0]
        self.assertTrue(item["identity_unconfirmed"])
        unconfirmed = [m for m in item["mentions"] if m["status"] == "unconfirmed"]
        self.assertTrue(any(m["alias_text"] == "王爱莲" for m in unconfirmed))

    def test_archivist_sees_everything(self) -> None:
        results = self.service.search_by_genre("晋剧", purpose="research", viewer="archivist")
        item = results[0]
        self.assertTrue(item["restricted"])
        self.assertIn("caption", item)
        self.assertIn("mentions", item)
        self.assertIn("pending_candidates", item)
        self.assertTrue(item["identity_unconfirmed"])

    def test_genre_alias_resolves(self) -> None:
        results = self.service.search_by_genre("山西梆子", viewer="researcher")
        self.assertEqual(len(results), 1)

    def test_unknown_genre_raises(self) -> None:
        from jin_opera_archive import NotFoundError

        with self.assertRaises(NotFoundError):
            self.service.search_by_genre("不存在的剧种")


if __name__ == "__main__":
    unittest.main()
