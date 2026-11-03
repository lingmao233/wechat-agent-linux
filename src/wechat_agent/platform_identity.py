"""Three-way account linking: one WeChat member ↔ stable platform user IDs.

The stable ID (Douyin/Xiaohongshu account ID) is the only authorization key
for an @ mention.  Display names are useful for the console and for the
platform's native suggestion list, but they are never unique and therefore
must not authorize a write by themselves.

This module only wraps :class:`wechat_agent.storage.Storage` CRUD and derives
the effective @ allowlist (configured IDs ∪ bound IDs).  The browser adapter
still has to select the platform's native mention candidate before a real
write; the service-level check below is the boundary before that step.
"""

from __future__ import annotations

from typing import Iterable

from .config import Settings
from .platforms.models import PlatformName
from .storage import Storage

PLATFORM_LABELS: dict[str, str] = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
}
SUPPORTED_PLATFORMS: tuple[str, ...] = ("douyin", "xiaohongshu")


def normalize_platform(value: object) -> str:
    platform = str(value or "").strip().casefold()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"unsupported_platform:{platform or 'empty'}")
    return platform


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(str(platform or "").strip(), str(platform or ""))


class PlatformIdentityService:
    """Bind/list/unbind platform identities and answer allowlist questions."""

    def __init__(self, storage: Storage, settings: Settings):
        self.storage = storage
        self.settings = settings

    @property
    def default_account_id(self) -> str:
        """Stable local account scope used by the console API and automation."""

        return str(getattr(self.settings, "wechat_account_id", "") or "").strip() or "default"

    def bind(
        self,
        *,
        platform: object,
        platform_user_id: object,
        member_id: object,
        platform_user_name: object = "",
        member_name: object = "",
        account_id: str | None = None,
    ) -> dict[str, object]:
        return self.storage.bind_platform_identity(
            account_id=account_id or self.default_account_id,
            member_id=str(member_id or "").strip(),
            member_name=str(member_name or "").strip(),
            platform=normalize_platform(platform),
            platform_user_id=str(platform_user_id or "").strip(),
            platform_user_name=str(platform_user_name or "").strip(),
        )

    def list(self, account_id: str | None = None) -> list[dict[str, object]]:
        return self.storage.list_platform_identities(account_id or self.default_account_id)

    def delete(self, identity_id: object) -> bool:
        return self.storage.delete_platform_identity(identity_id)  # type: ignore[arg-type]

    def member_for_platform_user(
        self,
        platform: object,
        platform_user_id: object,
        *,
        account_id: str | None = None,
    ) -> dict[str, object] | None:
        return self.storage.member_for_platform_user(
            account_id or self.default_account_id,
            normalize_platform(platform),
            str(platform_user_id or "").strip(),
        )

    def identities_for_member(
        self,
        member_id: object,
        *,
        account_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Bindings for a member; falls back to the console account scope.

        The agent event may carry a discovered WeChat account ID while the
        console bound identities before that ID was known.  In a single-account
        deployment both scopes are the same person, so a completed console
        binding must remain usable for the running agent.
        """

        member = str(member_id or "").strip()
        if not member:
            return []
        account = str(account_id or "").strip() or self.default_account_id
        identities = self.storage.identities_for_member(account, member)
        if not identities and account != self.default_account_id:
            identities = self.storage.identities_for_member(self.default_account_id, member)
        return identities

    def mention_ids_for_platform(self, platform: PlatformName) -> list[str]:
        """Stable IDs used to extend the platform @ allowlist.

        Passed to :meth:`PlatformService.set_mention_binding_provider`; only
        active bindings are returned and duplicates keep insertion order.
        """

        seen: set[str] = set()
        result: list[str] = []
        for user_id in self.storage.platform_identity_user_ids(str(platform)):
            value = str(user_id or "").strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def mention_names_for_ids(self, platform: str, ids: Iterable[str]) -> list[str]:
        """Display names aligned with ``ids`` for native mention candidates.

        The browser adapter may only select a native suggestion whose stable
        ``data-id`` matches; the display name is a secondary hint.  Missing
        names come back as empty strings so the ID list stays aligned.
        """

        values = [str(user_id or "").strip() for user_id in ids]
        names = self.storage.platform_mention_names(str(platform))
        return [names.get(user_id, "") for user_id in values]

    def mention_allowlist(self) -> dict[str, list[str]]:
        """Effective allowlist per platform for the console GET response."""

        allowlists: dict[str, list[str]] = {}
        for platform in SUPPORTED_PLATFORMS:
            configured = getattr(self.settings, f"{platform}_mention_allowlist", ()) or ()
            merged: list[str] = []
            seen: set[str] = set()
            for value in [*configured, *self.mention_ids_for_platform(platform)]:  # type: ignore[list-item]
                item = str(value or "").strip()
                if item and item not in seen:
                    seen.add(item)
                    merged.append(item)
            allowlists[platform] = merged
        return allowlists

    def identity_list_payload(self, account_id: str | None = None) -> dict[str, object]:
        """Shape shared with the console page and the DSH plugin proxy."""

        account = account_id or self.default_account_id
        identities = self.list(account)
        return {
            "account_id": account,
            "identities": identities,
            "mention_allowlist": self.mention_allowlist(),
        }

    @staticmethod
    def member_names(identities: Iterable[dict[str, object]]) -> dict[str, str]:
        """Map ``member_id`` → display name for memory rows."""

        names: dict[str, str] = {}
        for identity in identities:
            member_id = str(identity.get("member_id") or "").strip()
            if member_id and member_id not in names:
                names[member_id] = str(identity.get("member_name") or "").strip()
        return names
