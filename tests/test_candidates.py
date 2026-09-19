"""疑似同一人物、戏班、场所只生成候选关系，合并与否由人工判断。"""

import unittest

import support


class CandidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()
        self.result = support.import_seed(self.service)
        self.asset_id = self.result["items"][0]["asset_id"]

    def tearDown(self) -> None:
        self.service.close()

    def _mention_of(self, alias: str) -> dict:
        rows = self.service.store.all(
            "SELECT * FROM mentions WHERE alias_text=?", (alias,)
        )
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_mentions_do_not_auto_link(self) -> None:
        # 即使馆员已登记同名人物，导入也只生成候选，不自动挂接
        self.service.close()
        self.service = support.make_service()
        person = self.service.create_person("王爱莲", actor="馆员乙")
        result = support.import_seed(self.service)
        asset_id = result["items"][0]["asset_id"]
        linked = self.service.store.all(
            "SELECT * FROM asset_persons WHERE asset_id=?", (asset_id,)
        )
        self.assertEqual(linked, [])
        pending = self.service.list_candidates(status="pending")
        self.assertTrue(any(c["right_id"] == person["person_id"] for c in pending))

    def test_confirm_mention_to_person_links_asset(self) -> None:
        person = self.service.create_person("王爱莲", actor="馆员乙")
        mention = self._mention_of("王爱莲")
        cand = self.service.propose_candidate(
            "person",
            {"type": "mention", "id": mention["mention_id"]},
            {"type": "person", "id": person["person_id"]},
            "同名，疑似同一人",
            actor="馆员乙",
        )
        # 重复提出同一对关系幂等
        again = self.service.propose_candidate(
            "person",
            {"type": "person", "id": person["person_id"]},
            {"type": "mention", "id": mention["mention_id"]},
            "同名，疑似同一人",
            actor="馆员乙",
        )
        self.assertEqual(cand["candidate_id"], again["candidate_id"])

        resolved = self.service.resolve_candidate(
            cand["candidate_id"], "confirm", actor="馆员乙", rationale="胶片卡片笔迹一致"
        )
        self.assertEqual(resolved["status"], "confirmed")
        linked = self.service.store.all(
            "SELECT * FROM asset_persons WHERE asset_id=? AND person_id=?",
            (self.asset_id, person["person_id"]),
        )
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["role"], "演员")
        # 已判断的候选关系再次提交不重复执行
        replay = self.service.resolve_candidate(cand["candidate_id"], "reject", actor="馆员乙")
        self.assertEqual(replay["status"], "confirmed")

    def test_reject_keeps_identity_unconfirmed(self) -> None:
        person = self.service.create_person("王爱莲", actor="馆员乙")
        mention = self._mention_of("王爱莲")
        cand = self.service.propose_candidate(
            "person",
            {"type": "mention", "id": mention["mention_id"]},
            {"type": "person", "id": person["person_id"]},
            "同名",
            actor="馆员乙",
        )
        self.service.resolve_candidate(
            cand["candidate_id"], "reject", actor="馆员乙", rationale="年龄不符"
        )
        mention_after = self._mention_of("王爱莲")
        self.assertEqual(mention_after["status"], "unconfirmed")
        self.assertEqual(
            self.service.store.all(
                "SELECT * FROM asset_persons WHERE asset_id=?", (self.asset_id,)
            ),
            [],
        )

    def test_mention_pair_confirm_creates_entity(self) -> None:
        # 另一批资料也提到“王爱莲” → 生成提及对提及的候选
        payload = support.field_batch_payload()
        payload["items"][0]["file_fingerprint"] = "sha256:another"
        payload["items"][0]["caption"]["text"] = "另一场演出"
        self.service.import_field_batch(payload, import_key="field-1990-01", actor="馆员甲")
        pair = [
            c for c in self.service.list_candidates(status="pending")
            if c["left_type"] == "mention" and c["right_type"] == "mention"
            and c["entity_type"] == "person" and "王爱莲" in c["reason"]
        ]
        self.assertTrue(pair)
        resolved = self.service.resolve_candidate(
            pair[0]["candidate_id"], "confirm", actor="馆员乙", rationale="同一戏班同期演员"
        )
        self.assertEqual(resolved["status"], "confirmed")
        linked = self.service.store.all(
            "SELECT DISTINCT person_id FROM asset_persons WHERE role='演员'"
        )
        self.assertEqual(len(linked), 1)

    def test_merge_persons_keeps_both_traces(self) -> None:
        older = self.service.create_person("王爱莲", actor="馆员乙", aliases=["王艾莲"])
        newer = self.service.create_person("王爱莲", actor="馆员丙")
        mention = self._mention_of("王爱莲")
        self.service.link_mention(mention["mention_id"], newer["person_id"], actor="馆员丙")
        cand = self.service.propose_candidate(
            "person",
            {"type": "person", "id": older["person_id"]},
            {"type": "person", "id": newer["person_id"]},
            "同名且别名互见",
            actor="馆员乙",
        )
        self.service.resolve_candidate(
            cand["candidate_id"], "confirm", actor="馆员乙",
            rationale="口述确认", survivor_id=older["person_id"],
        )
        loser = self.service.get_person(newer["person_id"])
        self.assertEqual(loser["merged_into"], older["person_id"])
        winner = self.service.get_person(older["person_id"])
        self.assertIn("王艾莲", [a["alias"] for a in winner["aliases"]])
        linked = self.service.store.all(
            "SELECT * FROM asset_persons WHERE asset_id=?", (self.asset_id,)
        )
        self.assertEqual(linked[0]["person_id"], older["person_id"])
        # 合并全程留痕
        trail = self.service.lineage("person", newer["person_id"])
        self.assertTrue(any(e["action"] == "person.merged_into" for e in trail))

    def test_place_mention_generates_candidate_not_link(self) -> None:
        place = self.service.create_place("晋中某村古戏台", actor="馆员乙")
        payload = support.field_batch_payload()
        payload["items"][0]["file_fingerprint"] = "sha256:place-test"
        result = self.service.import_field_batch(payload, import_key="k-place", actor="馆员甲")
        asset_id = result["items"][0]["asset_id"]
        self.assertEqual(
            self.service.store.all(
                "SELECT * FROM asset_places WHERE asset_id=?", (asset_id,)
            ),
            [],
        )
        pending = self.service.list_candidates(status="pending", entity_type="place")
        self.assertTrue(any(c["right_id"] == place["place_id"] for c in pending))


if __name__ == "__main__":
    unittest.main()
