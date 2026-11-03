from __future__ import annotations

from datetime import datetime, timezone
import html
import re
from typing import Any
import xml.etree.ElementTree as ET

from ...models import MessageEvent, QuotedMessage, utc_now

MAX_CONTENT_BYTES = 4 * 1024 * 1024


def decode_content(value: object) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if raw.startswith(b"\x28\xb5\x2f\xfd"):
            import zstandard
            raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=MAX_CONTENT_BYTES)
        if len(raw) > MAX_CONTENT_BYTES:
            raise ValueError("消息超过解析大小上限")
        return raw.decode("utf-8", errors="replace")
    return str(value or "")[:MAX_CONTENT_BYTES]


def xml_root(text: str):
    start = text.find("<")
    if start < 0 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def _text(node, tag: str) -> str:
    if node is None:
        return ""
    return html.unescape(node.findtext(tag, default="")).strip()


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def parse_message(row: dict[str, Any], *, account_id: str, chat_id: str, chat_name: str, names: dict[int, str], display_names: dict[str, str] | None = None, self_id: str = "", shard: str = "message_0.db") -> MessageEvent:
    raw = decode_content(row.get("message_content", row.get("content", "")))
    sender_id = names.get(int(row.get("real_sender_id") or 0), "")
    if not sender_id:
        sender_id = str(row.get("sender_id") or "")
    if ":\n" in raw:
        prefix, body = raw.split(":\n", 1)
        if prefix == sender_id or prefix.startswith("wxid_"):
            sender_id = sender_id or prefix
            raw = body
    local_type = int(row.get("local_type") or row.get("type") or 1)
    base_type, subtype = local_type & 0xFFFFFFFF, local_type >> 32
    source_time = _time(row.get("create_time"))
    event = MessageEvent(
        account_id=account_id, group_id=chat_id, group_name=chat_name,
        member_id=sender_id, member_name=(display_names or {}).get(sender_id, sender_id or "未知成员"),
        text=raw, source="weixin_linux_db", source_created_at=source_time,
        observed_at=source_time or utc_now(), time_quality="platform" if source_time else "observed",
        external_message_id=f"{chat_id}:{shard}:{row.get('local_id', '')}:{row.get('server_id', '')}",
        is_self=bool(sender_id and sender_id == self_id),
        source_role="assistant" if sender_id and sender_id == self_id else "human",
        media_metadata={"local_id": int(row.get("local_id") or 0), "server_id": str(row.get("server_id") or ""), "shard": shard, "local_type": local_type,
                        "conversation_type": "group" if chat_id.endswith("@chatroom") else "private"},
        conversation_type="group" if chat_id.endswith("@chatroom") else "private",
    )
    root = xml_root(raw)
    if subtype == 57 or (base_type == 49 and root is not None and root.find(".//refermsg") is not None):
        reference = root.find(".//refermsg") if root is not None else None
        content = _text(reference, "content")
        reference_type = _text(reference, "type")
        event.message_type = "quote"
        event.reply_to = QuotedMessage(
            text=content, sender_name=_text(reference, "displayname"),
            message_id=_text(reference, "svrid") or None,
            sender_id=_text(reference, "fromusr"), created_at=_time(_text(reference, "createtime")),
            message_type="image" if reference_type == "3" else "text",
            raw_xml=content if reference_type == "3" else "",
        )
        event.text = _text(root, ".//appmsg/title") or "[引用消息]"
    elif base_type == 47:
        emoji = root if root is not None and root.tag == "emoji" else root.find(".//emoji") if root is not None else None
        attributes = dict(emoji.attrib) if emoji is not None else {}
        md5 = attributes.get("md5", "").lower()
        event.message_type = "sticker"
        event.text = "[表情]"
        event.sticker_md5 = md5 if re.fullmatch(r"[0-9a-f]{32}", md5) else ""
        event.sticker_url = html.unescape(attributes.get("cdnurl", ""))
        try:
            event.sticker_size = max(0, int(attributes.get("len", 0)))
        except ValueError:
            event.sticker_size = 0
    elif base_type == 3:
        image = root if root is not None and root.tag == "img" else root.find(".//img") if root is not None else None
        event.message_type = "image"
        event.text = "[图片]"
        event.media_metadata["image"] = dict(image.attrib) if image is not None else {}
    elif base_type == 34:
        voice = root if root is not None and root.tag == "voicemsg" else root.find(".//voicemsg") if root is not None else None
        event.message_type = "voice"
        event.text = "[语音]"
        event.media_metadata["voice"] = dict(voice.attrib) if voice is not None else {}
        event.media_metadata["raw_xml"] = raw
        duration = (voice.get("voicelength", "") if voice is not None else
                    (raw.rsplit(":", 1)[-1] if re.fullmatch(r"[0-9]{1,4}:[0-9]{1,6}", raw) else ""))
        if duration.isdigit():
            event.media_metadata["voice_duration_seconds"] = int(duration) / 1000
    elif base_type == 49:
        event.message_type = "appmsg"
        event.text = _text(root, ".//appmsg/title") or raw
        event.media_metadata["raw_xml"] = raw
    elif base_type != 1:
        event.message_type = "other"
    return event
