"""LLM 工具集成 - 为 LLM 提供 HAPI Coding Session 交互能力"""

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.provider import ProviderRequest

from . import formatters, session_ops


class LLMIntegration:
    """LLM 工具集成管理器"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.client = plugin.client
        self.state_mgr = plugin.state_mgr
        self.pending_mgr = plugin.pending_mgr
        self.sessions_cache = plugin.sessions_cache

    # ──── 工具可见性控制 ────

    async def on_llm_request_hook(
        self, event: AstrMessageEvent, request: ProviderRequest
    ):
        """根据权限和窗口状态动态控制工具可见性"""
        # 1. 权限检查：非管理员移除所有工具
        is_admin = self.plugin._is_admin(event)
        logger.debug(f"[LLM工具] 权限检查: is_admin={is_admin}")
        if not is_admin:
            self._remove_hapi_tools(request, keep_basic=False)
            logger.debug("[LLM工具] 非管理员，已移除所有工具")
            return

        # LLM 工具也视为当前 Discord 窗口的活跃入口：确保外部 REST
        # 创建的无 owner session 后续可以稳定 fallback 到这个默认窗口。
        await self.state_mgr.ensure_primary_session(event)

        # 2. 上下文检查：窗口无可见 session 时只保留基础工具
        visible_sessions = self.state_mgr.visible_sessions_for_window(
            event, self.sessions_cache
        )
        logger.debug(
            f"[LLM工具] 可见session数: {len(visible_sessions)}, 总session数: {len(self.sessions_cache)}"
        )
        if not visible_sessions:
            self._remove_hapi_tools(request, keep_basic=True)
            logger.debug("[LLM工具] 当前窗口无可见session，已移除非基础工具")
            return

    def _remove_hapi_tools(self, request: ProviderRequest, keep_basic: bool = False):
        """移除所有 dhapi_coding 工具

        Args:
            keep_basic: 是否保留基础工具（list_sessions/list_commands/execute_command/send_message）
        """
        if not hasattr(request, "func_tool") or not request.func_tool:
            return

        # 基础工具（始终可用）
        basic_tools = {
            "dhapi_coding_list_sessions",
            "dhapi_coding_list_commands",
            "dhapi_coding_execute_command",
            "dhapi_coding_send_message",
            "dhapi_coding_join_session",
            "dhapi_coding_leave_session",
            "dhapi_coding_create_session",
            "dhapi_coding_get_config_status",
            "dhapi_coding_change_config",
            "dhapi_coding_archive_session",
            "dhapi_coding_delete_session",
        }

        # 所有工具
        all_tools = {
            "dhapi_coding_get_status",
            "dhapi_coding_list_sessions",
            "dhapi_coding_message_history",
            "dhapi_coding_get_config_status",
            "dhapi_coding_list_commands",
            "dhapi_coding_send_message",
            "dhapi_coding_join_session",
            "dhapi_coding_leave_session",
            "dhapi_coding_create_session",
            "dhapi_coding_change_config",
            "dhapi_coding_stop_message",
            "dhapi_coding_archive_session",
            "dhapi_coding_delete_session",
            "dhapi_coding_execute_command",
        }

        # 决定要移除的工具
        tools_to_remove = all_tools - basic_tools if keep_basic else all_tools

        for tool_name in tools_to_remove:
            request.func_tool.remove_tool(tool_name)

    # ──── 审批机制 ────

    async def _require_approval(
        self, tool_name: str, args: dict, event: AstrMessageEvent
    ) -> tuple[bool, str]:
        """请求审批并等待结果

        Returns:
            (approved, reason): approved=True 表示批准，reason 说明原因
            （"approved"/"denied"/"timeout"/"auto_approved"）
        """
        # LLM 工具审批使用窗口 ID 作为 key，而不是 session ID
        window_id = event.unified_msg_origin
        await self.state_mgr.ensure_primary_session(event)

        # 添加到 pending 队列（伪装成 HAPI 权限请求）
        req_id, future, index = self.pending_mgr.add_llm_tool_request(
            window_id, tool_name, args
        )

        # 计算当前待审批总数（LLM 工具审批不受窗口限制，统计所有待审批）
        items = self.pending_mgr.flatten_pending(None, None)
        total = len(items)

        # 计算窗口数量
        visible_sids = {
            s.get("id")
            for s in self.state_mgr.visible_sessions_for_window(
                event, self.sessions_cache
            )
            if s.get("id")
        }
        visible_sids.add(event.unified_msg_origin)
        window_items = self.pending_mgr.flatten_pending(event, visible_sids)
        window_total = len(window_items)

        # 发送通知到当前窗口
        args_str = ", ".join(f"{k}={v}" for k, v in args.items())
        msg = f"""🤖 Astrbot 工具调用请求
  {tool_name}
  参数: {args_str}

当前总共 {total} 个待审批，当前对话窗口共 {window_total} 个待审批，此请求审批序号 {index}

审批方式:
  直接点击下方按钮批准/拒绝
  或打开 /dhapi → 审批 面板处理"""

        notification_sent = False
        embed = None
        try:
            fields = [
                {"name": "工具", "value": f"`{tool_name}`", "inline": True},
                {"name": "序号", "value": f"`{index}`", "inline": True},
                {"name": "参数", "value": args_str[:1024] or "-", "inline": False},
                {
                    "name": "审批方式",
                    "value": "直接点击下方按钮批准/拒绝，或打开 `/dhapi` → `审批` 面板处理",
                    "inline": False,
                },
            ]
            embed = self.plugin.notification_mgr.make_embed(
                title=f"待审批 - {tool_name}",
                description=f"当前总共 {total} 个待审批，当前 Discord 频道共 {window_total} 个待审批。",
                color=0xE74C3C,
                fields=fields,
                footer="LLM tool approval | HAPI Discord Connector",
            )
            from .discord_ui import ApprovalNoticeView
            from .notification_manager import make_view_component

            view = ApprovalNoticeView(self.plugin, event, window_id, req_id)
            await event.send(MessageChain([embed, make_view_component(view)]))
            notification_sent = True
        except Exception as e:
            logger.warning(
                f"LLM 工具审批 Embed+按钮通知发送失败，尝试 Embed-only 降级: {e}"
            )
            if embed is not None:
                try:
                    await event.send(MessageChain([embed]))
                    notification_sent = True
                except Exception as embed_exc:
                    logger.warning(
                        f"LLM 工具审批 Embed-only 通知发送失败，尝试纯文本降级: {embed_exc}"
                    )
        if not notification_sent:
            try:
                await event.send(MessageChain().message(msg))
                notification_sent = True
            except Exception as text_exc:
                logger.warning(f"LLM 工具审批纯文本通知发送失败: {text_exc}")

        # 通知失败时不要直接 notification_failed 拒绝：
        # - 自动审批开启表示全天托管，直接批准并清理 pending。
        # - 自动审批关闭则保留 pending，/dhapi 审批面板仍可在超时前处理。
        if not notification_sent:
            if self.plugin.sse_listener._auto_approve_enabled:
                self.pending_mgr.remove_entry(window_id, req_id)
                logger.info(f"自动审批开启，通知失败后自动批准 {tool_name}")
                return True, "auto_approved"
            logger.warning(
                "LLM 工具 %s 审批通知发送失败，保留 pending 供 /dhapi 面板处理",
                tool_name,
            )

        # 等待审批结果（1分钟超时）
        try:
            approved = await asyncio.wait_for(future, timeout=60)
            return (True, "approved") if approved else (False, "denied")
        except asyncio.TimeoutError:
            # 超时，清理请求
            self.pending_mgr.remove_entry(window_id, req_id)
            logger.warning(f"LLM 工具 {tool_name} 审批超时（60秒无响应）")
            # 自动审批开启时，超时默认允许
            if self.plugin.sse_listener._auto_approve_enabled:
                logger.info(f"自动审批开启，自动批准 {tool_name}")
                return True, "auto_approved"
            return False, "timeout"
        except asyncio.CancelledError:
            # 任务被取消（通常是外部超时），清理并返回拒绝，不再传播异常
            self.pending_mgr.remove_entry(window_id, req_id)
            logger.warning(f"LLM 工具 {tool_name} 审批被取消")
            return False, "cancelled"

    def _joined_session_lines(self, event: AstrMessageEvent) -> list[str]:
        """格式化当前窗口已加入 session 的短列表。"""
        joined = self.state_mgr.binding_mgr.get_window_sessions(event.unified_msg_origin)
        lines: list[str] = []
        for idx, sid in enumerate(joined, 1):
            session = next((s for s in self.sessions_cache if s.get("id") == sid), None)
            title = "(未知 session)"
            if session:
                meta = session.get("metadata") or {}
                summary = (meta.get("summary") or {}).get("text", "") or "(无标题)"
                flavor = meta.get("flavor", "?")
                title = f"[{flavor}] {summary}"
            lines.append(f"{idx}. {sid[:8]} - {title}")
        return lines

    def _resolve_sid_text(
        self, event: AstrMessageEvent, session_id: str | None
    ) -> tuple[str | None, str | None]:
        """解析工具目标 session；失败时返回 LLM 可读提示。"""
        sid, reason = self.state_mgr.resolve_target_sid(event, session_id)
        if sid:
            return sid, None
        if reason == "ambiguous":
            lines = self._joined_session_lines(event)
            return (
                None,
                "当前窗口已加入多个 session，请在下一轮工具调用里显式传 session_id：\n"
                + "\n".join(lines[:10]),
            )
        return None, self._missing_session_text()

    def validate_sid(self, sid: str) -> bool:
        """检查 sid 是否存在于当前 sessions_cache。"""
        target = str(sid or "").strip()
        return bool(target) and any(s.get("id") == target for s in self.sessions_cache)

    @staticmethod
    def _missing_sid_text(sid: str) -> str:
        return f"session_id `{str(sid or '')[:8]}` 不存在，请用 dhapi_coding_list_sessions 确认。"

    @staticmethod
    def _missing_session_text() -> str:
        return (
            "当前窗口未加入任何 session，请先用 dhapi_coding_join_session(session_id) 加入，"
            "或在参数里直接传 session_id。"
        )

    @staticmethod
    def _result_to_text(result) -> str:
        """从 AstrBot MessageEventResult 中提取纯文本，供 LLM 工具 return str。"""
        if hasattr(result, "chain"):
            parts = []
            for seg in result.chain:
                if hasattr(seg, "text"):
                    parts.append(str(seg.text))
            if parts:
                return "".join(parts)
        return str(result)

    # ──── 查询类工具（无需审批）────

    async def tool_get_status(self, event: AstrMessageEvent, session_id: str = ""):
        """获取 HAPI session 的状态信息。"""
        sid, error = self._resolve_sid_text(event, session_id)
        if error:
            return error
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)

        try:
            detail = await session_ops.fetch_session_detail(self.client, sid)
            return formatters.format_session_status(detail)
        except Exception as e:
            return f"获取状态失败: {e}"

    async def tool_list_sessions(
        self,
        event: AstrMessageEvent,
        window: str = "",
        path: str = "",
        agent: str = "",
        joined_only: bool = False,
    ):
        """列出 HAPI 的可交互 session 列表。

        Args:
            window(string): 按聊天窗口过滤（默认为空表示当前窗口，设为 'all' 查询所有聊天窗口，用户没有明确要求时一般置空）
            path(string): 按路径搜索
            agent(string): 按代理类型过滤（claude/codex/gemini/opencode）
            joined_only(boolean): 仅列出当前 Discord 窗口已加入的 session
        """
        if joined_only:
            joined_sids = self.state_mgr.binding_mgr.get_window_sessions(
                event.unified_msg_origin
            )
            by_sid = {
                session.get("id"): session
                for session in self.sessions_cache
                if session.get("id")
            }
            sessions = [by_sid[sid] for sid in joined_sids if sid in by_sid]
            auto_switched = False
        else:
            # 当前窗口无session时，自动查询所有session
            visible_sessions = self.state_mgr.visible_sessions_for_window(
                event, self.sessions_cache
            )
            if not visible_sessions and window == "":
                window = "all"
                auto_switched = True
            else:
                auto_switched = False

            if window == "all":
                sessions = self.sessions_cache
            else:
                sessions = visible_sessions

        # 过滤
        if path:
            sessions = [
                s
                for s in sessions
                if path.lower() in s.get("metadata", {}).get("path", "").lower()
            ]
        if agent:
            sessions = [
                s
                for s in sessions
                if s.get("metadata", {}).get("flavor", "").lower() == agent.lower()
            ]

        if not sessions:
            return "没有找到符合条件的 session"
            return

        # 复用 formatters.format_session_list，但移除 emoji；只有窗口恰好加入一个
        # session 时才显示为当前糖。
        current_sid, _ = self.state_mgr.resolve_target_sid(event, "")
        text = formatters.format_session_list(
            sessions,
            current_sid,
            self.sessions_cache,
            header_current_window=event.unified_msg_origin,
            session_owners=self.state_mgr.binding_mgr.get_all_bindings(),
            owner_formatter=lambda umo: self.state_mgr.format_umo_for_display(
                umo, max_len=32
            ),
        )

        # 替换 emoji 为文字
        text = text.replace("📁", "[目录]")
        text = text.replace("🏷️", "ID:")
        text = text.replace("💭", "[思考中]")
        text = text.replace("🟢", "[运行中]")
        text = text.replace("⚪", "[已关闭]")
        text = text.replace("🤖", "")
        text = text.replace("⚠️", "[待审批]")
        text = text.replace("💡", "提示:")

        # 如果自动切换到all，添加提示
        if auto_switched:
            text = "提示：当前窗口无可见session，已自动查询所有窗口的session\n\n" + text

        return text

    async def tool_message_history(
        self, event: AstrMessageEvent, rounds: int = 1, session_id: str = ""
    ):
        """查询 HAPI session 的历史消息。

        Args:
            rounds(number): 查询最近几轮消息（默认 1 轮）
            session_id(string): 可选，显式 session ID；不传时当前窗口必须只加入一个 session
        """
        sid, error = self._resolve_sid_text(event, session_id)
        if error:
            return error
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)

        try:
            # 多取消息以保证覆盖 N 轮
            fetch_limit = min(rounds * 80, 500)
            msgs = await session_ops.fetch_messages(self.client, sid, limit=fetch_limit)
            all_rounds = formatters.split_into_rounds(msgs)
            # 取最后 N 轮
            selected = all_rounds[-rounds:]
            if not selected:
                return "暂无消息记录"
                return

            # 格式化所有轮次
            lines = []
            total = len(selected)
            for i, round_msgs in enumerate(selected, 1):
                text = formatters.format_round(round_msgs, i, total)
                lines.append(text)

            return "\n\n".join(lines)
        except Exception as e:
            return f"获取消息失败: {e}"

    async def tool_get_config_status(self, event: AstrMessageEvent):
        """获取当前插件配置状态及可修改项说明。"""
        output_level = self.plugin.config.get("output_level", "simple")
        auto_approve = self.plugin.sse_listener._auto_approve_enabled
        remind = self.plugin.sse_listener._remind_enabled
        remind_interval = self.plugin.sse_listener._remind_interval
        agent_final_trigger = self.plugin.config.get(
            "enable_agent_final_trigger", False
        )
        trigger_agents = self.plugin.config.get("trigger_agents", ["codex"])
        max_content_chars = self.plugin.config.get("max_content_chars", 1500)
        final_preview_chars = self.plugin.config.get("agent_final_preview_chars", 240)
        final_file_ttl_days = self.plugin.config.get("agent_final_file_ttl_days", 7)
        final_use_file_when_chars = self.plugin.config.get(
            "agent_final_use_file_when_chars", 800
        )
        info = f"""当前配置状态:

output_level (SSE推送级别): {output_level}
  - silence: 仅推送权限请求和任务完成提醒
  - simple: 仅推送 agent 文本消息，不包含复杂的工具调用信息
  - summary: 任务完成时推送最近的 agent 消息
  - detail: 实时推送所有新消息（信息量较大）

auto_approve_enabled (24小时自动审批): {"开启" if auto_approve else "关闭"}
  开启后全天自动批准非 question 权限请求
  值: true/false

remind_pending (定时提醒待审批): {"开启" if remind else "关闭"}
  间隔: {remind_interval} 秒
  值: true/false

enable_agent_final_trigger (agent final 触发 AstrBot 主链): {"开启" if agent_final_trigger else "关闭"}
  仅响应 HAPI 原始 SSE 完成事件中的 assistant final；默认只支持 codex
  trigger_agents: {trigger_agents}
  max_content_chars: {max_content_chars}
  agent_final_preview_chars: {final_preview_chars}
  agent_final_file_ttl_days: {final_file_ttl_days}
  agent_final_use_file_when_chars: {final_use_file_when_chars}
  值: true/false"""
        return info

    async def tool_list_commands(self, event: AstrMessageEvent, topic: str = ""):
        """说明 HAPI Discord Connector 的交互方式与 LLM 工具能力。"""
        return (
            "Discord 侧仅保留 /dhapi 一个入口，会打开按钮/下拉菜单/Modal 控制面板；"
            "不再支持 /dhapi list/sw/a/deny 等文本子命令。\n"
            "请在 /dhapi 面板加入/退出 session；LLM 可直接使用 "
            "dhapi_coding_list_sessions、dhapi_coding_join_session、"
            "dhapi_coding_send_message、dhapi_coding_stop_message 等工具完成操作。"
        )

    # ──── 操作类工具（需要审批）────

    async def tool_send_message(
        self, event: AstrMessageEvent, message: str, session_id: str = ""
    ):
        """向 HAPI session 发送消息。

        Args:
            message(string): 要发送的消息内容
            session_id(string): 可选，显式 session ID；不传时当前窗口必须只加入一个 session
        """
        sid, error = self._resolve_sid_text(event, session_id)
        if error:
            return error
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)

        # 请求审批
        approved, reason = await self._require_approval(
            "dhapi_coding_send_message",
            {"message": message, "session_id": sid[:8]},
            event,
        )
        logger.debug(f"[tool_send_message] approved={approved}, reason={reason}")
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已绑定 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"
            return

        # 执行发送
        ok, result = await session_ops.send_message(self.client, sid, message)
        return result if ok else f"发送失败: {result}"

    def _find_session_by_id_or_prefix(self, target: str) -> tuple[dict | None, str | None]:
        """按完整 ID / 前缀 / 列表序号解析 session。"""
        sessions = self.sessions_cache
        value = (target or "").strip()
        if not value:
            return None, "请提供 session_id。"
        if value.isdigit():
            idx = int(value)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1], None
        matches = [s for s in sessions if s.get("id", "").startswith(value)]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"匹配到 {len(matches)} 个 session，请提供更长的 ID 前缀。"
        return None, f"未找到匹配的 session: {value}"

    async def tool_join_session(self, event: AstrMessageEvent, session_id: str):
        """把指定 session 加入当前 Discord 窗口订阅。

        Args:
            session_id(string): session ID / ID 前缀 / session 列表序号
        """
        # 请求审批
        approved, reason = await self._require_approval(
            "dhapi_coding_join_session", {"session_id": session_id}, event
        )
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已绑定 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"
            return

        await self.plugin._refresh_sessions()
        chosen, error = self._find_session_by_id_or_prefix(session_id)
        if error:
            return error

        sid = chosen["id"]
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)
        flavor = (chosen.get("metadata") or {}).get("flavor", "claude")
        await self.state_mgr.join_session(sid, event.unified_msg_origin, flavor)
        await self.state_mgr.set_user_state(event)
        logger.info(
            "[dhapi] LLM join sid=%s umo=%s flavor=%s",
            sid[:8],
            event.unified_msg_origin,
            flavor,
        )
        return f"已在当前 Discord 窗口加入 [{flavor}] {sid[:8]}"

    async def tool_leave_session(self, event: AstrMessageEvent, session_id: str = ""):
        """当前 Discord 窗口退出指定 session 订阅。"""
        sid, error = self._resolve_sid_text(event, session_id)
        if error:
            return error
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)

        approved, reason = await self._require_approval(
            "dhapi_coding_leave_session", {"session_id": sid[:8]}, event
        )
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已加入 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"

        await self.state_mgr.leave_session(sid, event.unified_msg_origin)
        await self.state_mgr.set_user_state(event)
        return f"已从当前 Discord 窗口退出 session `{sid[:8]}`"

    async def tool_create_session(
        self,
        event: AstrMessageEvent,
        directory: str,
        agent: str,
        machine_id: str = "",
        session_type: str = "simple",
        yolo: bool = False,
        model_reasoning_effort: str = "",
    ):
        """创建新的 coding session。

        Args:
            directory(string): 工作目录路径
            agent(string): 代理类型（claude/codex/gemini/opencode）
            machine_id(string): 机器 ID（可选，管理多机器时必填）
            session_type(string): session 类型（simple/worktree，默认 simple）
            yolo(boolean): 是否自动批准所有权限（默认 false）
            model_reasoning_effort(string): 仅 Codex 可选；留空表示继承 Codex 默认设置，可选 none/minimal/low/medium/high/xhigh
        """
        # 获取机器列表
        try:
            machines = await session_ops.fetch_machines(self.client)
        except Exception as e:
            return f"获取机器列表失败: {e}"
            return

        if not machines:
            return "没有在线的机器"
            return

        agent = (agent or "").strip().lower()
        from .constants import AGENTS

        if agent not in AGENTS:
            return f"不支持的 agent: {agent}，可选: {', '.join(AGENTS)}"
            return

        # 处理 machine_id
        if not machine_id:
            if len(machines) == 1:
                machine_id = machines[0].get("id")
            else:
                lines = ["有多个机器在线，请指定 machine_id:"]
                for m in machines:
                    mid = m.get("id", "?")
                    meta = m.get("metadata", {})
                    host = meta.get("host", "unknown")
                    plat = meta.get("platform", "?")
                    lines.append(f"  - {mid}: {host} ({plat})")
                return "\n".join(lines)
                return

        normalized_effort = (model_reasoning_effort or "").strip().lower()
        if agent == "codex":
            from .constants import CODEX_REASONING_EFFORT_VALUES

            inherit_aliases = {"", "inherit", "default", "auto"}
            if normalized_effort in inherit_aliases:
                normalized_effort = ""
            elif normalized_effort not in CODEX_REASONING_EFFORT_VALUES:
                return "Codex 的 model_reasoning_effort 只能是留空(继承默认配置)或 none/minimal/low/medium/high/xhigh"
                return
        elif normalized_effort:
            return "只有 Codex 支持 model_reasoning_effort；其他代理请留空"
            return

        approval_payload = {
            "machine_id": machine_id,
            "directory": directory,
            "agent": agent,
            "session_type": session_type,
            "yolo": yolo,
        }
        if agent == "codex":
            approval_payload["model_reasoning_effort"] = normalized_effort or "inherit"

        # 请求审批
        approved, reason = await self._require_approval(
            "dhapi_coding_create_session", approval_payload, event
        )
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已绑定 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"
            return

        # 执行创建
        ok, msg, sid = await session_ops.spawn_session(
            self.client,
            machine_id,
            directory,
            agent,
            session_type,
            yolo,
            model_reasoning_effort=normalized_effort or None,
        )
        if ok and sid:
            await self.state_mgr.join_session(sid, event.unified_msg_origin, agent)
            await self.state_mgr.set_user_state(event)
            logger.info(
                "[dhapi] LLM create joined sid=%s umo=%s flavor=%s",
                sid[:8],
                event.unified_msg_origin,
                agent,
            )
            return f"✅ 已创建 session: {sid[:8]}"
        else:
            return f"创建失败: {msg}"

    async def tool_change_config(
        self, event: AstrMessageEvent, config_name: str, value: str
    ):
        """修改插件配置项。必须先调用 dhapi_coding_get_config_status 查看可修改项。

        Args:
            config_name(string): 配置项名称
            value(string): 新值
        """
        # 请求审批
        approved, reason = await self._require_approval(
            "dhapi_coding_change_config",
            {"config_name": config_name, "value": value},
            event,
        )
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已绑定 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"
            return

        # 执行修改
        if config_name == "output_level":
            if value not in ["silence", "summary", "simple", "detail"]:
                return "output_level 只能是 silence/summary/simple/detail"
                return
            self.plugin.sse_listener.output_level = value
            self.plugin.config["output_level"] = value
            self.plugin.config.save_config()
            return f"✅ 已设置 {config_name} = {value}"
        elif config_name == "auto_approve_enabled":
            bool_val = value.lower() in ["true", "1", "yes", "on", "开启"]
            self.plugin.sse_listener._auto_approve_enabled = bool_val
            self.plugin.config["auto_approve_enabled"] = bool_val
            self.plugin.config.save_config()
            return f"✅ 已设置 {config_name} = {bool_val}"
        elif config_name == "remind_pending":
            bool_val = value.lower() in ["true", "1", "yes", "on", "开启"]
            self.plugin.sse_listener._remind_enabled = bool_val
            self.plugin.config["remind_pending"] = bool_val
            self.plugin.config.save_config()
            return f"✅ 已设置 {config_name} = {bool_val}"
        elif config_name == "enable_agent_final_trigger":
            bool_val = value.lower() in ["true", "1", "yes", "on", "开启"]
            self.plugin.config["enable_agent_final_trigger"] = bool_val
            self.plugin.config.save_config()
            return f"✅ 已设置 {config_name} = {bool_val}"
        else:
            return f"不支持的配置项: {config_name}，请先调用 dhapi_coding_get_config_status 查看可用配置"

    async def tool_stop_message(self, event: AstrMessageEvent, session_id: str = ""):
        """停止 HAPI session 的消息生成。"""
        sid, error = self._resolve_sid_text(event, session_id)
        if error:
            return error
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)

        # 请求审批
        approved, reason = await self._require_approval(
            "dhapi_coding_stop_message", {"session_id": sid[:8]}, event
        )
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已绑定 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"
            return

        # 执行停止
        ok, msg = await session_ops.abort_session(self.client, sid)
        if ok:
            await self.plugin._refresh_sessions()
        return msg

    async def tool_archive_session(self, event: AstrMessageEvent, session_id: str = ""):
        """归档 HAPI session。"""
        sid, error = self._resolve_sid_text(event, session_id)
        if error:
            return error
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)

        approved, reason = await self._require_approval(
            "dhapi_coding_archive_session", {"session_id": sid[:8]}, event
        )
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已绑定 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"

        ok, msg = await session_ops.archive_session(self.client, sid)
        if ok:
            await self.plugin._refresh_sessions()
        return msg

    async def tool_delete_session(self, event: AstrMessageEvent, session_id: str = ""):
        """删除 HAPI session。危险操作，必须审批。"""
        sid, error = self._resolve_sid_text(event, session_id)
        if error:
            return error
        if not self.validate_sid(sid):
            return self._missing_sid_text(sid)

        approved, reason = await self._require_approval(
            "dhapi_coding_delete_session", {"session_id": sid[:8]}, event
        )
        if not approved:
            if reason == "timeout":
                return "操作超时：60秒内未收到用户审批。请提醒用户打开 /dhapi 审批面板处理。"
            elif reason == "notification_failed":
                return "操作失败：无法发送审批通知到用户。请检查是否已绑定 session。"
            else:
                return "操作已被用户拒绝，请停止工具调用，先交流清楚问题"

        ok, msg = await session_ops.delete_session(self.client, sid)
        if ok:
            await self.plugin.state_mgr.leave_all_session_owners(sid)
            await self.plugin._refresh_sessions()
        return msg

    async def tool_execute_command(self, event: AstrMessageEvent, command: str):
        """文本 /dhapi 子命令已废弃；请改用专用 dhapi_coding_* 工具。"""
        return (
            "文本 /dhapi 子命令已废弃。Discord 用户请使用 /dhapi 打开交互面板；"
            "LLM 请直接调用 dhapi_coding_list_sessions / dhapi_coding_join_session / "
            "dhapi_coding_send_message / dhapi_coding_stop_message / "
            "dhapi_coding_archive_session / dhapi_coding_delete_session 等专用工具。"
        )
