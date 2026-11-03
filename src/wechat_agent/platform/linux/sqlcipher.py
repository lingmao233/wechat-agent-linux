from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import struct
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
RESERVE = 80
SQLITE_MAGIC = b"SQLite format 3\x00"


class CipherError(ValueError):
    pass


def mac_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, dklen=32)


def verify_page(key: bytes, page: bytes, page_number: int = 1, *, salt: bytes | None = None) -> bool:
    if len(key) != 32 or len(page) != PAGE_SIZE or page_number < 1:
        return False
    if salt is None:
        if page_number != 1:
            return False
        salt = page[:16]
    start = 16 if page_number == 1 else 0
    # SQLCipher authenticates ciphertext AND IV, followed by the LE page number.
    digest = hmac.new(mac_key(key, salt), page[start:PAGE_SIZE - 64] + struct.pack("<I", page_number), hashlib.sha512).digest()
    return hmac.compare_digest(digest, page[-64:])


def decrypt_page(key: bytes, page: bytes, page_number: int, salt: bytes) -> bytes:
    if not verify_page(key, page, page_number, salt=salt):
        raise CipherError(f"SQLCipher 页 {page_number} 认证失败；需要重新扫描该库密钥")
    start = 16 if page_number == 1 else 0
    end = PAGE_SIZE - RESERVE
    decryptor = Cipher(algorithms.AES(key), modes.CBC(page[end:end + 16])).decryptor()
    plain = decryptor.update(page[start:end]) + decryptor.finalize()
    return (SQLITE_MAGIC if page_number == 1 else b"") + plain + bytes(RESERVE)


def _checksum(data: bytes, state: tuple[int, int] = (0, 0), *, little: bool = True) -> tuple[int, int]:
    if len(data) % 8:
        raise CipherError("WAL 校验数据长度错误")
    values = struct.unpack(("<" if little else ">") + f"{len(data) // 4}I", data)
    s0, s1 = state
    for index in range(0, len(values), 2):
        s0 = (s0 + values[index] + s1) & 0xFFFFFFFF
        s1 = (s1 + values[index + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def committed_wal_frames(wal: bytes):
    """Yield only complete, checksummed transactions from the current WAL generation."""
    if len(wal) < 32:
        return
    magic, version, page_size = struct.unpack_from(">III", wal)
    if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != PAGE_SIZE:
        raise CipherError("不支持的 WAL 头或 page_size")
    little = magic == 0x377F0682
    state = _checksum(wal[:24], little=little)
    if state != struct.unpack_from(">II", wal, 24):
        raise CipherError("WAL 头校验失败")
    salt = wal[16:24]
    pending = []
    for offset in range(32, len(wal) - (24 + PAGE_SIZE) + 1, 24 + PAGE_SIZE):
        header = wal[offset:offset + 24]
        page = wal[offset + 24:offset + 24 + PAGE_SIZE]
        page_number, db_size = struct.unpack_from(">II", header)
        if page_number == 0 or header[8:16] != salt:
            break
        computed = _checksum(header[:8] + page, state, little=little)
        if computed != struct.unpack_from(">II", header, 16):
            break  # torn/uncommitted tail; prior commits remain valid
        state = computed
        pending.append((page_number, page))
        if db_size:
            yield pending, db_size
            pending = []


def decrypt_database(source: Path, destination: Path, key: bytes) -> None:
    """Publish a private, consistent SQLite snapshot; never modify WeChat's files."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    wal_path = Path(str(source) + "-wal")
    for attempt in range(3):
        before = (source.stat().st_mtime_ns, source.stat().st_size)
        wal_before = (wal_path.stat().st_mtime_ns, wal_path.stat().st_size) if wal_path.exists() else None
        descriptor, temporary = tempfile.mkstemp(prefix=".decrypt-", dir=destination.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w+b") as output, source.open("rb") as incoming:
                salt = incoming.read(16)
                incoming.seek(0)
                page_number = 0
                logical_pages = None
                while page := incoming.read(PAGE_SIZE):
                    page_number += 1
                    # WeChat preallocates databases in large zero-filled extents.
                    # These pages have never been encrypted and lie beyond the
                    # authenticated SQLite header's logical database size.
                    if logical_pages is not None and page_number > logical_pages and page == bytes(PAGE_SIZE):
                        output.write(page)
                        continue
                    plain = decrypt_page(key, page, page_number, salt)
                    if page_number == 1:
                        logical_pages = struct.unpack_from(">I", plain, 28)[0]
                    output.write(plain)
                if wal_before:
                    # A WAL is bounded by SQLite checkpointing; guard malformed input.
                    if wal_before[1] > 256 * 1024 * 1024:
                        raise CipherError("WAL 超过 256MB，请先等待微信 checkpoint")
                    for frames, db_size in committed_wal_frames(wal_path.read_bytes()):
                        for number, page in frames:
                            output.seek((number - 1) * PAGE_SIZE)
                            output.write(decrypt_page(key, page, number, salt))
                        output.truncate(db_size * PAGE_SIZE)
                output.flush()
                os.fsync(output.fileno())
            after = (source.stat().st_mtime_ns, source.stat().st_size)
            wal_after = (wal_path.stat().st_mtime_ns, wal_path.stat().st_size) if wal_path.exists() else None
            if before != after or wal_before != wal_after:
                if attempt < 2:
                    continue
                raise CipherError("微信数据库正在变化，未能取得一致快照；稍后重试")
            os.replace(temporary, destination)
            return
        finally:
            Path(temporary).unlink(missing_ok=True)
