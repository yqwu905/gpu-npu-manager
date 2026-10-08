"""导出 OpenAPI 文档到 docs/openapi.json：python scripts/export_openapi.py"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    app = create_app(Settings(database_url=f"sqlite:///{os.path.join(tmp, 'x.db')}", enable_poller=False))
    spec = app.openapi()

out = ROOT / "docs" / "openapi.json"
out.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"written {out}")
