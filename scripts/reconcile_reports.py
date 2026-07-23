"""Audit report files against persisted Artifacts.

Usage: ``uv run python scripts/reconcile_reports.py [--remove-stale-temp]``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.common.config import get_settings  # noqa: E402
from packages.session.artifact_reconcile import reconcile_report_files  # noqa: E402
from packages.session.store import SessionStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="对账报告 Artifact 与落盘文件")
    parser.add_argument(
        "--remove-stale-temp",
        action="store_true",
        help="仅删除超过宽限期、且匹配原子写入命名规则的临时文件",
    )
    args = parser.parse_args()
    settings = get_settings()
    result = reconcile_report_files(
        SessionStore(settings.chat_db_path),
        settings.report_dir,
        stale_after_seconds=settings.report_temp_grace_seconds,
        remove_stale_temporary=args.remove_stale_temp,
    )
    print(json.dumps(result.log_fields(), ensure_ascii=False, indent=2))
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
