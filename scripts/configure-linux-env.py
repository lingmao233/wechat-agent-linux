#!/usr/bin/env python3
"""Point an existing .env at this machine's Linux WeChat account directory.

Only rewrites the platform, data-directory, account-ID and (optionally) the
send switch. Comments, secrets and every unrelated line are preserved. A
timestamped backup is written next to the original file.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<raw>.*)$"
)
PLAIN_VALUE = re.compile(r"[A-Za-z0-9_./:@%+,\-\[\]{}]+")


def unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    return raw


def quote(value: str) -> str:
    if value == "":
        return ""
    if PLAIN_VALUE.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_key(text: str, key: str, value: str) -> str:
    pattern = re.compile(
        rf"^(?P<lead>\s*(?:export\s+)?){re.escape(key)}\s*=.*$", re.MULTILINE
    )
    replacement = f"{key}={quote(value)}"
    if pattern.search(text):
        return pattern.sub(lambda match: match.group("lead") + replacement, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + replacement + "\n"


def assignments(text: str):
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            yield match.group("key"), unquote(match.group("raw"))


def discover_data_dir() -> Path:
    # WeChat Linux 4.x puts the account tree under ~/xwechat_files on some
    # builds and under ~/Documents/xwechat_files on others; accept both.
    roots = [Path.home() / "xwechat_files", Path.home() / "Documents" / "xwechat_files"]
    candidates = sorted(
        path.parent for root in roots for path in root.glob("*/db_storage") if path.is_dir()
    )
    if not candidates:
        raise SystemExit(
            "未找到 ~/xwechat_files 或 ~/Documents/xwechat_files 下的 */db_storage；"
            "请先安装并登录微信 Linux 版，再重试（也可用 --data-dir 指定）"
        )
    if len(candidates) > 1:
        joined = "\n  ".join(str(path) for path in candidates)
        raise SystemExit(f"发现多个微信账号目录，请用 --data-dir 明确指定：\n  {joined}")
    return candidates[0]


def account_from_dir(data_dir: Path) -> str:
    # WeChat stores an account directory as wxid_xxx_<install-suffix>.
    name = data_dir.name
    return name.rsplit("_", 1)[0] if "_" in name else name


def validate_data_dir(path: Path) -> None:
    if not (path / "db_storage").is_dir():
        raise SystemExit(f"{path} 不包含 db_storage；请指向 ~/xwechat_files/<账号目录>")
    if not (path / "db_storage" / "message").is_dir():
        print(
            f"警告: {path}/db_storage/message 不存在；微信可能尚未完成首次登录",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="要修改的 .env 路径")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data-dir", help="~/xwechat_files/<账号目录>")
    source.add_argument(
        "--discover-data-dir", action="store_true", help="自动发现唯一的微信账号目录"
    )
    parser.add_argument(
        "--disable-send", action="store_true", help="同时把 WECHAT_ALLOW_SEND 设为 false"
    )
    parser.add_argument(
        "--migrate-home",
        metavar="OLD_HOME",
        help="旧 Linux 主目录（例如 /home/wx）；把配置中的该前缀改为当前 $HOME",
    )
    parser.add_argument(
        "--force-account",
        action="store_true",
        help="数据目录推导出的账号与现有 WECHAT_ACCOUNT_ID 不同时仍覆盖",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    args = parser.parse_args(argv)

    env_path = Path(args.env).expanduser()
    if not env_path.is_file():
        raise SystemExit(
            f"{env_path} 不存在；先运行 bash scripts/setup-linux.sh，或用 --env-from 导入"
        )
    text = env_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    current = dict(assignments(text))

    data_dir = (
        Path(args.data_dir).expanduser() if args.data_dir else discover_data_dir()
    )
    data_dir = data_dir.resolve()
    validate_data_dir(data_dir)
    account = account_from_dir(data_dir)
    if not account:
        raise SystemExit("无法从数据目录名推导 WECHAT_ACCOUNT_ID")

    previous = current.get("WECHAT_ACCOUNT_ID", "")
    if previous and previous != account and not args.force_account:
        raise SystemExit(
            f"现有 WECHAT_ACCOUNT_ID={previous} 与数据目录账号 {account} 不一致；"
            "确认目录无误后加 --force-account"
        )

    planned = {
        "WECHAT_PLATFORM": "linux",
        "WECHAT_LINUX_DATA_DIR": str(data_dir),
        "WECHAT_ACCOUNT_ID": account,
    }
    if args.disable_send:
        planned["WECHAT_ALLOW_SEND"] = "false"

    migrated: list[str] = []
    if args.migrate_home:
        old = args.migrate_home.rstrip("/") + "/"
        new = str(Path.home()).rstrip("/") + "/"
        if old != new:
            for key, value in current.items():
                if old in value:
                    text = set_key(text, key, value.replace(old, new))
                    migrated.append(key)

    for key, value in planned.items():
        text = set_key(text, key, value)

    home = str(Path.home())
    leftovers = sorted(
        key
        for key, value in assignments(text)
        if home not in value and re.search(r"/home/[A-Za-z0-9_.-]+/", value)
    )

    print(f"配置文件: {env_path}")
    for key, value in planned.items():
        print(f"  {key}={value}")
    if migrated:
        print("已迁移主目录路径: " + ", ".join(migrated))
    if leftovers:
        print(
            "警告: 以下键仍含其他用户主目录，当前用户不是该用户时需手工修改: "
            + ", ".join(leftovers),
            file=sys.stderr,
        )

    if args.dry_run:
        print("--dry-run: 未写入")
        return 0

    backup = env_path.with_name(
        f"{env_path.name}.bak.{datetime.now():%Y%m%d-%H%M%S}"
    )
    shutil.copy2(env_path, backup)
    os.chmod(backup, 0o600)

    descriptor, temporary = tempfile.mkstemp(prefix=".env-", dir=env_path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, env_path)
        os.chmod(env_path, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)

    print(f"已写入 {env_path}；备份 {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
