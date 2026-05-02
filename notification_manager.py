"""Discord 原生通知推送和去重管理。"""

from __future__ import annotations

import re
import time
from typing import Any

import discord

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import BaseMessageComponent
from astrbot.core.platform.sources.discord.components import DiscordEmbed

_PATCHED = False
_NATIVE_DISCORD_EMBED_USABLE: bool | None = None


def _native_discord_embed_component_usable() -> bool:
    """Return whether AstrBot's bundled DiscordEmbed can be constructed safely."""
    global _NATIVE_DISCORD_EMBED_USABLE
    if _NATIVE_DISCORD_EMBED_USABLE is not None:
        return _NATIVE_DISCORD_EMBED_USABLE
    try:
        DiscordEmbed(description="compat check")
        _NATIVE_DISCORD_EMBED_USABLE = True
    except Exception as exc:
        _NATIVE_DISCORD_EMBED_USABLE = False
        logger.warning(
            "HAPI Discord Connector: AstrBot DiscordEmbed 不可直接构造，启用插件 Embed 兼容组件: %s",
            exc,
        )
    return _NATIVE_DISCORD_EMBED_USABLE


class _DhapiDiscordEmbed(BaseMessageComponent):
    """Plugin-local Discord embed component compatible with AstrBot MessageChain.

    AstrBot's bundled DiscordEmbed currently defines only `type` as a Pydantic
    field, so assigning title/description may fail.  This local component keeps
    the same duck-typed `type == "discord_embed"` contract and is converted to a
    native `discord.Embed` by the plugin-level `_parse_to_discord` patch below.
    """

    type: str = "discord_embed"
    title: str | None = None
    description: str | None = None
    color: int | None = None
    url: str | None = None
    thumbnail: str | None = None
    image: str | None = None
    footer: str | None = None
    fields: list[dict] | None = None

    def to_discord_embed(self) -> discord.Embed:
        return _component_to_discord_embed(self)


def _component_to_discord_embed(comp) -> discord.Embed:
    """Convert any DHAPI/AstrBot duck-typed embed component to discord.Embed."""
    embed = discord.Embed()
    if getattr(comp, "title", None):
        embed.title = getattr(comp, "title")
    if getattr(comp, "description", None):
        embed.description = getattr(comp, "description")
    if getattr(comp, "color", None):
        embed.color = getattr(comp, "color")
    if getattr(comp, "url", None):
        embed.url = getattr(comp, "url")
    if getattr(comp, "thumbnail", None):
        embed.set_thumbnail(url=getattr(comp, "thumbnail"))
    if getattr(comp, "image", None):
        embed.set_image(url=getattr(comp, "image"))
    if getattr(comp, "footer", None):
        embed.set_footer(text=getattr(comp, "footer"))

    for field in getattr(comp, "fields", None) or []:
        embed.add_field(
            name=field.get("name", ""),
            value=field.get("value", ""),
            inline=field.get("inline", False),
        )
    return embed


def _is_nonempty_discord_embed_component(comp) -> bool:
    """Duck-type DiscordEmbed validity for AstrBot respond stage."""
    return getattr(comp, "type", None) == "discord_embed" and bool(
        getattr(comp, "title", None)
        or getattr(comp, "description", None)
        or getattr(comp, "fields", None)
        or getattr(comp, "image", None)
        or getattr(comp, "thumbnail", None)
    )


def restore_legacy_dhapi_diagnostics() -> None:
    """Undo old hot-patched Discord send/adapter diagnostics from earlier builds."""
    try:
        from astrbot.core.platform.sources.discord.discord_platform_event import (
            DiscordPlatformEvent,
        )

        original_send = getattr(DiscordPlatformEvent, "_dhapi_original_send", None)
        if original_send is not None:
            DiscordPlatformEvent.send = original_send
            logger.info(
                "HAPI Discord Connector: 已恢复 DiscordPlatformEvent.send 旧诊断补丁"
            )
    except Exception as exc:
        logger.warning("恢复 Discord send 旧补丁失败: %s", exc)

    try:
        from astrbot.core.platform.sources.discord.discord_platform_adapter import (
            DiscordPlatformAdapter,
        )

        original_handle_msg = getattr(
            DiscordPlatformAdapter, "_dhapi_original_handle_msg", None
        )
        if original_handle_msg is not None:
            DiscordPlatformAdapter.handle_msg = original_handle_msg
            logger.info(
                "HAPI Discord Connector: 已恢复 DiscordPlatformAdapter.handle_msg 旧诊断补丁"
            )
    except Exception as exc:
        logger.warning("恢复 Discord handle_msg 旧补丁失败: %s", exc)


def apply_discord_embed_compat_patches() -> None:
    """Make AstrBot's generic respond stage and Discord adapter keep plugin-created embeds.

    Some AstrBot stages only know built-in ComponentType validators, and Discord's
    bundled DiscordEmbed may not be constructible in this runtime.  Patch by
    duck-typing `type == "discord_embed"` and converting directly to native
    discord.Embed before send.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    _native_discord_embed_component_usable()

    try:
        from astrbot.core.pipeline.respond.stage import RespondStage

        original_empty_check = RespondStage._is_empty_message_chain

        async def patched_empty_check(self, chain):
            for comp in chain or []:
                if _is_nonempty_discord_embed_component(comp):
                    return False
            return await original_empty_check(self, chain)

        if (
            getattr(
                RespondStage._is_empty_message_chain, "_dhapi_embed_patch_version", 0
            )
            < 2
        ):
            patched_empty_check._dhapi_embed_patch = True
            patched_empty_check._dhapi_embed_patch_version = 2
            RespondStage._is_empty_message_chain = patched_empty_check
    except Exception as exc:
        logger.warning(
            "HAPI Discord Connector: respond stage Embed 兼容补丁失败: %s", exc
        )

    try:
        from astrbot.core.platform.sources.discord.discord_platform_event import (
            DiscordPlatformEvent,
        )

        original_parse = DiscordPlatformEvent._parse_to_discord

        async def patched_parse_to_discord(self, message):
            dhapi_embeds = []
            original_chain = message.chain
            try:
                filtered_chain = []
                for comp in original_chain or []:
                    if getattr(comp, "type", None) == "discord_embed":
                        dhapi_embeds.append(_component_to_discord_embed(comp))
                    else:
                        filtered_chain.append(comp)

                # Let AstrBot parse all built-in components, then append the
                # plugin-local/native embeds we already converted.  This avoids
                # constructing the broken bundled DiscordEmbed class.
                message.chain = filtered_chain
                (
                    content,
                    files,
                    view,
                    embeds,
                    reference_message_id,
                ) = await original_parse(self, message)
                embeds.extend(dhapi_embeds)
                return content, files, view, embeds, reference_message_id
            finally:
                message.chain = original_chain

        if (
            getattr(
                DiscordPlatformEvent._parse_to_discord, "_dhapi_embed_patch_version", 0
            )
            < 2
        ):
            patched_parse_to_discord._dhapi_embed_patch = True
            patched_parse_to_discord._dhapi_embed_patch_version = 2
            DiscordPlatformEvent._parse_to_discord = patched_parse_to_discord
    except Exception as exc:
        logger.warning(
            "HAPI Discord Connector: Discord adapter Embed 兼容补丁失败: %s", exc
        )


restore_legacy_dhapi_diagnostics()
apply_discord_embed_compat_patches()


class NotificationManager:
    """处理 SSE 事件通知的 Discord Embed 推送和去重。"""

    MAX_LEN = 1900

    def __init__(self, context, state_mgr):
        self.context = context
        self.state_mgr = state_mgr
        self._recent_notifications: dict[tuple[str, str, str], float] = {}

    @staticmethod
    def notification_body_key(text: str) -> str:
        lines = text.splitlines()
        if (
            len(lines) >= 3
            and lines[0].startswith("💬 ")
            and lines[1].startswith("📂 ")
            and lines[2].startswith("🤖 ")
        ):
            lines = lines[3:]
        elif lines and lines[0].startswith("🏷️ "):
            lines = lines[1:]
        return "\n".join(line.rstrip() for line in lines).strip() or text.strip()

    @staticmethod
    def is_request_notification(text: str) -> bool:
        return "待审批" in text

    def should_skip_duplicate(self, umo: str, session_id: str, text: str) -> bool:
        if self.is_request_notification(text):
            return False

        now = time.monotonic()
        dedupe_window = 2.5
        expire_before = now - 30
        for key, ts in list(self._recent_notifications.items()):
            if ts < expire_before:
                self._recent_notifications.pop(key, None)

        body_key = self.notification_body_key(text)
        cache_key = (umo, session_id or "", body_key)
        last_sent = self._recent_notifications.get(cache_key)
        if last_sent is not None and now - last_sent <= dedupe_window:
            logger.info(
                "跳过重复通知: sid=%s umo=%s", (session_id or "global")[:8], umo[:20]
            )
            return True

        self._recent_notifications[cache_key] = now
        return False

    @staticmethod
    def split_message(text: str, max_len: int = MAX_LEN) -> list[str]:
        """按行边界将 Discord 文本分片。"""
        chunks = []
        current = ""
        for line in (text or "").split("\n"):
            if len(line) > max_len:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(
                    line[i : i + max_len] for i in range(0, len(line), max_len)
                )
                continue
            if current and len(current) + 1 + len(line) > max_len:
                chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            chunks.append(current)
        return chunks or [""]

    def _embed_enabled(self) -> bool:
        try:
            return bool(self.state_mgr.kv.config.get("embed_enabled", True))
        except Exception:
            return True

    def _brand_color(self) -> int:
        try:
            return int(self.state_mgr.kv.config.get("embed_brand_color", 0x5865F2))
        except Exception:
            return 0x5865F2

    @staticmethod
    def _limit_embed_text(value: Any, limit: int) -> str:
        text = "" if value is None else str(value)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 12)] + "\n…(truncated)"

    @classmethod
    def _sanitize_embed_fields(cls, fields: list[dict] | None) -> list[dict]:
        safe_fields = []
        for field in (fields or [])[:25]:
            safe_fields.append(
                {
                    "name": cls._limit_embed_text(field.get("name") or "\u200b", 256),
                    "value": cls._limit_embed_text(
                        field.get("value") or "\u200b", 1024
                    ),
                    "inline": bool(field.get("inline", False)),
                }
            )
        return safe_fields

    @staticmethod
    def _session_meta(
        session_id: str, sessions_cache: list[dict]
    ) -> tuple[str, str, str]:
        session = next((s for s in sessions_cache if s.get("id") == session_id), None)
        if not session:
            return (session_id[:8] if session_id else "global", "unknown", "")
        meta = session.get("metadata", {}) or {}
        flavor = meta.get("flavor", "unknown")
        summary = (meta.get("summary") or {}).get("text", "") or "session"
        return session_id[:8], flavor, summary

    @staticmethod
    def _extract_tool(text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("🔐 权限请求") or stripped.startswith("❓ 问题请求"):
                continue
            if stripped and not stripped.startswith(("当前", "审批", "/dhapi", "💡")):
                m = re.match(r"([^:：\s]+)", stripped)
                if m:
                    return m.group(1)
        return "request"

    @staticmethod
    def _event_style(text: str, brand_color: int) -> tuple[str, int]:
        lower = text.lower()
        if "待审批" in text or "权限请求" in text or "问题请求" in text:
            return "approval", 0xE74C3C
        if "任务完成" in text or "completed" in lower or "完成" in text:
            return "done", 0x2ECC71
        if (
            "等待输入" in text
            or "question" in lower
            or "answer" in lower
            or "问题请求" in text
        ):
            return "waiting", 0xF1C40F
        return "message", brand_color or 0x3498DB

    def _title_for(
        self, kind: str, text: str, short_sid: str, flavor: str, summary: str
    ) -> str:
        if kind == "approval":
            return f"待审批 - {self._extract_tool(text)}"
        if kind == "done":
            label = summary or short_sid
            return f"任务完成 - {label}"
        if kind == "waiting":
            return f"任务等待输入 - {summary or short_sid}"
        return f"HAPI 消息 - {summary or short_sid}"

    @staticmethod
    def _approval_fields(text: str) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        index = None
        m = re.search(r"审批序号\s*(\d+)", text)
        if m:
            index = m.group(1)
        detail_lines = []
        command_lines = []
        in_commands = False
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("审批指令"):
                in_commands = True
                continue
            if in_commands or stripped.startswith("/dhapi"):
                if stripped.startswith("/dhapi"):
                    command_lines.append(f"`{stripped}`")
                continue
            if not stripped.startswith(("当前总共", "🔐", "❓")):
                detail_lines.append(stripped)
        if detail_lines:
            fields.append(
                {
                    "name": "参数 / 详情",
                    "value": "\n".join(detail_lines)[:1024],
                    "inline": False,
                }
            )
        if index:
            fields.append({"name": "序号", "value": f"`{index}`", "inline": True})
        fields.append(
            {
                "name": "审批方式",
                "value": "打开 `/dhapi` → `审批` 面板处理",
                "inline": False,
            }
        )
        return fields

    def make_embed(
        self,
        title: str,
        description: str = "",
        color: int | None = None,
        fields: list[dict] | None = None,
        footer: str | None = None,
    ):
        safe_title = self._limit_embed_text(title, 256)
        safe_description = self._limit_embed_text(description, 3900)
        safe_footer = self._limit_embed_text(footer, 2048) if footer else None
        safe_fields = self._sanitize_embed_fields(fields)

        # Discord rejects embeds over 6000 aggregate characters.  Keep a small
        # safety margin and drop/trim fields rather than letting slash-command
        # followups fail after defer.
        used = len(safe_title) + len(safe_description) + len(safe_footer or "")
        budget = 5800 - used
        bounded_fields = []
        for field in safe_fields:
            field_len = len(field["name"]) + len(field["value"])
            if budget <= 0:
                break
            if field_len > budget:
                field = {
                    **field,
                    "value": self._limit_embed_text(
                        field["value"], max(1, budget - len(field["name"]))
                    ),
                }
                field_len = len(field["name"]) + len(field["value"])
            if field_len > budget:
                break
            bounded_fields.append(field)
            budget -= field_len

        embed_cls = (
            DiscordEmbed
            if _native_discord_embed_component_usable()
            else _DhapiDiscordEmbed
        )
        return embed_cls(
            title=safe_title,
            description=safe_description,
            color=self._brand_color() if color is None else color,
            fields=bounded_fields,
            footer=safe_footer,
        )

    async def push_embed(
        self,
        umo: str,
        title: str,
        description: str,
        color: int,
        fields: list[dict] | None = None,
        footer: str | None = None,
    ):
        """向 Discord 目标窗口推送一个或多个 Embed。"""
        chunks = self.split_message(description, self.MAX_LEN)
        for idx, chunk in enumerate(chunks, 1):
            page_title = title if len(chunks) == 1 else f"{title} ({idx}/{len(chunks)})"
            embed = self.make_embed(
                page_title, chunk, color, fields if idx == 1 else [], footer
            )
            try:
                logger.info(
                    "[dhapi] push_embed sending umo=%s title=%s desc_len=%s fields=%s page=%s/%s",
                    umo,
                    page_title[:80],
                    len(chunk),
                    len(fields or []) if idx == 1 else 0,
                    idx,
                    len(chunks),
                )
                await self.context.send_message(umo, MessageChain([embed]))
                logger.info(
                    "[dhapi] push_embed sent umo=%s title=%s", umo, page_title[:80]
                )
            except Exception as exc:
                logger.warning("Discord Embed 推送失败 (umo=%s): %s", umo[:20], exc)
                raise

    async def send_embed(self, umo: str, embed_payload: dict):
        """预留 Embed payload 扩展点。"""
        embed = self.make_embed(
            embed_payload.get("title", "HAPI"),
            embed_payload.get("description", ""),
            embed_payload.get("color"),
            embed_payload.get("fields"),
            embed_payload.get("footer"),
        )
        await self.context.send_message(umo, MessageChain([embed]))

    async def push_notification(
        self, text: str, session_id: str, sessions_cache: list[dict]
    ):
        """推送通知到 Discord 目标窗口，优先使用 Embed，失败仅记录警告。"""
        targets = self.state_mgr.select_notification_targets(session_id, sessions_cache)
        logger.info(
            "[dhapi] push_notification sid=%s targets=%s text_len=%s",
            session_id[:8] if session_id else "global",
            targets,
            len(text or ""),
        )
        if not targets:
            if session_id:
                short_sid, flavor, _ = self._session_meta(session_id, sessions_cache)
                logger.warning(
                    "Session %s [%s] 无绑定窗口且无默认窗口，推送失败",
                    short_sid,
                    flavor,
                )
            else:
                logger.warning("全局通知无可用默认窗口，推送失败")
            return

        short_sid, flavor, summary = self._session_meta(session_id, sessions_cache)
        footer = f"session {short_sid} | {flavor} | HAPI Discord Connector"
        kind, color = self._event_style(text, self._brand_color())
        title = self._title_for(kind, text, short_sid, flavor, summary)
        fields = self._approval_fields(text) if kind == "approval" else []

        for umo in targets:
            if self.should_skip_duplicate(umo, session_id, text):
                continue
            if self._embed_enabled():
                try:
                    await self.push_embed(umo, title, text, color, fields, footer)
                    continue
                except Exception:
                    logger.warning("Embed 推送失败，尝试纯文本降级 (umo=%s)", umo[:20])
            for chunk in self.split_message(text, self.MAX_LEN):
                try:
                    logger.info(
                        "[dhapi] push_text sending umo=%s chunk_len=%s", umo, len(chunk)
                    )
                    await self.context.send_message(umo, MessageChain().message(chunk))
                    logger.info("[dhapi] push_text sent umo=%s", umo)
                except Exception as exc:
                    logger.warning(
                        "推送到 Discord 窗口失败 (umo=%s): %s", umo[:20], exc
                    )
                    break
