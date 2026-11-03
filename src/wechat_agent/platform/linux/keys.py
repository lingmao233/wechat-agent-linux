from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .procmem import key_candidates
from .sqlcipher import PAGE_SIZE, verify_page
from ..base import BackendError


def private_json_write(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        Path(name).unlink(missing_ok=True)


class KeyStore:
    def __init__(self, path: Path, account: str):
        self.path, self.account = Path(path), account
        self.payload: dict = {"account": account, "keys": {}}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("account") == account:
                self.payload = data
                if os.name == "posix":
                    os.chmod(self.path, 0o600)

    def database_key(self, relative: str, database: Path) -> bytes | None:
        try:
            raw = bytes.fromhex(self.payload.get("keys", {}).get(relative) or "")
            with database.open("rb") as stream:
                return raw if verify_page(raw, stream.read(PAGE_SIZE)) else None
        except (ValueError, OSError):
            return None

    def scan(self, pid: int, root: Path, databases: list[Path], *, max_mb: int = 4096) -> dict[str, bool]:
        unresolved: dict[str, bytes] = {}
        for database in databases:
            relative = database.relative_to(root).as_posix()
            if self.database_key(relative, database) is None:
                with database.open("rb") as stream:
                    unresolved[relative] = stream.read(PAGE_SIZE)
        for key in key_candidates(pid, max_mb=max_mb) if unresolved else ():
            for relative, page in list(unresolved.items()):
                if verify_page(key, page):
                    self.payload.setdefault("keys", {})[relative] = key.hex()
                    unresolved.pop(relative)
            if not unresolved:
                break
        from ...models import utc_now
        self.payload["generated_at"] = utc_now().isoformat()
        private_json_write(self.path, self.payload)
        return {database.relative_to(root).as_posix(): self.database_key(database.relative_to(root).as_posix(), database) is not None for database in databases}

    def require(self, relative: str, database: Path) -> bytes:
        key = self.database_key(relative, database)
        if key is None:
            raise BackendError("KEY_REQUIRED", f"{relative} 缺少有效密钥；运行 wechat-agent keyscan 后重试")
        return key
