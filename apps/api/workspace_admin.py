"""工作区离线备份、校验和恢复 CLI。"""

from __future__ import annotations

import argparse
import json

from packages.common.config import Settings, get_settings
from packages.session.workspace_backup import (
    backup_workspace,
    restore_workspace,
    verify_workspace_backup,
)


def _backup(settings: Settings, args: argparse.Namespace) -> dict[str, object]:
    return backup_workspace(
        db_path=settings.chat_db_path,
        dataset_dir=settings.dataset_dir,
        artifact_dir=settings.report_dir,
        backup_root=settings.workspace_backup_dir,
        output=args.output,
        service_stopped=bool(args.service_stopped),
    )


def _restore(settings: Settings, args: argparse.Namespace) -> dict[str, object]:
    return restore_workspace(
        input_dir=args.input,
        db_path=settings.chat_db_path,
        dataset_dir=settings.dataset_dir,
        artifact_dir=settings.report_dir,
        backup_root=settings.workspace_backup_dir,
        service_stopped=bool(args.service_stopped),
        confirmed=bool(args.yes),
        replace_files=bool(args.replace_files),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ChatBI SQLite/Dataset/Artifact 一致备份与恢复",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="创建离线一致备份")
    backup.add_argument("--output", help="WORKSPACE_BACKUP_DIR 内的目录名/路径")
    backup.add_argument("--service-stopped", action="store_true")
    verify = subparsers.add_parser("verify", help="只读校验备份")
    verify.add_argument("--input", required=True)
    restore = subparsers.add_parser("restore", help="恢复完整工作区")
    restore.add_argument("--input", required=True)
    restore.add_argument("--service-stopped", action="store_true")
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--replace-files", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = get_settings()
    if args.command == "backup":
        result = _backup(settings, args)
    elif args.command == "verify":
        result = verify_workspace_backup(args.input)
    else:
        result = _restore(settings, args)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
