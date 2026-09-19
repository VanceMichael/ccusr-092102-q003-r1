"""田野资料回传的幂等性：离线重复导入不得产生重复记录。"""

import unittest

import support


class ImportIdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()

    def tearDown(self) -> None:
        self.service.close()

    def _counts(self) -> dict:
        store = self.service.store
        return {
            table: store.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in ("assets", "captions", "mentions", "candidates", "events", "batches")
        }

    def test_first_import_creates_records(self) -> None:
        result = support.import_seed(self.service)
        self.assertFalse(result["replayed"])
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["created"])
        # 三个人名/团体名/地名提及 + 剧目、说明首版
        self.assertEqual(len(result["mentions"]), 4)
        captions = self.service.caption_history(result["items"][0]["asset_id"])
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0]["version"], 1)
        self.assertEqual(captions[0]["basis"], "胶片卡片原文")
        self.assertEqual(captions[0]["sources"][0]["kind"], "film_card")

    def test_replay_same_import_key_is_noop(self) -> None:
        first = support.import_seed(self.service)
        before = self._counts()
        second = support.import_seed(self.service)
        after = self._counts()
        self.assertTrue(second["replayed"])
        second.pop("replayed")
        first.pop("replayed")
        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_same_fingerprint_other_import_does_not_duplicate(self) -> None:
        first = support.import_seed(self.service, import_key="field-1989-04")
        asset_id = first["items"][0]["asset_id"]
        payload = support.field_batch_payload()
        result = self.service.import_field_batch(
            payload, import_key="field-1989-04-resync", actor="馆员甲"
        )
        self.assertFalse(result["items"][0]["created"])
        self.assertEqual(result["items"][0]["asset_id"], asset_id)
        counts = self._counts()
        self.assertEqual(counts["assets"], 1)
        self.assertEqual(counts["captions"], 1)
        # 提及按内容寻址，重复回传也不重复
        self.assertEqual(counts["mentions"], 4)

    def test_conflicting_caption_is_not_silently_versioned(self) -> None:
        first = support.import_seed(self.service)
        asset_id = first["items"][0]["asset_id"]
        payload = support.field_batch_payload()
        payload["items"][0]["caption"]["text"] = "被另一台设备改写过的说明"
        result = self.service.import_field_batch(
            payload, import_key="field-1989-04-edited", actor="馆员甲"
        )
        self.assertTrue(any("caption_conflict" in w for w in result["items"][0]["warnings"]))
        versions = self.service.caption_history(asset_id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["text"], "晋剧《打金枝》演出后台，演员候场")

    def test_unresolved_genre_label_warns_without_blocking(self) -> None:
        payload = support.field_batch_payload()
        payload["items"][0]["genre_label"] = "未登记小剧种"
        result = self.service.import_field_batch(payload, import_key="k1", actor="馆员甲")
        self.assertTrue(any("genre_unresolved" in w for w in result["items"][0]["warnings"]))


if __name__ == "__main__":
    unittest.main()
