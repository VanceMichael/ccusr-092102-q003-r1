"""HTTP 接口冒烟测试：健康检查、幂等导入、检索与溯源。"""

import http.client
import json
import threading
import unittest
import urllib.parse

import support
from jin_opera_archive.server import ArchiveHTTPServer
from jin_opera_archive import ArchiveService


class ServerSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ArchiveHTTPServer(("127.0.0.1", 0), ArchiveService(":memory:"))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.service.close()
        cls.thread.join(timeout=5)

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        headers = {"Content-Type": "application/json; charset=utf-8"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_health(self) -> None:
        status, data = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"状态": "档案服务已启动"})

    def test_import_replay_and_search_flow(self) -> None:
        self._request("POST", "/genres", {"name": "晋剧", "actor": "编目员"})
        payload = support.field_batch_payload()
        payload["import_key"] = "http-import-1"
        payload["actor"] = "馆员甲"
        status, first = self._request("POST", "/imports", payload)
        self.assertEqual(status, 200)
        self.assertFalse(first["replayed"])
        status, replay = self._request("POST", "/imports", payload)
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["items"], replay["items"])

        asset_id = first["items"][0]["asset_id"]
        query = urllib.parse.urlencode({"genre": "晋剧", "viewer": "researcher"})
        status, results = self._request("GET", f"/search?{query}")
        self.assertEqual(status, 200)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["restricted"])
        self.assertNotIn("caption", results[0])

        status, history = self._request("GET", f"/assets/{asset_id}/history")
        self.assertEqual(status, 200)
        self.assertEqual(history["captions"][0]["version"], 1)

        # 许可不足时登记使用 → 409
        status, denied = self._request("POST", f"/assets/{asset_id}/usages", {
            "purpose": "exhibition", "venue": "测试展", "actor": "策展人",
            "at": "2020-06-01",
        })
        self.assertEqual(status, 409)
        self.assertIn("missing", denied)

    def test_unknown_route_and_record(self) -> None:
        status, _ = self._request("GET", "/nope")
        self.assertEqual(status, 404)
        path = urllib.parse.quote("/assets/asset-不存在/history")
        status, data = self._request("GET", path)
        self.assertEqual(status, 404)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
