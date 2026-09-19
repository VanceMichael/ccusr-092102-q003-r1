"""许可核对与使用留痕：按作者、演员、院团、出版方×用途；
撤回仅限制未来，已发生的出版与展出继续留痕。"""

import unittest

import support
from jin_opera_archive import NotAuthorizedError


class LicenseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()
        result = support.import_seed(self.service)
        self.asset_id = result["items"][0]["asset_id"]
        # 馆员确认身份：摄影作者、演员、院团、出版方
        self.author = self.service.create_person("赵摄", actor="馆员甲")
        self.performer = self.service.create_person("王爱莲", actor="馆员甲")
        self.troupe = self.service.create_troupe("晋中红旗剧团", actor="馆员甲")
        self.publisher = self.service.create_troupe(
            "三晋出版社", actor="馆员甲", kind="publisher"
        )
        for cand in self.service.list_candidates(status="pending"):
            self.service.resolve_candidate(
                cand["candidate_id"], "confirm", actor="馆员甲", rationale="底片袋标注一致"
            )
        self.service.set_publisher(
            self.asset_id, self.publisher["troupe_id"], actor="馆员甲"
        )

    def tearDown(self) -> None:
        self.service.close()

    def _grant_all(self, purpose: str, granted_at: str = "2020-01-01") -> dict:
        return {
            "author": self.service.grant_license(
                "author", self.author["person_id"], purpose, actor="版权员",
                granted_at=granted_at),
            "performer": self.service.grant_license(
                "performer", self.performer["person_id"], purpose, actor="版权员",
                granted_at=granted_at),
            "troupe": self.service.grant_license(
                "troupe", self.troupe["troupe_id"], purpose, actor="版权员",
                granted_at=granted_at),
            "publisher": self.service.grant_license(
                "publisher", self.publisher["troupe_id"], purpose, actor="版权员",
                granted_at=granted_at),
        }

    def test_authorization_requires_every_subject(self) -> None:
        auth = self.service.authorize(self.asset_id, "research", at="2020-06-01")
        self.assertFalse(auth["ok"])
        self.assertEqual(
            sorted(m["subject_type"] for m in auth["missing"]),
            ["author", "performer", "publisher", "troupe"],
        )
        self._grant_all("research")
        auth = self.service.authorize(self.asset_id, "research", at="2020-06-01")
        self.assertTrue(auth["ok"])
        self.assertEqual(len(auth["licenses"]), 4)
        # 另一用途未获授权
        self.assertFalse(
            self.service.authorize(self.asset_id, "exhibition", at="2020-06-01")["ok"]
        )

    def test_grant_is_idempotent(self) -> None:
        first = self.service.grant_license(
            "author", self.author["person_id"], "research", actor="版权员",
            granted_at="2020-01-01")
        again = self.service.grant_license(
            "author", self.author["person_id"], "research", actor="版权员",
            granted_at="2020-01-01")
        self.assertEqual(first["license_id"], again["license_id"])

    def test_unidentified_author_blocks_authorization(self) -> None:
        # 新导入一条只有指纹的影像：作者身份未确认 → 无法授权
        result = self.service.import_field_batch(
            {"items": [{"file_fingerprint": "sha256:无作者"}]},
            import_key="no-author", actor="馆员甲",
        )
        asset_id = result["items"][0]["asset_id"]
        auth = self.service.authorize(asset_id, "research", at="2020-06-01")
        self.assertFalse(auth["ok"])
        self.assertTrue(any(m.get("unidentified") for m in auth["missing"]))

    def test_usage_requires_license_and_is_idempotent(self) -> None:
        with self.assertRaises(NotAuthorizedError) as ctx:
            self.service.record_usage(
                self.asset_id, "exhibition", "省博物馆戏曲展", actor="策展人",
                at="2020-06-01")
        self.assertTrue(ctx.exception.missing)

        self._grant_all("exhibition")
        usage = self.service.record_usage(
            self.asset_id, "exhibition", "省博物馆戏曲展", actor="策展人",
            at="2020-06-01")
        self.assertFalse(usage["replayed"])
        self.assertEqual(len(usage["license_ids"]), 4)
        replay = self.service.record_usage(
            self.asset_id, "exhibition", "省博物馆戏曲展", actor="策展人",
            at="2020-06-01")
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.service.list_usages(self.asset_id)), 1)

    def test_withdrawal_only_restricts_future_use(self) -> None:
        grants = self._grant_all("publication", granted_at="2020-01-01")
        usage = self.service.record_usage(
            self.asset_id, "publication", "《晋戏图录》第一次印刷", actor="出版编辑",
            at="2020-06-01")
        self.assertFalse(usage["replayed"])

        self.service.withdraw_license(
            grants["performer"]["license_id"], actor="版权员",
            at="2021-01-01", note="演员家属要求撤回")
        # 撤回后：未来时点不再获准
        self.assertFalse(
            self.service.authorize(self.asset_id, "publication", at="2021-06-01")["ok"])
        # 撤回前的时点仍然获准（历史使用合法）
        self.assertTrue(
            self.service.authorize(self.asset_id, "publication", at="2020-06-01")["ok"])
        # 新的使用登记被拒绝
        with self.assertRaises(NotAuthorizedError):
            self.service.record_usage(
                self.asset_id, "publication", "《晋戏图录》第二次印刷", actor="出版编辑",
                at="2021-06-01")
        # 已发生的出版继续留痕
        usages = self.service.list_usages(self.asset_id)
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["venue"], "《晋戏图录》第一次印刷")
        self.assertIn(grants["performer"]["license_id"], usages[0]["license_ids"])
        # 撤回操作本身幂等：重复撤回不改变首次撤回时间
        again = self.service.withdraw_license(
            grants["performer"]["license_id"], actor="版权员", at="2021-02-01")
        self.assertEqual(again["withdrawn_at"], "2021-01-01")


if __name__ == "__main__":
    unittest.main()
