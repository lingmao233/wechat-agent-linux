from __future__ import annotations

from pathlib import Path
import re

import numpy as np

from ..base import BackendError

CONFIG_XOR_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
KEY_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


def discover_pid() -> int:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
            if Path(command).name.lower() in {"wechat", "weixin"}:
                found.append(int(entry.name))
        except (OSError, ValueError):
            continue
    if len(found) != 1:
        raise BackendError("PROCESS_NOT_FOUND" if not found else "PROCESS_AMBIGUOUS", "请先登录 Linux 专用微信；有多个进程时设置 WECHAT_LINUX_PID")
    return found[0]


def memory_chunks(pid: int, *, max_mb: int = 4096, chunk_size: int = 2 * 1024 * 1024):
    try:
        maps = Path(f"/proc/{pid}/maps").read_text()
        memory = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except PermissionError as exc:
        raise BackendError("PTRACE_DENIED", f"无法读取 /proc/{pid}/mem；请由微信同用户运行 keyscan，或以 sudo 单独执行密钥扫描。不要以 root 运行控制台。") from exc
    remaining = max_mb * 1024 * 1024
    with memory:
        for line in maps.splitlines():
            fields = line.split()
            if len(fields) < 2 or not fields[1].startswith("rw"):
                continue
            start, end = (int(value, 16) for value in fields[0].split("-"))
            if end - start > 256 * 1024 * 1024:
                continue
            position = start
            while position < end and remaining > 0:
                size = min(chunk_size, end - position, remaining)
                try:
                    memory.seek(position)
                    data = memory.read(size)
                except OSError:
                    position += size
                    remaining -= size
                    continue
                if data:
                    yield position, data
                position += max(1, size - 256) if position + size < end else size
                remaining -= size


def process_bytes(pid: int, address: int, size: int = 16) -> bytes:
    """Read a bounded key candidate pointed to by an in-process string object."""
    if not 0 < size <= 64 or not 4096 <= address < (1 << 63):
        return b""
    try:
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            memory.seek(address)
            return memory.read(size)
    except PermissionError as exc:
        raise BackendError("PTRACE_DENIED", "图片密钥扫描需要单次 sudo 权限；不要以 root 运行控制台") from exc
    except OSError:
        return b""


def key_candidates(pid: int, *, max_mb: int = 4096):
    seen: set[bytes] = set()
    mask = np.frombuffer(CONFIG_XOR_MASK, dtype=np.uint8)
    for _, data in memory_chunks(pid, max_mb=max_mb):
        variants = [data]
        array = np.frombuffer(data, dtype=np.uint8)
        for phase in range(32):
            shifted = np.roll(mask, -phase)
            variants.append(np.bitwise_xor(array, np.resize(shifted, array.size)).tobytes())
            # Consume each phase immediately to keep peak scanner memory bounded.
            candidate_data = variants.pop()
            for match in KEY_LITERAL.finditer(candidate_data):
                raw = match.group(1)
                for offset in range(0, len(raw) - 63, 32):
                    key = bytes.fromhex(raw[offset:offset + 64].decode())
                    if key not in seen:
                        seen.add(key)
                        yield key
        for match in KEY_LITERAL.finditer(data):
            key = bytes.fromhex(match.group(1)[:64].decode())
            if key not in seen:
                seen.add(key)
                yield key
