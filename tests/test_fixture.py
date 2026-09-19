"""检查影像档案样例。"""

import json
from pathlib import Path
import unittest


class ArchiveFixtureTest(unittest.TestCase):
    def test_asset_has_fingerprint_and_caption_version(self) -> None:
        payload = json.loads(Path("fixtures/archive_item.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["file_fingerprint"].startswith("sha256:"))
        self.assertGreaterEqual(payload["caption_version"], 1)


if __name__ == "__main__":
    unittest.main()
