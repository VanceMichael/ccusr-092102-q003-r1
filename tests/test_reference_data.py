"""参考数据：地点沿革、戏班改名、剧种分类版本与标注订正。"""

import unittest

import support


class ReferenceDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_place_name_evolution(self) -> None:
        place = self.service.create_place(
            "晋中某村古戏台", actor="编目员", valid_from="1949-01-01", valid_to="1985-12-31"
        )
        place_id = place["place_id"]
        self.service.add_place_name(
            place_id, "晋中某村人民剧场", actor="编目员",
            valid_from="1986-01-01", valid_to="2003-12-31",
        )
        self.service.add_place_name(
            place_id, "晋中某村文化活动中心", actor="编目员", valid_from="2004-01-01"
        )
        self.assertEqual(
            self.service.place_name_at(place_id, "1989-04-18"), "晋中某村人民剧场"
        )
        self.assertEqual(
            self.service.place_name_at(place_id, "1970-01-01"), "晋中某村古戏台"
        )
        self.assertEqual(
            self.service.place_name_at(place_id, "2020-01-01"), "晋中某村文化活动中心"
        )
        history = self.service.place_history(place_id)
        self.assertEqual(len(history["names"]), 3)
        self.assertTrue(any(e["action"] == "place.alias" for e in history["events"]))

    def test_troupe_rename_keeps_old_name_as_alias(self) -> None:
        troupe = self.service.create_troupe("晋中红旗剧团", actor="编目员")
        troupe_id = troupe["troupe_id"]
        renamed = self.service.rename_troupe(
            troupe_id, "晋中青年晋剧团", valid_from="1992-01-01",
            actor="编目员", old_name_valid_to="1991-12-31",
        )
        self.assertEqual(renamed["display_name"], "晋中青年晋剧团")
        aliases = {a["alias"]: a for a in renamed["aliases"]}
        self.assertEqual(aliases["晋中红旗剧团"]["valid_to"], "1991-12-31")
        self.assertEqual(aliases["晋中青年晋剧团"]["valid_from"], "1992-01-01")
        # 旧名仍归一到同一团体：新导入提及旧名时生成候选而非新建
        result = self.service.import_field_batch(
            {"items": [{"file_fingerprint": "sha256:改名后",
                        "troupes": [{"name": "晋中红旗剧团"}]}]},
            import_key="rename-check", actor="馆员甲",
        )
        pending = self.service.list_candidates(status="pending", entity_type="troupe")
        self.assertTrue(any(c["right_id"] == troupe_id for c in pending))

    def test_genre_classification_versions_coexist(self) -> None:
        jinju = self.service.create_genre("晋剧", actor="编目员")
        bangzi = self.service.create_genre("中路梆子", actor="编目员")
        self.service.create_scheme(
            "scheme-1987", "1987年剧种调查分类", actor="编目员", issued_at="1987",
            entries=[{"genre_id": bangzi["genre_id"]}],
        )
        self.service.create_scheme(
            "scheme-2016", "2016年非遗名录分类", actor="编目员", issued_at="2016",
            entries=[{"genre_id": jinju["genre_id"], "parent_genre_id": bangzi["genre_id"]}],
        )
        result = support.import_seed(self.service)
        asset_id = result["items"][0]["asset_id"]
        # 1987 方案下归为中路梆子，2016 方案下归为晋剧
        self.service.assign_genre(
            asset_id, bangzi["genre_id"], "scheme-1987", actor="编目员"
        )
        self.service.assign_genre(
            asset_id, jinju["genre_id"], "scheme-2016", actor="编目员"
        )
        by_1987 = self.service.search_by_genre(
            "中路梆子", viewer="archivist", scheme_id="scheme-1987"
        )
        self.assertEqual([i["asset_id"] for i in by_1987], [asset_id])
        by_2016 = self.service.search_by_genre(
            "晋剧", viewer="archivist", scheme_id="scheme-2016"
        )
        self.assertEqual([i["asset_id"] for i in by_2016], [asset_id])

    def test_genre_correction_supersedes_but_keeps_trace(self) -> None:
        jinju = self.service.create_genre("晋剧", actor="编目员")
        puju = self.service.create_genre("蒲剧", actor="编目员")
        result = support.import_seed(self.service)
        asset_id = result["items"][0]["asset_id"]
        self.service.assign_genre(
            asset_id, puju["genre_id"], "scheme-original", actor="编目员",
            supersede=True,
        )
        rows = self.service.store.all(
            "SELECT * FROM asset_genres WHERE asset_id=? AND scheme_id='scheme-original'"
            " ORDER BY genre_id",
            (asset_id,),
        )
        status = {r["genre_id"]: r["status"] for r in rows}
        self.assertEqual(status[jinju["genre_id"]], "superseded")
        self.assertEqual(status[puju["genre_id"]], "active")
        # 订正前后两次标注都在事件流中
        actions = [
            e for e in self.service.asset_history(asset_id)["events"]
            if e["action"] == "asset.genre_assign"
        ]
        self.assertEqual(len(actions), 2)

    def test_genre_registration_is_idempotent(self) -> None:
        first = self.service.create_genre("晋剧", actor="编目员")
        again = self.service.create_genre("晋剧", actor="编目员")
        self.assertEqual(first["genre_id"], again["genre_id"])
        count = self.service.store.one("SELECT COUNT(*) AS n FROM genres")
        self.assertEqual(count["n"], 1)


if __name__ == "__main__":
    unittest.main()
