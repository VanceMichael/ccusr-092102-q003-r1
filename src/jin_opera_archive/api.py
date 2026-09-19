"""档案服务 HTTP 接口（仅依赖标准库）。

路由一览
--------
GET    /health
POST   /batches
POST   /entities/{persons,organizations,places,genres,plays}
POST   /entities/{kind}/{id}/aliases
POST   /places/{id}/rename
POST   /organizations/{id}/rename
POST   /assets/import
POST   /assets/{id}/captions
POST   /assets/{id}/genre-classifications
GET    /assets/{id}/provenance
GET    /candidates?status=pending
POST   /candidates
POST   /candidates/{id}/resolution
POST   /assets/{id}/licenses
POST   /licenses/{id}/withdraw
GET    /assets/{id}/permission?purpose=research
POST   /assets/{id}/usages
GET    /search?genre=晋剧&purpose=research
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import SERVICE_NAME
from .models import CANDIDATE_PENDING
from .service import ArchiveService
from .store import ArchiveError, Conflict, NotFound, Store, ValidationError


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(f"请求体不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    return payload


class ArchiveHandler(BaseHTTPRequestHandler):
    service: ArchiveService  # 由 server 工厂注入（类属性）

    server_version = "JinOperaArchive/0.1"

    # ------------------------------------------------------------------ 工具

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items()}

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静的测试日志
        if os.environ.get("ARCHIVE_HTTP_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._send(200, {"状态": "档案服务已启动"})
                return
            if path == "/candidates":
                status = self._query().get("status", CANDIDATE_PENDING)
                self._send(200, {"candidates": self.service.list_candidates(status=status)})
                return
            if path.startswith("/assets/") and path.endswith("/permission"):
                asset_id = path.split("/")[2]
                q = self._query()
                self._send(
                    200,
                    self.service.check_permission(
                        asset_id,
                        q.get("purpose", "research"),
                        publisher_id=q.get("publisher_id"),
                    ),
                )
                return
            if path.startswith("/assets/") and path.endswith("/provenance"):
                asset_id = path.split("/")[2]
                self._send(200, self.service.provenance("asset", asset_id))
                return
            if path.startswith("/provenance/"):
                parts = path.split("/")
                self._send(200, self.service.provenance(parts[2], parts[3]))
                return
            if path == "/search":
                q = self._query()
                genre = q.get("genre")
                if not genre:
                    raise ValidationError("检索必须提供 genre（剧种名或 id）")
                self._send(200, self.service.search_by_genre(genre, purpose=q.get("purpose", "research")))
                return
            self._send(404, {"error": "未找到接口", "path": path})
        except ArchiveError as exc:
            self._send_error(exc)

    # ------------------------------------------------------------------ POST

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            payload = _read_json(self)
            svc = self.service

            if path == "/batches":
                batch_id = svc.register_batch(
                    payload["code"],
                    photographer_id=payload.get("photographer_id"),
                    received_at=payload.get("received_at"),
                    note=payload.get("note", ""),
                    source=payload["source"],
                )
                self._send(200, {"batch_id": batch_id})
                return

            if path.startswith("/entities/"):
                self._handle_entities(path, payload)
                return

            if path == "/assets/import":
                self._send(200, svc.import_asset(payload))
                return

            if path.startswith("/assets/") and path.endswith("/captions"):
                asset_id = path.split("/")[2]
                version = svc.revise_caption(
                    asset_id,
                    payload["text"],
                    source=payload["source"],
                    basis=payload["basis"],
                )
                self._send(200, {"asset_id": asset_id, "caption_version": version})
                return

            if path.startswith("/assets/") and path.endswith("/genre-classifications"):
                asset_id = path.split("/")[2]
                version = svc.classify_genre(
                    asset_id, payload["genre_id"],
                    source=payload["source"], basis=payload["basis"],
                )
                self._send(200, {"asset_id": asset_id, "genre_version": version})
                return

            if path == "/candidates":
                cid = svc.suggest_identity(
                    payload["kind"], payload["id_a"], payload["id_b"],
                    reason=payload.get("reason", ""),
                )
                self._send(200, {"candidate_id": cid})
                return

            if path.startswith("/candidates/") and path.endswith("/resolution"):
                candidate_id = path.split("/")[2]
                cand = svc.resolve_candidate(
                    candidate_id,
                    decision=payload["decision"],
                    keep_id=payload.get("keep_id"),
                    reviewer=payload["reviewer"],
                )
                self._send(200, cand)
                return

            if path.startswith("/assets/") and path.endswith("/licenses"):
                asset_id = path.split("/")[2]
                lic_id = svc.grant_license(
                    asset_id,
                    holder_kind=payload["holder_kind"],
                    holder_id=payload["holder_id"],
                    purposes=payload["purposes"],
                    scope_note=payload.get("scope_note", ""),
                    source=payload["source"],
                    granted_at=payload.get("granted_at"),
                )
                self._send(200, {"license_id": lic_id})
                return

            if path.startswith("/licenses/") and path.endswith("/withdraw"):
                license_id = path.split("/")[2]
                svc.withdraw_license(
                    license_id,
                    purposes=payload.get("purposes"),
                    source=payload["source"],
                    reason=payload["reason"],
                )
                self._send(200, {"license_id": license_id, "status": "withdrawn"})
                return

            if path.startswith("/assets/") and path.endswith("/usages"):
                asset_id = path.split("/")[2]
                record = svc.register_usage(
                    asset_id,
                    purpose=payload["purpose"],
                    publisher_id=payload.get("publisher_id"),
                    venue=payload.get("venue", ""),
                    published_at=payload.get("published_at"),
                    source=payload["source"],
                    force=bool(payload.get("force", False)),
                )
                self._send(200, record)
                return

            self._send(404, {"error": "未找到接口", "path": path})
        except ArchiveError as exc:
            self._send_error(exc)

    def _handle_entities(self, path: str, payload: dict[str, Any]) -> None:
        svc = self.service
        parts = [p for p in path.split("/") if p]
        # /entities/{collection}[/{id}/aliases|...]
        collection = parts[1]
        source = payload["source"]

        if len(parts) == 2:
            if collection == "persons":
                rid = svc.register_person(
                    payload["name"], aliases=payload.get("aliases"), source=source,
                    note=payload.get("note", ""), person_id=payload.get("id"),
                )
            elif collection == "organizations":
                rid = svc.register_organization(
                    payload["name"], org_type=payload.get("org_type", "troupe"),
                    aliases=payload.get("aliases"), source=source,
                    note=payload.get("note", ""), org_id=payload.get("id"),
                )
            elif collection == "places":
                rid = svc.register_place(
                    payload["name"], aliases=payload.get("aliases"),
                    source=source, place_id=payload.get("id"),
                )
            elif collection == "genres":
                rid = svc.register_genre(
                    payload["name"], aliases=payload.get("aliases"),
                    source=source, genre_id=payload.get("id"),
                )
            elif collection == "plays":
                rid = svc.register_play(
                    payload["name"], aliases=payload.get("aliases"),
                    source=source, play_id=payload.get("id"),
                )
            else:
                self._send(404, {"error": f"未知实体集合: {collection}"})
                return
            self._send(200, {"id": rid})
            return

        kind = {
            "persons": "person",
            "organizations": "organization",
            "places": "place",
            "genres": "genre",
            "plays": "play",
        }[collection]
        record_id = parts[2]
        action = parts[3]
        if action == "aliases":
            svc.add_alias(kind, record_id, payload["alias"], source=payload["source"])
            self._send(200, {"id": record_id, "alias_added": payload["alias"]})
        elif action == "rename":
            if kind == "place":
                svc.rename_place(record_id, payload["name"], since=payload["since"], source=payload["source"])
            elif kind == "organization":
                svc.rename_organization(record_id, payload["name"], since=payload["since"], source=payload["source"])
            else:
                raise ValidationError("只有场所与院团支持更名沿革接口")
            self._send(200, {"id": record_id, "name": payload["name"]})
        else:
            self._send(404, {"error": f"未知实体操作: {action}"})

    def _send_error(self, exc: ArchiveError) -> None:
        if isinstance(exc, NotFound):
            status = 404
        elif isinstance(exc, Conflict):
            status = 409
        elif isinstance(exc, ValidationError):
            status = 400
        else:
            status = 500
        self._send(status, {"error": str(exc), "type": type(exc).__name__})


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """构造带独立存储的服务实例（测试用端口 0 自动分配）。"""
    store = Store(db_path)
    service = ArchiveService(store)

    class _BoundHandler(ArchiveHandler):
        pass

    _BoundHandler.service = service
    return ThreadingHTTPServer((host, port), _BoundHandler)


def main() -> None:
    db_path = os.environ.get("ARCHIVE_DB", "archive_data.json")
    host = os.environ.get("ARCHIVE_HOST", "0.0.0.0")
    port = int(os.environ.get("ARCHIVE_PORT", "8080"))
    server = build_server(db_path, host=host, port=port)
    print(f"{SERVICE_NAME} 监听 {host}:{port}，数据文件 {db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
