"""测试公共支撑：把 src 加入导入路径，并提供建库与种子数据工具。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jin_opera_archive import ArchiveService  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def make_service() -> ArchiveService:
    return ArchiveService(":memory:")


def field_batch_payload() -> dict:
    return json.loads((FIXTURES / "field_batch.json").read_text(encoding="utf-8"))


def import_seed(service: ArchiveService, import_key: str = "field-1989-04") -> dict:
    return service.import_field_batch(
        field_batch_payload(), import_key=import_key, actor="馆员甲",
        at="2026-09-19T08:00:00+00:00",
    )
