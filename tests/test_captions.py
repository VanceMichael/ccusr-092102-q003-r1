"""说明文本勘误：每次修改生成新版本并记录依据，最初说明永远可回查。"""

import unittest

import support
from jin_opera_archive import NotFoundError, ValidationError


class CaptionHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()
        result = support.import_seed(self.service)
        self.asset_id = result["items"][0]["asset_id"]

    def tearDown(self) -> None:
        self.service.close()

    def test_correction_requires_basis(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.correct_caption(self.asset_id, "新说明", "", actor="馆员甲")

    def test_versions_accumulate_with_basis_and_sources(self) -> None:
        oral = self.service.register_source(
            "oral", "1995年访问演员王爱莲录音第2盒", actor="馆员甲", collector="馆员甲"
        )
        pub = self.service.register_source(
            "publication", "《晋剧志》2001年版第213页", actor="馆员甲"
        )
        v2 = self.service.correct_caption(
            self.asset_id, "晋剧《打金枝》演出后台，左一为王爱莲",
            "据1995年口述访谈补出人物姓名", actor="馆员甲",
            source_ids=[oral["source_id"]], at="1995-08-01T10:00:00+00:00",
        )
        self.assertEqual(v2["version"], 2)
        v3 = self.service.correct_caption(
            self.asset_id, "晋剧《打金枝》演出后台，左一为王爱莲（时年二十一岁）",
            "据《晋剧志》勘定生年", actor="馆员乙",
            source_ids=[pub["source_id"]], at="2002-03-01T10:00:00+00:00",
        )
        self.assertEqual(v3["version"], 3)

        history = self.service.caption_history(self.asset_id)
        self.assertEqual([c["version"] for c in history], [1, 2, 3])
        # 最初说明与各自依据全部保留
        self.assertEqual(history[0]["text"], "晋剧《打金枝》演出后台，演员候场")
        self.assertEqual(history[0]["basis"], "胶片卡片原文")
        self.assertEqual(history[1]["basis"], "据1995年口述访谈补出人物姓名")
        self.assertEqual(history[1]["sources"][0]["kind"], "oral")
        self.assertEqual(history[2]["sources"][0]["kind"], "publication")

    def test_asset_history_traces_back_to_first_caption(self) -> None:
        self.service.correct_caption(
            self.asset_id, "修订说明", "据胶片盒背注记", actor="馆员甲"
        )
        history = self.service.asset_history(self.asset_id)
        actions = [e["action"] for e in history["events"]]
        self.assertIn("asset.create", actions)
        self.assertIn("caption.create", actions)
        self.assertIn("caption.correct", actions)
        correct_events = [e for e in history["events"] if e["action"] == "caption.correct"]
        self.assertEqual(correct_events[0]["rationale"], "据胶片盒背注记")
        self.assertEqual(history["captions"][0]["version"], 1)

    def test_unknown_asset_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.caption_history("asset-不存在")


if __name__ == "__main__":
    unittest.main()
