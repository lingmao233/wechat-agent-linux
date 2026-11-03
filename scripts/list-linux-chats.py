#!/usr/bin/env python3
"""List WeChat Linux chats and stable chatroom IDs for WECHAT_LINUX_CHAT_IDS."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from wechat_agent.config import Settings
from wechat_agent.platform.base import BackendError
from wechat_agent.platform.linux.db import WeChatLinuxDB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=".env", help=".env 路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 数组")
    parser.add_argument(
        "--mapping",
        action="store_true",
        help="输出可直接粘贴到 WECHAT_LINUX_CHAT_IDS 的名称→ID JSON",
    )
    args = parser.parse_args(argv)

    settings = Settings(_env_file=Path(args.config))
    if settings.effective_platform != "linux":
        print("当前 .env 的 WECHAT_PLATFORM 不是 linux；请在 Linux 上运行", file=sys.stderr)
        return 2
    try:
        db = WeChatLinuxDB(settings)
        chats = sorted(
            db.list_chats(), key=lambda chat: (chat.chat_id != "filehelper", chat.name)
        )
    except BackendError as exc:
        print(
            json.dumps({"error": exc.code, "hint": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    if args.mapping:
        print(
            json.dumps(
                {chat.name: chat.chat_id for chat in chats},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.json:
        print(json.dumps([asdict(chat) for chat in chats], ensure_ascii=False, indent=2))
        return 0

    print(f"账号: {db.account}  数据目录: {db.data_dir}")
    print(f"{'chat_id':<30} name")
    for chat in chats:
        print(f"{chat.chat_id:<30} {chat.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
