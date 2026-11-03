from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
import re
import sqlite3
import threading

from .keys import KeyStore
from .sqlcipher import SQLITE_MAGIC, decrypt_database
from ..base import BackendError, ChatInfo


# These are WeChat's service conversations, not user contacts.  They remain
# excluded from the private-chat picker and listener even when they appear in
# the contact database.
SYSTEM_CHAT_IDS = frozenset({"filehelper", "fmessage", "medianote", "newsapp", "notifymessage", "weixin"})


def discover_data_dir(configured: Path | None = None) -> Path:
    if configured:
        path = Path(configured).expanduser()
        if (path / "db_storage").is_dir():
            return path
        raise BackendError("DATA_DIRECTORY_MISSING", "WECHAT_LINUX_DATA_DIR 必须指向含 db_storage 的账号目录")
    candidates = sorted(path.parent for path in (Path.home() / "xwechat_files").glob("*/db_storage"))
    if len(candidates) != 1:
        raise BackendError("ACCOUNT_AMBIGUOUS" if candidates else "DATA_DIRECTORY_MISSING", "设置 WECHAT_LINUX_DATA_DIR=~/xwechat_files/<账号目录>，避免选错微信账号")
    return candidates[0]


def message_table(chat_id: str) -> str:
    return "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()


def _protobuf_fields(data: bytes, *, max_bytes: int = 4096) -> list[tuple[int, int, bytes | int]]:
    """Read a bounded wire message; never search arbitrary bytes for a hash."""
    if len(data) > max_bytes:
        raise ValueError("资源元数据过大")
    offset, fields = 0, []

    def varint() -> int:
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                break
            byte = data[offset]
            offset += 1
            if shift == 63 and byte > 1:
                break
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("资源 varint 无效")

    while offset < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not number or len(fields) >= 64:
            raise ValueError("资源字段无效")
        if wire == 0:
            value = varint()
        elif wire in {1, 2, 5}:
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - offset:
                raise ValueError("资源字段截断")
            value, offset = data[offset:offset + size], offset + size
        else:
            raise ValueError("不支持的资源 wire type")
        fields.append((number, wire, value))
    return fields


def resource_image_hashes(packed: bytes) -> list[str]:
    # Observed MessageResourceInfo: field 2 -> image field 1 -> ASCII hash.
    try:
        values = [value.decode("ascii").lower()
                  for number, wire, image in _protobuf_fields(bytes(packed))
                  if number == 2 and wire == 2
                  for field, inner_wire, value in _protobuf_fields(image)
                  if field == 1 and inner_wire == 2 and re.fullmatch(rb"[0-9a-fA-F]{32}", value)]
        return list(dict.fromkeys(values))
    except (TypeError, ValueError, UnicodeError):
        return []


def voice_text(row: dict) -> str:
    """Read only the client's stored transcript, never OCR or a guessed string."""
    from .parser import decode_content, xml_root
    values = []
    try:
        root = xml_root(decode_content(row.get("message_content")))
        node = root.find(".//voicetrans") if root is not None else None
        if node is not None and node.get("transtext", "").strip():
            values.append(node.get("transtext").strip())
        # PackedInfoData.info.audioTxt is wire field 5 -> field 2.
        packed = row.get("packed_info_data") or b""
        for number, wire, info in _protobuf_fields(bytes(packed), max_bytes=16384):
            if (number, wire) == (5, 2):
                for field, inner_wire, text in _protobuf_fields(info, max_bytes=16384):
                    if (field, inner_wire) == (2, 2):
                        value = text.decode("utf-8").strip()
                        if value:
                            values.append(value)
        return values[0] if values and len(set(values)) == 1 and len(values[0]) <= 4000 else ""
    except (TypeError, ValueError, UnicodeError):
        return ""


class WeChatLinuxDB:
    def __init__(self, settings, *, data_dir: Path | None = None, key_store: KeyStore | None = None):
        self.settings = settings
        self.data_dir = data_dir or discover_data_dir(settings.wechat_linux_data_dir)
        self.root = self.data_dir / "db_storage"
        self.account = self.data_dir.name.rsplit("_", 1)[0]
        self.keys = key_store or KeyStore(settings.wechat_linux_key_file, self.account)
        self.cache = settings.wechat_linux_cache_dir / hashlib.sha256(str(self.data_dir).encode()).hexdigest()[:16]
        self._signatures: dict[str, tuple] = {}
        self._lock = threading.RLock()

    @property
    def message_databases(self) -> list[Path]:
        return sorted(self.root.glob("message/message_[0-9]*.db"))

    def database_files(self) -> list[Path]:
        return sorted(self.root.rglob("*.db"))

    def snapshot(self, relative: str) -> Path:
        if ".." in Path(relative).parts or Path(relative).is_absolute():
            raise ValueError("数据库路径无效")
        source = self.root / relative
        if not source.is_file():
            raise BackendError("DATABASE_MISSING", f"缺少数据库 {relative}")
        wal = Path(str(source) + "-wal")
        with self._lock:
            signature = (source.stat().st_mtime_ns, source.stat().st_size, wal.stat().st_mtime_ns if wal.exists() else 0, wal.stat().st_size if wal.exists() else 0)
            destination = self.cache / relative
            if self._signatures.get(relative) != signature or not destination.exists():
                key = self.keys.require(relative, source)
                decrypt_database(source, destination, key)
                self._signatures[relative] = signature
            return destination

    def connect(self, relative: str) -> sqlite3.Connection:
        with self._lock:
            target = self.snapshot(relative)
            connection = sqlite3.connect(target.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            connection.row_factory = sqlite3.Row
            connection.text_factory = lambda raw: raw.decode("utf-8", "replace")
            return connection

    def names(self, shard: str | Path | None = None) -> dict[int, str]:
        result: dict[int, str] = {}
        paths = [path for path in self.message_databases if path.name == Path(shard).name] if shard else self.message_databases
        for path in paths:
            with self.connect(path.relative_to(self.root).as_posix()) as connection:
                try:
                    rows = connection.execute("SELECT rowid AS mapping_id, * FROM Name2Id")
                    for row in rows:
                        values = dict(row)
                        name = values.get("user_name") or values.get("username") or next((value for key, value in values.items() if key != "mapping_id" and isinstance(value, str)), "")
                        if name:
                            result[int(values["mapping_id"])] = str(name)
                except sqlite3.DatabaseError:
                    continue
        return result

    def generation(self, shard: Path) -> str:
        # SQLCipher salt is stable through WAL/checkpoints and changes on replacement.
        with shard.open("rb") as stream:
            salt = stream.read(16)
        identity = (shard.stat().st_dev, shard.stat().st_ino, salt.hex())
        return hashlib.sha256(repr(identity).encode()).hexdigest()

    def display_names(self) -> dict[str, str]:
        result = {}
        try:
            with self.connect("contact/contact.db") as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("contact", "Contact", "ContactTable"):
                    if table not in tables:
                        continue
                    for row in connection.execute(f'SELECT * FROM "{table}"'):
                        values = dict(row)
                        identifier = values.get("username") or values.get("user_name") or values.get("wxid")
                        label = values.get("remark") or values.get("remark_name") or values.get("nick_name") or values.get("nickname")
                        if identifier and label:
                            result[str(identifier)] = str(label)
        except (BackendError, sqlite3.DatabaseError):
            pass  # Optional contact key; stable IDs remain usable.
        return result

    def list_chats(self, *, include_private: bool = False) -> list[ChatInfo]:
        labels = self.display_names()
        identifiers = dict.fromkeys(value for shard in self.message_databases for value in self.names(shard).values())
        # Name2Id only contains conversations that have appeared in a message
        # shard.  Include the contact database for the private picker so an
        # operator can explicitly allow every current contact, including one
        # that has not sent a message yet.
        if include_private:
            identifiers.update(labels)
        result = []
        for value in identifiers:
            if not value or value == self.account:
                continue
            is_group = value.endswith("@chatroom") or value == "filehelper"
            if include_private and value in SYSTEM_CHAT_IDS:
                continue
            if is_group or include_private:
                result.append(ChatInfo(value, labels.get(value, value)))
        return result

    def resolve_chat(self, value: str) -> ChatInfo:
        mapping = json.loads(self.settings.wechat_linux_chat_ids)
        identifier = mapping.get(value, value)
        # Direct chats are resolved only when explicitly enabled by the
        # backend; keeping the DB resolver capable of finding them also lets a
        # stable wxid be used instead of a duplicate display name.
        matches = [chat for chat in self.list_chats(include_private=True)
                   if chat.chat_id == identifier or chat.name == value]
        if len(matches) != 1:
            raise BackendError("CHAT_NOT_FOUND" if not matches else "CHAT_AMBIGUOUS", f"无法唯一定位已配置会话 {value}；在 WECHAT_LINUX_CHAT_IDS 中设置稳定 ID")
        return ChatInfo(matches[0].chat_id, value)

    def private_chats(self, configured: list[str] | tuple[str, ...] = ()) -> list[ChatInfo]:
        """Return user conversations, optionally narrowed by names/wxids."""

        wanted = {str(value).strip().casefold() for value in configured if str(value).strip()}
        values = []
        for chat in self.list_chats(include_private=True):
            if chat.chat_id.endswith("@chatroom") or chat.chat_id == "filehelper":
                continue
            if wanted and chat.chat_id.casefold() not in wanted and chat.name.casefold() not in wanted:
                continue
            values.append(chat)
        return values

    def rows(self, chat_id: str, *, shard: Path | None = None, after_id: int | None = None, limit: int = 100) -> list[dict]:
        table = message_table(chat_id)
        result = []
        for path in [shard] if shard else self.message_databases:
            with self.connect(path.relative_to(self.root).as_posix()) as connection:
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                if after_id is None:
                    query = f'SELECT * FROM "{table}" ORDER BY local_id DESC LIMIT ?'
                    arguments = (max(0, min(limit, 5000)),)
                else:
                    query = f'SELECT * FROM "{table}" WHERE local_id > ? ORDER BY local_id ASC LIMIT ?'
                    arguments = (after_id, max(0, min(limit, 5000)))
                for row in connection.execute(query, arguments):
                    result.append({**dict(row), "_shard": path.name})
        return sorted(result, key=lambda row: (int(row.get("create_time") or 0), str(row["_shard"]), int(row.get("local_id") or 0)))

    def watermarks(self, chat_id: str) -> dict[str, int]:
        values = {}
        for database in self.message_databases:
            rows = self.rows(chat_id, shard=database, limit=1)
            values[database.name] = int(rows[-1].get("local_id") or 0) if rows else 0
        return values

    def voice_row(self, event) -> dict | None:
        """Resolve one voice by account, chat, shard, local/server ID and author."""
        metadata = event.media_metadata
        shard = str(metadata.get("shard") or "")
        server = str(metadata.get("server_id") or "")
        local = metadata.get("local_id")
        if (event.account_id != self.account or event.message_type != "voice" or not event.member_id
                or not re.fullmatch(r"message_[0-9]+\.db", shard)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", server)
                or not isinstance(local, int) or local <= 0):
            return None
        with self.connect("message/" + shard) as connection:
            row = connection.execute('SELECT * FROM "' + message_table(event.group_id) +
                                     '" WHERE local_id=? AND server_id=? AND (local_type & 4294967295)=34',
                                     (local, server)).fetchone()
        if row is None:
            return None
        result = dict(row, _shard=shard)
        if self.names(shard).get(int(result.get("real_sender_id") or 0)) != event.member_id:
            return None
        if event.source_created_at and int(event.source_created_at.timestamp()) != int(result.get("create_time") or 0):
            return None
        return result

    def resource_image_hashes(self, chat_id: str, *, local_id: int | None = None,
                              server_id: str | None = None,
                              created_at: datetime | int | None = None) -> list[str]:
        """Resolve a single image in this account/chat, including quoted svrid."""
        timestamp = int(created_at.timestamp()) if isinstance(created_at, datetime) else int(created_at or 0)
        server = str(server_id or "")
        has_server = bool(re.fullmatch(r"[1-9][0-9]{0,19}", server))
        if not ((local_id and timestamp > 0) or has_server):
            return []
        predicates = ["c.user_name=?", "(i.message_local_type & 4294967295)=3"]
        parameters: list[object] = [chat_id]
        if local_id and timestamp > 0:
            predicates.append("i.message_local_id=?")
            parameters.append(int(local_id))
        if timestamp > 0:
            predicates.append("i.message_create_time=?")
            parameters.append(timestamp)
        if has_server:
            predicates.append("i.message_svr_id=?")
            parameters.append(server)
        try:
            with self.connect("message/message_resource.db") as connection:
                rows = connection.execute(
                    "SELECT i.packed_info FROM MessageResourceInfo i "
                    "JOIN ChatName2Id c ON c.rowid=i.chat_id WHERE " +
                    " AND ".join(predicates) + " LIMIT 5", parameters).fetchall()
            # An ambiguous local id/time or server id must never select an image.
            hashes = [resource_image_hashes(row[0] or b"") for row in rows]
            return hashes[0] if hashes and len(rows) < 5 and all(value == hashes[0] for value in hashes) else []
        except (BackendError, sqlite3.DatabaseError):
            return []

    def favorite_stickers(self) -> list[str]:
        try:
            with self.connect("emoticon/emoticon.db") as connection:
                return [str(row[0]) for row in connection.execute("SELECT md5 FROM kFavEmoticonOrderTable ORDER BY rowid DESC") if re.fullmatch(r"[0-9a-fA-F]{32}", str(row[0]))]
        except (BackendError, sqlite3.DatabaseError):
            return []

    def voice_data(self, chat_id: str, server_id: str, *, max_bytes: int) -> bytes | None:
        """WeChat 4 VoiceInfo, scoped through this account's media Name2Id.

        Schema verified against wechatauto-replica 1.2.7.0's media reader.
        Read length before the BLOB and reject ambiguous matches across shards.
        """
        server = str(server_id or "")
        if not chat_id or not re.fullmatch(r"[1-9][0-9]{0,19}", server):
            return None
        result = None
        for path in sorted(self.root.glob("message/media_[0-9]*.db")):
            try:
                with self.connect(path.relative_to(self.root).as_posix()) as connection:
                    matches = connection.execute(
                        "SELECT v.rowid, length(v.voice_data) FROM VoiceInfo v "
                        "JOIN Name2Id n ON n.rowid=v.chat_name_id "
                        "WHERE n.user_name=? AND v.svr_id=? LIMIT 3", (chat_id, server),
                    ).fetchall()
                    if len(matches) >= 3:
                        return None
                    for row_id, size in matches:
                        if not size or not 0 < size <= max_bytes:
                            return None
                        blob = connection.execute("SELECT voice_data FROM VoiceInfo WHERE rowid=?", (row_id,)).fetchone()[0]
                        if not isinstance(blob, bytes) or not blob.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
                            return None
                        if result is not None and blob != result:
                            return None
                        result = blob
            except (BackendError, sqlite3.DatabaseError):
                continue
        return result
