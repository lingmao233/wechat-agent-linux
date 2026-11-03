"""Opt-in synthetic Linux delivery checks; ordinary pytest never sends messages."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from PIL import Image, ImageDraw

from wechat_agent.config import Settings
from wechat_agent.llm import LLMClient
from wechat_agent.models import OutboundAction
from wechat_agent.platform.linux.backend import LinuxBackend
from wechat_agent.platform.linux.keys import private_json_write


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--chat", required=True, help="exact alias already in WECHAT_GROUP_NAMES")
    parser.add_argument("--kind", choices=("text", "image", "sticker"), default="text")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--sticker-intent", default="祝贺开心庆祝")
    parser.add_argument("--from-model", action="store_true", help="one synthetic prompt -> configured LLM -> verified text send")
    parser.add_argument("--allow-send", action="store_true", help="enable this process's explicit test sends")
    parser.add_argument("--out", type=Path, default=Path("state/acceptance/delivery.json"))
    args = parser.parse_args()
    if not 1 <= args.count <= 100:
        parser.error("--count must be between 1 and 100")
    if args.from_model and (args.kind != "text" or args.count != 1):
        parser.error("--from-model requires one text message")
    if not args.allow_send:
        parser.error("live delivery requires --allow-send; use wechat-agent ready for read-only diagnostics")
    settings = Settings(_env_file=args.config)
    if settings.effective_platform != "linux" or args.chat not in settings.wechat_group_names:
        parser.error("requires Linux and an exact configured chat alias")
    settings.wechat_allow_send = True
    backend = LinuxBackend(settings)
    run = uuid.uuid4().hex
    report = {"at": datetime.now(timezone.utc).isoformat(), "run": run, "kind": args.kind,
              "requested": args.count, "input_origin": "synthetic_prompt" if args.from_model else "synthetic_action",
              "attempts": [], "passed": False}
    try:
        backend.connect()
        chat = backend.db.resolve_chat(args.chat)
        report["scope_fingerprint"] = hashlib.sha256((backend.account_id + ":" + chat.chat_id).encode()).hexdigest()[:16]
        for index in range(args.count):
            text = f"Linux acceptance {run[:8]} {args.kind} {index + 1}/{args.count}"
            if args.from_model:
                if not settings.llm_api_key:
                    raise ValueError("MODEL_NOT_CONFIGURED")
                generated = LLMClient(settings).complete("只准确复述用户提供的测试文本，不添加其他内容。", text, max_tokens=128).strip()
                if generated != text:
                    raise ValueError("MODEL_OUTPUT_MISMATCH")
                text = generated
            action = OutboundAction(backend.account_id, chat.chat_id, args.chat, text,
                                    idempotency_key=f"acceptance:{run}:{index}")
            if args.kind == "sticker":
                action.sticker_intent = args.sticker_intent
            elif args.kind == "image":
                picture = Image.new("RGB", (560, 220), "#edf6ee")
                ImageDraw.Draw(picture).text((24, 90), text, fill="#183b25")
                target = settings.wechat_media_dir / f"acceptance-{run}-{index}.png"
                picture.save(target)
                action.text, action.media_paths = "", [str(target)]
            started = time.monotonic()
            try:
                result = backend.send(action)
                watermarks = backend.db.watermarks(chat.chat_id)
                repeated = backend.send(action)
                idempotent = repeated.message_ids == result.message_ids and watermarks == backend.db.watermarks(chat.chat_id)
                item = {"index": index + 1, "verified": result.verified, "message_type": result.message_type,
                        "receipt_fingerprints": [hashlib.sha256(value.encode()).hexdigest() for value in result.message_ids],
                        "idempotent": idempotent, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
                item["passed"] = result.verified and idempotent and result.message_type == args.kind
            except Exception as exc:
                item = {"index": index + 1, "passed": False, "error": getattr(exc, "code", type(exc).__name__),
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
            report["attempts"].append(item)
            private_json_write(args.out, report)
            print(json.dumps(item), flush=True)
            if not item["passed"]:
                break  # A send with an unknown outcome is never replayed.
            if index + 1 < args.count:
                time.sleep(max(3.2, settings.wechat_reply_cooldown_seconds + .2))
        report["verified_count"] = sum(item["passed"] for item in report["attempts"])
        report["passed"] = report["verified_count"] == args.count
        private_json_write(args.out, report)
        return 0 if report["passed"] else 1
    except Exception as exc:
        report["error"] = getattr(exc, "code", type(exc).__name__)
        private_json_write(args.out, report)
        return 1
    finally:
        settings.wechat_allow_send = False
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
