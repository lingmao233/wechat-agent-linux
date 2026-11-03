from __future__ import annotations

from datetime import datetime
import hashlib
import math
import re
import threading

from sqlalchemy import select

from .memory_jobs import scope_transaction
from .memory_schema import StickerCatalogRow, StickerObservationRow
from .models import utc_now
from .platform.linux.media import VisionClient


class StickerCatalog:
    def __init__(self, settings, storage, db, ui, *, vision=None):
        self.settings, self.storage, self.db, self.ui = settings, storage, db, ui
        self.vision = vision or VisionClient(settings)
        self._lock = threading.Lock()

    def entries(self, account_id: str) -> list[dict]:
        with self.storage.Session() as session:
            return [{"tab": row.tab, "row": row.row, "col": row.col, "md5": row.md5, "description": row.description, "emotion": row.emotion, "updated_at": row.updated_at.isoformat()} for row in session.scalars(select(StickerCatalogRow).where(StickerCatalogRow.account_id == account_id, StickerCatalogRow.valid.is_(True)))]

    def invalidate(self, account_id: str) -> None:
        with scope_transaction(self.storage, account_id, "stickers") as session:
            for row in session.scalars(select(StickerCatalogRow).where(StickerCatalogRow.account_id == account_id)):
                row.valid = False

    def rebuild(self, account_id: str, chat) -> list[dict]:
        if not self._lock.acquire(blocking=False):
            return self.entries(account_id)
        try:
            favorites = self.db.favorite_stickers()
            if not favorites or not self.vision.configured:
                return []
            panel = self.ui.open_sticker_panel(chat)
            try:
                self.ui.select_favorites(panel)
                png = self.ui.capture_panel(panel)
                labels = self.vision.describe(png, panel=True)
            finally:
                self.ui.close_panel()
            if not isinstance(labels, list):
                return []
            entries = []
            for item in labels:
                if not isinstance(item, dict):
                    continue
                try:
                    row, col = int(item.get("row", -1)), int(item.get("col", -1))
                    confidence = float(item.get("confidence", 0))
                except (TypeError, ValueError):
                    continue
                index = row * self.settings.sticker_panel_columns + col
                if not (0 <= row < self.settings.sticker_panel_rows and 0 <= col < self.settings.sticker_panel_columns and 0 <= index < len(favorites)) or not math.isfinite(confidence) or not self.settings.sticker_min_confidence <= confidence <= 1:
                    continue
                description = str(item.get("description") or "").strip()[:200]
                if description:
                    entries.append({"tab": 1, "row": row, "col": col, "md5": favorites[index], "description": description, "emotion": str(item.get("emotion") or "")[:64]})
            if entries:
                now = utc_now()
                with scope_transaction(self.storage, account_id, "stickers") as session:
                    for old in session.scalars(select(StickerCatalogRow).where(StickerCatalogRow.account_id == account_id)):
                        old.valid = False
                    for item in entries:
                        existing = session.get(StickerCatalogRow, (account_id, item["tab"], item["row"], item["col"]))
                        if existing:
                            for key, value in item.items():
                                setattr(existing, key, value)
                            existing.valid, existing.updated_at = True, now
                        else:
                            session.add(StickerCatalogRow(account_id=account_id, **item, valid=True, screenshot_path="sha256:" + hashlib.sha256(png).hexdigest(), updated_at=now))
            return entries
        finally:
            self._lock.release()

    def choose(self, account_id: str, intent: str, *, chat=None) -> dict | None:
        entries = self.entries(account_id)
        if not entries and chat:
            entries = self.rebuild(account_id, chat)
        synonyms = {
            "庆祝": ("祝贺", "恭喜", "开心", "新年", "好耶", "撒花", "庆祝"),
            "无语": ("无奈", "无力", "无语", "叹气"),
            "安慰": ("抱抱", "安慰", "温暖", "摸头"),
            "赞同": ("点赞", "同意", "赞", "支持"),
        }
        expanded = intent
        for words in synonyms.values():
            if any(word in intent for word in words):
                expanded += " " + " ".join(words)
        def score(entry):
            haystack = entry["description"] + " " + entry["emotion"]
            terms = set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]{2}", expanded))
            return sum(term.casefold() in haystack.casefold() for term in terms) + int(bool(intent) and intent in haystack)
        ranked = sorted(entries, key=score, reverse=True)
        return ranked[0] if ranked and score(ranked[0]) > 0 else None
