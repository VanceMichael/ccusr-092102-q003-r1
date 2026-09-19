"""启动档案 HTTP 服务。

用法：
    python -m jin_opera_archive [host] [port] [db_path]

默认监听 0.0.0.0:8080，数据库文件为 ./jin_archive.db；
也可用环境变量 JIN_ARCHIVE_HOST / JIN_ARCHIVE_PORT / JIN_ARCHIVE_DB 覆盖。
"""

from __future__ import annotations

import os
import sys

from .server import create_server


def main(argv: list[str]) -> None:
    host = argv[1] if len(argv) > 1 else os.environ.get("JIN_ARCHIVE_HOST", "0.0.0.0")
    port = int(argv[2]) if len(argv) > 2 else int(os.environ.get("JIN_ARCHIVE_PORT", "8080"))
    db_path = argv[3] if len(argv) > 3 else os.environ.get("JIN_ARCHIVE_DB", "jin_archive.db")
    server = create_server(host, port, db_path)
    print(f"晋戏影像沿革档案服务已启动: http://{host}:{port} (db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.service.close()
        server.server_close()


if __name__ == "__main__":
    main(sys.argv)
