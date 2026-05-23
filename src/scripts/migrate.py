"""
Run Alembic migrations. Idempotent — chạy ở entrypoint container.

Postgres advisory lock đảm bảo chỉ 1 worker chạy migration cùng lúc,
các replica khác chờ.
"""

from __future__ import annotations

import sys

from alembic import command
from alembic.config import Config


def main() -> int:
    cfg = Config("alembic.ini")
    try:
        command.upgrade(cfg, "head")
        print("[migrate] upgraded to head")
        return 0
    except Exception as e:
        print(f"[migrate] error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
