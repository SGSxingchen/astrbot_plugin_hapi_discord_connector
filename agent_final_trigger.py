"""Synthetic AstrBot event trigger for HAPI agent final messages.

This module intentionally does not touch Discord adapter behavior.  It only
constructs a controlled in-process AstrMessageEvent and enqueues it into
AstrBot's normal event queue after the SSE listener has observed a HAPI-side
assistant final message.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.platform.astr_message_event import MessageSession
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.sources.discord.discord_platform_adapter import (
    DiscordPlatformAdapter,
)
from astrbot.core.platform.sources.discord.discord_platform_event import DiscordPlatformEvent


@dataclass
class AgentFinalPayload:
    session_id: str
    agent: str
    event_id: str
    content: str


class AgentFinalTrigger:
    """Build and enqueue synthetic user events for agent final messages."""

    SOURCE = "dhapi_agent"
    _RECOVERABLE_CONTEXT_MARKERS = (
        "NoneType' object has no attribute 'get_channel",
        "Discord client missing",
        "live Discord client unavailable",
    )

    def __init__(self, plugin):
        self.plugin = plugin
        # umo -> last real Discord event seen in this plugin.  Used to preserve
        # sender/channel/client details for the synthetic message.
        self._event_cache: dict[str, Any] = {}
        # event_id -> monotonic timestamp.  In-memory TTL is enough to suppress
        # SSE reconnect / repeated poll replays within a plugin generation.
        self._dedupe: dict[str, float] = {}

    def remember_event(self, event) -> None:
        """Remember a real Discord event as a template for future synthetic events."""
        try:
            if event.get_platform_name() != "discord":
                return
            umo = str(event.unified_msg_origin or "")
            if not umo:
                return
            # Never cache our own synthetic events.
            if event.get_extra("synthetic", False) or event.get_extra("source") == self.SOURCE:
                return
            is_admin = self.plugin._is_admin(event)
            if not is_admin:
                return
            self._event_cache[umo] = event
        except Exception as exc:
            logger.debug("[dhapi] remember_event skipped: %s", exc)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            return bool(self.plugin.config.get(key, default))
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.plugin.config.get(key, default))
        except Exception:
            return default

    def _cfg_list(self, key: str, default: list[str]) -> list[str]:
        try:
            raw = self.plugin.config.get(key, default)
        except Exception:
            raw = default
        if isinstance(raw, str):
            return [x.strip().lower() for x in raw.split(",") if x.strip()]
        if isinstance(raw, list):
            return [str(x).strip().lower() for x in raw if str(x).strip()]
        return default

    def _cfg_template(self) -> str:
        try:
            return str(
                self.plugin.config.get(
                    "trigger_message_template",
                    "【DHAPI_AGENT_FINAL】Codex 会话已完成。这是来自 HAPI/Codex 的 assistant final 回包，不是用户新任务，请不要把它再次发送给 Codex。\n完成信息：\n{content}",
                )
            )
        except Exception:
            return "【DHAPI_AGENT_FINAL】Codex 会话已完成。这是来自 HAPI/Codex 的 assistant final 回包，不是用户新任务，请不要把它再次发送给 Codex。\n完成信息：\n{content}"

    @staticmethod
    def _safe_filename_part(value: str, default: str = "final") -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
        safe = safe.strip(".-")
        return safe[:80] or default

    def _agent_final_dir(self) -> Path:
        """Return AstrBot/data/temp/dhapi_agent_final, with plugin-local fallback."""
        plugin_dir = Path(__file__).resolve().parent
        try:
            # Normal plugin layout:
            #   <AstrBot root>/data/plugins/astrbot_plugin_hapi_discord_connector
            data_dir = plugin_dir.parent.parent
            if data_dir.name == "data":
                return data_dir / "temp" / "dhapi_agent_final"
        except Exception:
            pass
        return plugin_dir / "data" / "temp" / "agent_final"

    def _cleanup_agent_final_files(self) -> None:
        ttl_days = max(0, self._cfg_int("agent_final_file_ttl_days", 7))
        if ttl_days <= 0:
            return
        try:
            final_dir = self._agent_final_dir()
            if not final_dir.exists():
                return
            cutoff = time.time() - ttl_days * 86400
            for path in final_dir.iterdir():
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _preview(self, content: str) -> str:
        preview_chars = max(1, self._cfg_int("agent_final_preview_chars", 240))
        if len(content) <= preview_chars:
            return content
        head = content[:preview_chars]
        # Prefer not to cut mid-line when there is a useful newline near the end.
        newline_at = head.rfind("\n")
        if newline_at >= max(40, preview_chars // 2):
            head = head[:newline_at].rstrip()
        return head.rstrip() + "\n…"

    def _write_agent_final_file(self, payload: AgentFinalPayload, content: str) -> str:
        self._cleanup_agent_final_files()
        final_dir = self._agent_final_dir()
        final_dir.mkdir(parents=True, exist_ok=True)
        sid = self._safe_filename_part(payload.session_id[:8], "session")
        eid = self._safe_filename_part(payload.event_id, str(time.time_ns()))
        path = final_dir / f"{sid}-{eid}.md"
        if path.exists():
            path = final_dir / f"{sid}-{eid}-{time.time_ns()}.md"
        path.write_text(content, encoding="utf-8")
        return str(path.resolve())

    def _prepare_final_message(
        self, payload: AgentFinalPayload
    ) -> tuple[str, dict[str, Any]]:
        """Prepare synthetic message text and metadata for the final payload.

        Long finals are written to a local file so AstrBot memory/vector hooks do
        not receive the whole assistant final as a retrieval query.
        """
        content = self._truncate(payload.content)
        self._cleanup_agent_final_files()
        use_file_at = max(1, self._cfg_int("agent_final_use_file_when_chars", 800))
        meta: dict[str, Any] = {
            "dhapi_final_chars": len(payload.content),
        }
        if len(content) < use_file_at:
            meta["dhapi_final_preview_chars"] = len(content)
            return content, meta

        preview = self._preview(content)
        file_path = self._write_agent_final_file(payload, content)
        meta.update(
            {
                "dhapi_final_file": file_path,
                "dhapi_final_preview_chars": len(preview),
            }
        )
        text = (
            "【DHAPI_AGENT_FINAL】Codex 会话已完成。这是 HAPI/Codex 的回包，不是用户新任务，请不要再发回 Codex。\n"
            f"预览：{preview}\n"
            f"完整内容已写入文件：{file_path}\n"
            "如需查看全文，请用文件读取工具读取该路径。"
        )
        return text, meta

    def _mark_dedupe(self, event_id: str) -> bool:
        ttl = max(1, self._cfg_int("dedupe_ttl_seconds", 3600))
        now = time.monotonic()
        cutoff = now - ttl
        for key, ts in list(self._dedupe.items()):
            if ts < cutoff:
                self._dedupe.pop(key, None)
        if event_id in self._dedupe:
            return False
        self._dedupe[event_id] = now
        return True

    def _truncate(self, content: str) -> str:
        max_chars = max(1, self._cfg_int("max_content_chars", 1500))
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n…(已截断，原始长度 {len(content)} 字符)"

    def _target_umos(self, session_id: str) -> list[str]:
        return self.plugin.state_mgr.select_notification_targets(
            session_id, self.plugin.sessions_cache
        )

    def _find_discord_platform(self, platform_id: str) -> DiscordPlatformAdapter | None:
        for platform in self.plugin.context.platform_manager.platform_insts:
            if not isinstance(platform, DiscordPlatformAdapter):
                continue
            meta = platform.meta()
            if meta.id == platform_id and meta.name == "discord":
                return platform
        return None

    def _live_discord_context(self, umo: str):
        """Return (MessageSession, live Discord platform, live client) or raise.

        The synthetic event must use the current DiscordPlatformAdapter context.
        This plugin does not modify AstrBot core, so after isinstance checks it
        uses DiscordPlatformAdapter's concrete fields directly.
        """
        try:
            session = MessageSession.from_str(umo)
        except Exception as exc:
            raise RuntimeError(f"invalid target unified_msg_origin {umo!r}: {exc}") from exc

        platform = self._find_discord_platform(session.platform_name)
        if platform is None:
            raise RuntimeError(f"cannot find live Discord platform for {umo}")

        client = platform.client
        if client is None:
            raise RuntimeError(f"live Discord client unavailable for synthetic event umo={umo}")

        return session, platform, client

    @classmethod
    def _is_recoverable_context_error(cls, exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}"
        return any(marker in text for marker in cls._RECOVERABLE_CONTEXT_MARKERS)

    @staticmethod
    def _validate_event_send_context(event, target_umo: str) -> None:
        """Fail before enqueueing an event that would obviously explode in send()."""
        client = event.client
        if client is None:
            raise RuntimeError(
                f"Discord client missing for synthetic event target_umo={target_umo}"
            )
        try:
            client.get_channel
        except AttributeError as exc:
            raise RuntimeError(
                f"Discord client missing get_channel for synthetic event target_umo={target_umo}"
            ) from exc

    def _fallback_sender_id(self, umo: str) -> str | None:
        try:
            admins = [str(x) for x in self.plugin.context.get_config(umo).get("admins_id", [])]
            if admins:
                return admins[0]
        except Exception:
            pass
        try:
            admins = [str(x) for x in self.plugin.context.get_config().get("admins_id", [])]
            if admins:
                return admins[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _is_group_session(session: MessageSession) -> bool:
        return session.message_type.value == "GroupMessage"

    def _raw_message(
        self, payload: AgentFinalPayload, target_umo: str, final_meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raw = {
            "synthetic": True,
            "source": self.SOURCE,
            "session_id": payload.session_id,
            "agent": payload.agent,
            "event_id": payload.event_id,
            "target_umo": target_umo,
        }
        if final_meta:
            raw.update(final_meta)
        return raw

    def _refresh_event_flags(
        self, event, payload: AgentFinalPayload, final_meta: dict[str, Any] | None = None
    ) -> None:
        event.clear_result()
        event._force_stopped = False
        event._has_send_oper = False
        event.call_llm = False
        event.is_wake = True
        event.is_at_or_wake_command = True
        event.role = "admin"
        # This is a normal channel send context, never an interaction followup.
        event.interaction_followup_webhook = None
        event.set_extra("source", self.SOURCE)
        event.set_extra("synthetic", True)
        event.set_extra("session_id", payload.session_id)
        event.set_extra("agent", payload.agent)
        event.set_extra("event_id", payload.event_id)
        if final_meta:
            for key, value in final_meta.items():
                event.set_extra(key, value)

    def _build_message_obj(
        self,
        *,
        session: MessageSession,
        platform: DiscordPlatformAdapter,
        text: str,
        payload: AgentFinalPayload,
        target_umo: str,
        final_meta: dict[str, Any] | None = None,
        base_message_obj=None,
    ) -> AstrBotMessage:
        msg = copy.copy(base_message_obj) if base_message_obj is not None else AstrBotMessage()
        msg.type = session.message_type
        msg.self_id = str(platform.bot_self_id or "")
        msg.session_id = session.session_id
        msg.group_id = session.session_id if self._is_group_session(session) else ""
        msg.message_id = f"dhapi-agent-final:{payload.event_id}"
        msg.message = [Plain(text)]
        msg.message_str = text
        msg.raw_message = self._raw_message(payload, target_umo, final_meta)
        msg.timestamp = int(time.time())

        sender_id = self._fallback_sender_id(target_umo)
        if not sender_id:
            raise RuntimeError(
                f"cannot find AstrBot admin sender for synthetic event umo={target_umo}"
            )
        msg.sender = MessageMember(
            user_id=sender_id,
            nickname="DHAPI Agent",
        )
        return msg

    def _build_from_cached_event(
        self,
        base_event,
        target_umo: str,
        text: str,
        payload: AgentFinalPayload,
        final_meta: dict[str, Any] | None = None,
    ):
        # Normal cached-event path, with key Discord context refreshed from the
        # explicitly-typed live DiscordPlatformAdapter.
        session, platform, client = self._live_discord_context(target_umo)
        event = copy.copy(base_event)
        event._extras = dict(base_event._extras)
        event.client = client
        event.platform_meta = platform.meta()
        event.unified_msg_origin = target_umo
        event.session_id = session.session_id
        event.message_str = text
        event.message_obj = self._build_message_obj(
            session=session,
            platform=platform,
            text=text,
            payload=payload,
            target_umo=target_umo,
            final_meta=final_meta,
            base_message_obj=base_event.message_obj,
        )
        self._refresh_event_flags(event, payload, final_meta)
        self._validate_event_send_context(event, target_umo)
        return event

    def _build_fallback_event(
        self,
        umo: str,
        text: str,
        payload: AgentFinalPayload,
        final_meta: dict[str, Any] | None = None,
    ):
        # No cached base_event: construct a qualified DiscordPlatformEvent from
        # the HAPI session binding target.  This still enters AstrBot through the
        # normal event queue and respond stage.
        session, platform, client = self._live_discord_context(umo)
        msg = self._build_message_obj(
            session=session,
            platform=platform,
            text=text,
            payload=payload,
            target_umo=umo,
            final_meta=final_meta,
        )

        event = DiscordPlatformEvent(
            message_str=text,
            message_obj=msg,
            platform_meta=platform.meta(),
            session_id=session.session_id,
            client=client,
            interaction_followup_webhook=None,
        )
        self._refresh_event_flags(event, payload, final_meta)
        self._validate_event_send_context(event, umo)
        return event

    def _recover_event_context(
        self,
        text: str,
        payload: AgentFinalPayload,
        reason: Exception,
        target_umo: str,
        final_meta: dict[str, Any] | None = None,
    ):
        """One-shot recovery for missing cached event Discord context.

        Recovery is still main-architecture friendly: it rebuilds a normal
        DiscordPlatformEvent from payload.session_id -> target_umo -> live
        DiscordPlatformAdapter and enqueues it like any other event.
        """
        if not target_umo:
            raise RuntimeError(
                f"recovery failed: no target umo for sid={payload.session_id[:8]}"
            ) from reason
        logger.warning(
            "[dhapi] recovering synthetic agent-final Discord context sid=%s umo=%s reason=%s",
            payload.session_id[:8],
            target_umo,
            reason,
        )
        event = self._build_fallback_event(target_umo, text, payload, final_meta)
        self._validate_event_send_context(event, target_umo)
        return event

    async def trigger(self, payload: AgentFinalPayload) -> None:
        if not self._cfg_bool("enable_agent_final_trigger", False):
            return
        agent = (payload.agent or "").strip().lower()
        if agent not in set(self._cfg_list("trigger_agents", ["codex"])):
            return
        if not payload.content or not payload.content.strip():
            return
        if not self._mark_dedupe(payload.event_id):
            logger.info(
                "[dhapi] skip duplicate agent final event_id=%s sid=%s",
                payload.event_id,
                payload.session_id[:8],
            )
            return

        targets = self._target_umos(payload.session_id)
        if not targets:
            logger.warning(
                "[dhapi] agent final trigger skipped: no target umo sid=%s",
                payload.session_id[:8],
            )
            return

        try:
            text, final_meta = self._prepare_final_message(payload)
        except Exception as exc:
            logger.warning(
                "[dhapi] agent final trigger skipped: prepare message failed sid=%s event_id=%s: %s",
                payload.session_id[:8],
                payload.event_id,
                exc,
            )
            return
        queued: set[str] = set()
        for umo in targets:
            if umo in queued:
                continue
            queued.add(umo)
            base_event = self._event_cache.get(umo)
            try:
                if base_event is not None:
                    event = self._build_from_cached_event(
                        base_event, umo, text, payload, final_meta
                    )
                else:
                    event = self._build_fallback_event(umo, text, payload, final_meta)
            except Exception as exc:
                if not self._is_recoverable_context_error(exc):
                    logger.warning(
                        "[dhapi] agent final trigger skipped sid=%s event_id=%s umo=%s: %s",
                        payload.session_id[:8],
                        payload.event_id,
                        umo,
                        exc,
                    )
                    continue
                try:
                    event = self._recover_event_context(text, payload, exc, umo, final_meta)
                except Exception as recover_exc:
                    logger.warning(
                        "[dhapi] agent final trigger recovery failed sid=%s event_id=%s umo=%s: %s",
                        payload.session_id[:8],
                        payload.event_id,
                        umo,
                        recover_exc,
                    )
                    continue

            try:
                self.plugin.context.get_event_queue().put_nowait(event)
                logger.info(
                    "[dhapi] queued synthetic agent-final event sid=%s agent=%s event_id=%s umo=%s text_len=%s base_event=%s",
                    payload.session_id[:8],
                    agent,
                    payload.event_id,
                    umo,
                    len(text),
                    base_event is not None,
                )
            except Exception as exc:
                logger.warning(
                    "[dhapi] agent final trigger enqueue failed sid=%s event_id=%s umo=%s: %s",
                    payload.session_id[:8],
                    payload.event_id,
                    umo,
                    exc,
                )
