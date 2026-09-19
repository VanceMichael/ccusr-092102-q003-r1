"""HTTP 接口端到端测试（标准库 urllib，启动随机端口的真实服务）。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jin_opera_archive.api import build_server


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "archive.json")
        self.server = build_server(self.db, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method: str, path: str, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_workflow_over_http(self) -> None:
        # 健康检查
        status, body = self._req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["状态"], "档案服务已启动")

        # 登记剧种、人物、院团、场所、剧目、批次
        _, genre = self._req("POST", "/entities/genres", {"name": "晋剧", "source": "戏曲志"})
        _, photo = self._req("POST", "/entities/persons", {"name": "王摄影", "source": "工作证"})
        _, actor = self._req("POST", "/entities/persons", {"name": "张三红", "source": "卡片"})
        _, troupe = self._req("POST", "/entities/organizations",
                              {"name": "晋中晋剧团", "source": "剧团档案"})
        _, place = self._req("POST", "/entities/places",
                             {"name": "古戏台", "source": "卡片"})
        _, play = self._req("POST", "/entities/plays", {"name": "打金枝", "source": "剧目表"})
        _, batch = self._req("POST", "/batches",
                             {"code": "b-1989", "photographer_id": photo["id"], "source": "回传包"})

        # 幂等导入两次
        asset_payload = {
            "batch_code": "b-1989", "client_ref": "a-1",
            "file_fingerprint": "sha256:deadbeef", "captured_at": "1989-04-18",
            "source": "胶片卡片", "place_id": place["id"], "genre_id": genre["id"],
            "play_id": play["id"], "troupe_id": troupe["id"],
            "photographer_id": photo["id"],
            "caption": "胶片卡片上的原始说明",
            "performers": [{"person_id": actor["id"], "role": "唐王", "certainty": "confirmed"}],
        }
        s1, r1 = self._req("POST", "/assets/import", asset_payload)
        s2, r2 = self._req("POST", "/assets/import", asset_payload)
        self.assertEqual((s1, s2), (200, 200))
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        asset_id = r1["asset_id"]

        # 检索：未授权 -> restricted，且能识别受限影像
        _, search = self._req("GET", "/search?genre=%E6%99%8B%E5%89%A7&purpose=web")
        self.assertEqual(search["items"][0]["access"], "restricted")

        # 各方授权
        for holder in (
            {"holder_kind": "person", "holder_id": photo["id"]},
            {"holder_kind": "person", "holder_id": actor["id"]},
            {"holder_kind": "organization", "holder_id": troupe["id"]},
        ):
            status, _ = self._req("POST", f"/assets/{asset_id}/licenses",
                                  {**holder, "purposes": ["research", "web", "publication"],
                                   "source": "授权书"})
            self.assertEqual(status, 200)

        # 核对通过；检索可见说明
        _, check = self._req("GET", f"/assets/{asset_id}/permission?purpose=web")
        self.assertTrue(check["permitted"])
        _, search2 = self._req("GET", "/search?genre=%E6%99%8B%E5%89%A7")
        self.assertEqual(search2["items"][0]["access"], "granted")
        self.assertIn("胶片卡片", search2["items"][0]["caption"])

        # 勘误说明必须带依据；溯源保留两版
        status, err = self._req("POST", f"/assets/{asset_id}/captions",
                                {"text": "无依据的改写", "source": "x", "basis": ""})
        self.assertEqual(status, 400)
        self._req("POST", f"/assets/{asset_id}/captions",
                  {"text": "经演员回访订正的说明", "source": "2003 回访", "basis": "演员本人确认"})
        _, prov = self._req("GET", f"/assets/{asset_id}/provenance")
        self.assertEqual(len(prov["caption_versions"]), 2)
        self.assertEqual(prov["caption_versions"][0]["basis"], "初次著录")

        # 出版使用留痕，随后撤回网络用途：未来被拦、历史留痕不变
        _, pub = self._req("POST", "/entities/organizations",
                           {"name": "山西音像出版社", "org_type": "publisher", "source": "版权页"})
        self._req("POST", f"/assets/{asset_id}/licenses",
                  {"holder_kind": "organization", "holder_id": pub["id"],
                   "purposes": ["publication"], "source": "出版合作协议"})
        _, use = self._req("POST", f"/assets/{asset_id}/usages",
                           {"purpose": "publication", "publisher_id": pub["id"],
                            "venue": "画册", "published_at": "2005-05-01", "source": "版权页"})
        self.assertTrue(use["immutable"])
        web_lic = next(
            l for l in check["authorized_holders"] if l["role"] == "演员"
        )
        # 找到演员许可 id 再撤回
        _, check_after_grants = self._req("GET", f"/assets/{asset_id}/permission?purpose=web")
        actor_lic = next(
            h["license_id"] for h in check_after_grants["authorized_holders"]
            if h["holder_id"] == actor["id"]
        )
        status, _ = self._req("POST", f"/licenses/{actor_lic}/withdraw",
                              {"purposes": ["web"], "source": "家属声明", "reason": "停止网络传播"})
        self.assertEqual(status, 200)
        _, check_web = self._req("GET", f"/assets/{asset_id}/permission?purpose=web")
        self.assertFalse(check_web["permitted"])
        _, prov2 = self._req("GET", f"/assets/{asset_id}/provenance")
        self.assertEqual(len(prov2["usages"]), 1)
        self.assertTrue(prov2["usages"][0]["permission_snapshot"]["permitted"])

    def test_unknown_route_and_missing_record(self) -> None:
        status, body = self._req("GET", "/nope")
        self.assertEqual(status, 404)
        status, body = self._req("GET", "/assets/asset-99999/provenance")
        self.assertEqual(status, 404)
        self.assertIn("未找到", body["error"])


if __name__ == "__main__":
    unittest.main()
