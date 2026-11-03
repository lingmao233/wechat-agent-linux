from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta, timezone
import json
import logging
import sys
from pathlib import Path
from dataclasses import asdict

from .agent import WeChatAgent
from .config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded Weixin 4.1.x AI test-group agent")
    parser.add_argument(
        "command",
        choices=["ready", "run", "init-db", "desktop", "rebuild-memory", "export-training-data", "keyscan", "read-messages", "diagnose", "memory-status", "replay-memory", "retry-memory"],
        nargs="?",
        default="ready",
    )
    parser.add_argument("--config", help="optional .env path", default=None)
    parser.add_argument("--window", type=int, help="messages used to rebuild group profiles")
    parser.add_argument(
        "--out",
        default=None,
        help="export directory; --today defaults to data/training/daily/<date>",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="export only messages from the local calendar day (for nightly jobs)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=6,
        help="prior messages per exported behavior example",
    )
    parser.add_argument(
        "--no-anonymize",
        action="store_true",
        help="keep real member/group names in the local training export",
    )
    parser.add_argument("--allow-send", action="store_true", help="enable sending; requires group allowlist")
    parser.add_argument("--stop", action="store_true", help="create the configured STOP file and exit")
    parser.add_argument("--host", help="desktop console bind host")
    parser.add_argument("--port", type=int, help="desktop console bind port")
    parser.add_argument("--open-browser", action="store_true", help="open the local desktop console")
    parser.add_argument("--account", default="", help="explicit account ID for history replay")
    parser.add_argument("--group", default="", help="stable group ID for history replay")
    parser.add_argument("--since", help="inclusive ISO timestamp with timezone")
    parser.add_argument("--until", help="exclusive ISO timestamp with timezone")
    parser.add_argument("--apply", action="store_true", help="apply explicitly scoped replay (default is dry-run; never backfills emotion)")
    parser.add_argument("--limit", type=int, default=30, help="maximum messages for read-only export")
    parser.add_argument("--images-only", action="store_true", help="keyscan: scan/cache the Linux image key without rescanning database keys")
    parser.add_argument("--local-only", action="store_true", help="keyscan --images-only: recover from local files/cache without process-memory access or sudo")
    return parser


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config:
        # pydantic-settings reads the selected file without mutating the
        # process-wide environment.
        settings = Settings(_env_file=Path(args.config))
    else:
        settings = Settings()
    if args.host:
        settings.desktop_host = args.host
    if args.port:
        settings.desktop_port = args.port
    if args.allow_send:
        settings.wechat_allow_send = True
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.stop:
        settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
        settings.stop_file.write_text("stop\n", encoding="utf-8")
        print(f"created {settings.stop_file}")
        return 0

    if args.command == "desktop":
        from .desktop import serve

        serve(
            settings,
            open_browser=args.open_browser,
            config_path=Path(args.config).expanduser() if args.config else Path(".env"),
            host=args.host,
            port=args.port,
        )
        return 0

    if args.command == "export-training-data":
        from .storage import Storage
        from .training_data import export_training_data

        storage = Storage(settings)
        storage.init_schema()
        since = until = None
        if args.today:
            now = datetime.now().astimezone()
            day = now.date()
            start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
            since = start.astimezone(timezone.utc)
            until = (start + timedelta(days=1)).astimezone(timezone.utc)
            out = args.out or f"data/training/daily/{day.isoformat()}"
        else:
            out = args.out or "data/training"
        report = export_training_data(
            storage,
            out,
            window_messages=args.context,
            anonymize=not args.no_anonymize,
            since=since,
            until=until,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "init-db":
        from .storage import Storage
        Storage(settings).init_schema()
        print("database schema initialized")
        return 0
    if args.command in {"rebuild-memory", "memory-status", "replay-memory", "retry-memory"}:
        from .storage import Storage
        from .memory_runtime import MemoryRuntime
        from .console_memory import MemoryConsole
        storage = Storage(settings)
        storage.init_schema()
        runtime = MemoryRuntime(storage)
        if args.command == "rebuild-memory":
            report = storage.rebuild_memory(window_messages=args.window)
        elif args.command == "memory-status":
            report = runtime.groups()
        elif args.command == "retry-memory":
            report = {"retried": runtime.retry_failed()}
        else:
            since, until = MemoryConsole.date(args.since), MemoryConsole.date(args.until)
            if not since or not until:
                raise ValueError("replay-memory 必须明确 --account --group --since --until；时间包含时区")
            report = runtime.replay(account_id=args.account, group_id=args.group, since=since, until=until, dry_run=not args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"keyscan", "read-messages", "diagnose", "ready"}:
        from .platform import create_backend
        backend = create_backend(settings)
        if args.command == "keyscan":
            if settings.effective_platform != "linux":
                raise ValueError("keyscan 仅用于 Linux；设置 WECHAT_PLATFORM=linux 并在 Linux 会话中运行")
            from .platform.linux.db import WeChatLinuxDB
            from .platform.linux.procmem import discover_pid
            db = WeChatLinuxDB(settings)
            if args.images_only:
                from .platform.linux.media import MediaService
                if args.local_only:
                    report = MediaService(settings, db).scan_image_key(None, process_memory=False)
                else:
                    report = MediaService(settings, db).scan_image_key(settings.wechat_linux_pid)
            else:
                if args.local_only:
                    raise ValueError("--local-only 必须和 keyscan --images-only 一起使用")
                pid = settings.wechat_linux_pid or discover_pid()
                result = db.keys.scan(pid, db.root, db.database_files(), max_mb=settings.wechat_linux_memory_scan_max_mb)
                failed = [relative for relative, valid in result.items() if not valid]
                report = {"ready": bool(result) and not failed, "account_id": db.account, "cache": str(settings.wechat_linux_key_file), "databases": len(result), "discovered": sum(bool(valid) for valid in result.values()), "failed_databases": failed}
        elif args.command == "read-messages":
            if settings.effective_platform != "linux":
                raise ValueError("read-messages 为 Linux 只读导出入口")
            backend.connect()
            events = backend.read_messages(limit=max(1, min(args.limit, 1000)))
            from collections import Counter
            report = {"account_id": backend.account_id, "counts": dict(Counter(event.message_type for event in events)), "messages": [asdict(event) for event in events]}
        else:
            report = backend.check_ready()
        encoded = json.dumps(report, ensure_ascii=False, indent=2, default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value))
        if args.out:
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(encoded, encoding="utf-8")
            target.chmod(0o600)
            print(f"report saved: {target}")
        else:
            print(encoded)
        return 0 if report.get("ready", True) else 2
    agent = WeChatAgent(settings)
    if args.command == "run":
        agent.run()
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    from .platform.base import BackendError
    from pydantic import ValidationError
    try:
        return _main(argv)
    except ValidationError as exc:
        print(json.dumps({"ready": False, "code": "CONFIG_INVALID", "errors": exc.errors(include_input=False, include_context=False)}, ensure_ascii=False, default=str))
        return 2
    except BackendError as exc:
        print(json.dumps({"ready": False, "code": exc.code, "hint": str(exc)}, ensure_ascii=False))
        return 2
    except ValueError as exc:
        print(json.dumps({"ready": False, "code": "INVALID_REQUEST", "hint": str(exc)}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
