"""晋戏影像沿革档案的 HTTP 接口（仅依赖标准库）。

所有端点读写 JSON（UTF-8）。错误约定：
400 参数不合法 / 404 记录不存在 / 409 许可不足或状态冲突。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote

from .service import (
    ArchiveError,
    ArchiveService,
    NotAuthorizedError,
    NotFoundError,
    ValidationError,
)

Handler = Callable[[dict[str, Any], dict[str, str]], Any]


def make_router(service: ArchiveService) -> list[tuple[str, re.Pattern, Handler]]:
    """把 HTTP 方法与路径模式映射到服务调用。"""

    routes: list[tuple[str, re.Pattern, Handler]] = []
    def add(method: str, pattern: str, handler: Handler) -> None:
        routes.append((method, re.compile(f"^{pattern}$"), handler))

    add("GET", r"/health", lambda b, q: {"状态": "档案服务已启动"})
    add("POST", r"/imports", lambda b, q: service.import_field_batch(
        b, import_key=b.get("import_key", ""), actor=b.get("actor", "unknown")))
    add("GET", r"/imports/(?P<key>[^/]+)", lambda b, q: service.get_import(q["key"]))

    add("POST", r"/assets/(?P<asset>[^/]+)/captions", lambda b, q: service.correct_caption(
        q["asset"], b.get("text", ""), b.get("basis", ""), b.get("actor", "unknown"),
        source_ids=b.get("source_ids", []), at=b.get("at")))
    add("GET", r"/assets/(?P<asset>[^/]+)/captions", lambda b, q: service.caption_history(q["asset"]))
    add("GET", r"/assets/(?P<asset>[^/]+)/history", lambda b, q: service.asset_history(q["asset"]))
    add("GET", r"/assets/(?P<asset>[^/]+)/authorization", lambda b, q: service.authorize(
        q["asset"], q.get("purpose", "research"), at=q.get("at")))
    add("POST", r"/assets/(?P<asset>[^/]+)/usages", lambda b, q: service.record_usage(
        q["asset"], b.get("purpose", ""), b.get("venue", ""), b.get("actor", "unknown"),
        at=b.get("at"), note=b.get("note", "")))
    add("GET", r"/assets/(?P<asset>[^/]+)/usages", lambda b, q: service.list_usages(q["asset"]))
    add("POST", r"/assets/(?P<asset>[^/]+)/genres", lambda b, q: _ok(service.assign_genre(
        q["asset"], b.get("genre_id", ""), b.get("scheme_id", ""), b.get("actor", "unknown"),
        source_id=b.get("source_id"), supersede=bool(b.get("supersede")), at=b.get("at"))))
    add("POST", r"/assets/(?P<asset>[^/]+)/publisher", lambda b, q: _ok(service.set_publisher(
        q["asset"], b.get("troupe_id", ""), b.get("actor", "unknown"),
        source_id=b.get("source_id"), at=b.get("at"))))

    add("POST", r"/persons", lambda b, q: service.create_person(
        b.get("display_name", ""), b.get("actor", "unknown"),
        aliases=b.get("aliases", []), source_ids=b.get("source_ids", []), at=b.get("at")))
    add("GET", r"/persons/(?P<pid>[^/]+)", lambda b, q: service.get_person(q["pid"]))
    add("POST", r"/troupes", lambda b, q: service.create_troupe(
        b.get("display_name", ""), b.get("actor", "unknown"), kind=b.get("kind", "troupe"),
        aliases=b.get("aliases", []), source_ids=b.get("source_ids", []), at=b.get("at")))
    add("GET", r"/troupes/(?P<tid>[^/]+)", lambda b, q: service.get_troupe(q["tid"]))
    add("POST", r"/troupes/(?P<tid>[^/]+)/rename", lambda b, q: service.rename_troupe(
        q["tid"], b.get("new_name", ""), b.get("valid_from", ""), b.get("actor", "unknown"),
        old_name_valid_to=b.get("old_name_valid_to"), source_id=b.get("source_id"),
        at=b.get("at")))
    add("POST", r"/places", lambda b, q: service.create_place(
        b.get("name", ""), b.get("actor", "unknown"), valid_from=b.get("valid_from"),
        valid_to=b.get("valid_to"), source_ids=b.get("source_ids", []), at=b.get("at")))
    add("GET", r"/places/(?P<pid>[^/]+)/history", lambda b, q: service.place_history(q["pid"]))
    add("POST", r"/places/(?P<pid>[^/]+)/names", lambda b, q: service.add_place_name(
        q["pid"], b.get("name", ""), b.get("actor", "unknown"),
        valid_from=b.get("valid_from"), valid_to=b.get("valid_to"),
        source_id=b.get("source_id"), at=b.get("at")))
    add("POST", r"/entities/(?P<etype>[^/]+)/(?P<eid>[^/]+)/aliases", lambda b, q: _ok(
        service.add_alias(q["etype"], q["eid"], b.get("alias", ""), b.get("actor", "unknown"),
                          valid_from=b.get("valid_from"), valid_to=b.get("valid_to"),
                          source_id=b.get("source_id"), at=b.get("at"))))

    add("POST", r"/genres", lambda b, q: service.create_genre(
        b.get("name", ""), b.get("actor", "unknown"), aliases=b.get("aliases", []),
        at=b.get("at")))
    add("POST", r"/genre-schemes", lambda b, q: service.create_scheme(
        b.get("scheme_id", ""), b.get("title", ""), b.get("actor", "unknown"),
        issued_at=b.get("issued_at", ""), entries=b.get("entries", []),
        note=b.get("note", ""), at=b.get("at")))

    add("GET", r"/candidates", lambda b, q: service.list_candidates(
        status=q.get("status", "pending"), entity_type=q.get("entity_type")))
    add("POST", r"/candidates", lambda b, q: service.propose_candidate(
        b.get("entity_type", ""), b.get("left", {}), b.get("right", {}),
        b.get("reason", ""), b.get("actor", "unknown"), at=b.get("at")))
    add("POST", r"/candidates/(?P<cid>[^/]+)/resolve", lambda b, q: service.resolve_candidate(
        q["cid"], b.get("decision", ""), b.get("actor", "unknown"),
        rationale=b.get("rationale", ""), survivor_id=b.get("survivor_id"), at=b.get("at")))
    add("POST", r"/mentions/(?P<mid>[^/]+)/link", lambda b, q: service.link_mention(
        q["mid"], b.get("entity_id", ""), b.get("actor", "unknown"), at=b.get("at")))

    add("POST", r"/licenses", lambda b, q: service.grant_license(
        b.get("subject_type", ""), b.get("subject_id", ""), b.get("purpose", ""),
        b.get("actor", "unknown"), asset_id=b.get("asset_id"),
        granted_at=b.get("granted_at"), note=b.get("note", ""), at=b.get("at")))
    add("POST", r"/licenses/(?P<lid>[^/]+)/withdraw", lambda b, q: service.withdraw_license(
        q["lid"], b.get("actor", "unknown"), at=b.get("at"), note=b.get("note", "")))

    add("POST", r"/sources", lambda b, q: service.register_source(
        b.get("kind", ""), b.get("citation", ""), b.get("actor", "unknown"),
        collector=b.get("collector", ""), collected_at=b.get("collected_at"), at=b.get("at")))

    add("GET", r"/search", lambda b, q: service.search_by_genre(
        q.get("genre", ""), purpose=q.get("purpose", "research"),
        viewer=q.get("viewer", "researcher"), scheme_id=q.get("scheme_id"), at=q.get("at")))
    add("GET", r"/lineage/(?P<etype>[^/]+)/(?P<eid>[^/]+)", lambda b, q: service.lineage(
        q["etype"], q["eid"]))

    return routes


def _ok(_result: Any) -> dict[str, bool]:
    return {"ok": True}


class ArchiveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: ArchiveService) -> None:
        self.service = service
        self.routes = make_router(service)
        super().__init__(address, ArchiveRequestHandler)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: ArchiveHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # 静默访问日志
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path, _, query = self.path.partition("?")
        path = unquote(path)
        params = dict(parse_qsl(query, keep_blank_values=True))
        body: dict[str, Any] = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._respond(400, {"error": "请求体不是合法 JSON"})
                return
        for route_method, pattern, handler in self.server.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            merged = {**params, **match.groupdict()}
            try:
                result = handler(body, merged)
            except NotAuthorizedError as exc:
                self._respond(409, {"error": "许可不足", "missing": exc.missing})
            except NotFoundError as exc:
                self._respond(404, {"error": str(exc)})
            except ValidationError as exc:
                self._respond(400, {"error": str(exc)})
            except ArchiveError as exc:
                self._respond(400, {"error": str(exc)})
            else:
                self._respond(200, result)
            return
        self._respond(404, {"error": f"未知路径: {method} {path}"})

    def _respond(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str, port: int, db_path: str) -> ArchiveHTTPServer:
    return ArchiveHTTPServer((host, port), ArchiveService(db_path))
