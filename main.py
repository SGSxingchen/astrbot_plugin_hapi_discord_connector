"""HAPI Discord Connector AstrBot 插件入口
注册 /dhapi Discord 原生交互面板、SSE 生命周期管理
所有指令仅管理员可用
"""

import asyncio
import builtins
import os

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import session_ops
from .binding_manager import BindingManager
from .cf_access import CfAccessManager
from .hapi_client import AsyncHapiClient
from .notification_manager import NotificationManager
from .pending_manager import PendingManager
from .sse_listener import SSEListener
from .state_manager import StateManager

_REGISTRY_KEY = "_astrbot_hapi_discord_connector_registry"


def _process_registry() -> dict:
    """Process-global registry that survives module reloads."""
    registry = getattr(builtins, _REGISTRY_KEY, None)
    if not isinstance(registry, dict):
        registry = {"generation": 0, "plugin": None, "sse_listener": None}
        setattr(builtins, _REGISTRY_KEY, registry)
    return registry


def _is_current_generation(generation_id: int) -> bool:
    return int(_process_registry().get("generation") or 0) == int(generation_id)


async def _cancel_stale_sse_tasks():
    """Best-effort cleanup for listener tasks from older hot-reloaded code.

    Older plugin builds did not register their SSE listener globally.  During
    hot reload they can keep running with old notification_manager code.  Cancel
    any still-live task whose coroutine stack comes from this plugin's
    sse_listener.py before starting the new singleton listener.
    """
    current = asyncio.current_task()
    sse_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "sse_listener.py"))
    stale_tasks = []
    for task in asyncio.all_tasks():
        if task is current or task.done():
            continue
        stack = task.get_stack(limit=8)
        coro = task.get_coro()
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
        filenames = {
            os.path.abspath(frame.f_code.co_filename)
            for frame in stack
            if getattr(frame, "f_code", None)
        }
        if code is not None:
            filenames.add(os.path.abspath(code.co_filename))
        if sse_path in filenames:
            stale_tasks.append(task)

    if not stale_tasks:
        return

    logger.warning(
        "[dhapi] cancelling %d stale SSE task(s) from previous plugin instance",
        len(stale_tasks),
    )
    for task in stale_tasks:
        task.cancel()
    for task in stale_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("[dhapi] stale SSE task ended with error: %s", exc)


@register(
    "astrbot_plugin_hapi_discord_connector",
    "SGSxingchen",
    "HAPI 远程 coding 的 Discord 专用版：/dhapi 原生交互与 session 管理",
    "1.0.0",
)
class HapiDiscordConnectorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # HAPI 客户端
        endpoint = self.config.get("hapi_endpoint", "")
        token = self.config.get("access_token", "")
        proxy = self.config.get("proxy_url", "") or None
        jwt_life = self.config.get("jwt_lifetime", 900)
        refresh_before = self.config.get("refresh_before_expiry", 180)

        # Cloudflare Zero Trust Access（可选，仅在填写了 client_id 时生效）
        cf_id = self.config.get("cf_access_client_id", "").strip()
        cf_secret = self.config.get("cf_access_client_secret", "").strip()
        if cf_id.lower().startswith("cf-access-client-id:"):
            cf_id = cf_id.split(":", 1)[1].strip()
        if cf_secret.lower().startswith("cf-access-client-secret:"):
            cf_secret = cf_secret.split(":", 1)[1].strip()
        cf_mgr = None
        if cf_id and cf_secret:
            cf_mgr = CfAccessManager(client_id=cf_id, client_secret=cf_secret)

        self.client = AsyncHapiClient(
            endpoint=endpoint,
            access_token=token,
            proxy_url=proxy,
            jwt_lifetime=jwt_life,
            refresh_before=refresh_before,
            cf_access_mgr=cf_mgr,
        )

        # session 缓存
        self.sessions_cache: list[dict] = []

        # 绑定管理器
        self.binding_mgr = BindingManager()

        # 状态管理器
        self.state_mgr = StateManager(self, self.binding_mgr)

        # 通知管理器
        self.notification_mgr = NotificationManager(self.context, self.state_mgr, self)

        # SSE 监听器
        self.sse_listener = SSEListener(
            self.client,
            self.sessions_cache,
            self._notify_from_sse,
        )
        self.sse_listener.set_kv(self)

        # 待审批管理器
        self.pending_mgr = PendingManager(self.sse_listener)

        # summary 模式消息条数
        self._summary_msg_count = self.config.get("summary_msg_count", 5)

        # LLM 工具集成
        from .llm_integration import LLMIntegration

        self.llm_integration = LLMIntegration(self)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为管理员（动态读取配置）"""
        astrbot_config = self.context.get_config(event.unified_msg_origin)
        admin_ids = [str(x) for x in astrbot_config.get("admins_id", [])]
        return str(event.get_sender_id()) in admin_ids

    @staticmethod
    def _is_discord_event(event: AstrMessageEvent) -> bool:
        """仅允许 Discord 平台事件触发本插件逻辑，非 Discord 静默忽略。"""
        try:
            return event.get_platform_name() == "discord"
        except Exception:
            return False

    async def _notify_from_sse(self, text: str, sid: str):
        """Generation-guarded SSE notification callback."""
        generation_id = int(getattr(self, "_generation_id", 0) or 0)
        if generation_id and not _is_current_generation(generation_id):
            logger.info(
                "[dhapi] skip stale plugin notification generation=%s sid=%s",
                generation_id,
                sid[:8] if sid else "global",
            )
            return
        await self.notification_mgr.push_notification(text, sid, self.sessions_cache)

    @filter.on_llm_request()
    async def on_llm_request_hook(self, event: AstrMessageEvent, request):
        """LLM 工具可见性控制钩子；非 Discord 平台移除 dhapi 工具。"""
        if not self._is_discord_event(event):
            self.llm_integration._remove_hapi_tools(request, keep_basic=False)
            return
        await self.llm_integration.on_llm_request_hook(event, request)

    # ──── LLM 工具代理方法 ────

    @filter.llm_tool(name="dhapi_coding_get_status")
    async def tool_get_status(self, event: AstrMessageEvent) -> str:
        """获取当前 Discord 对话中已选 HAPI session 的状态信息。"""
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_get_status(event)

    @filter.llm_tool(name="dhapi_coding_list_sessions")
    async def tool_list_sessions(
        self, event: AstrMessageEvent, window: str = "", path: str = "", agent: str = ""
    ) -> str:
        """列出 Discord 当前频道可交互的 HAPI session。

        Args:
            window(string): 窗口过滤，空=当前 Discord 频道，all=所有频道。
            path(string): 按工作目录路径关键词过滤。
            agent(string): 按代理类型过滤，claude/codex/gemini/opencode。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_list_sessions(event, window, path, agent)

    @filter.llm_tool(name="dhapi_coding_message_history")
    async def tool_message_history(
        self, event: AstrMessageEvent, rounds: int = 1
    ) -> str:
        """查询当前 Discord 对话所选 session 的最近历史消息。

        Args:
            rounds(number): 查询最近几轮消息，默认 1 轮。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_message_history(event, rounds)

    @filter.llm_tool(name="dhapi_coding_get_config_status")
    async def tool_get_config_status(self, event: AstrMessageEvent) -> str:
        """获取 HAPI Discord Connector 当前配置状态和可修改项说明。"""
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_get_config_status(event)

    @filter.llm_tool(name="dhapi_coding_list_commands")
    async def tool_list_commands(self, event: AstrMessageEvent, topic: str = "") -> str:
        """列出 /dhapi 可用指令，供 LLM 在 Discord 中选择正确操作。

        Args:
            topic(string): 帮助专题，可选：会话/对话/审批/通知/文件/配置/全部；留空显示常用帮助。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_list_commands(event, topic)

    @filter.llm_tool(name="dhapi_coding_send_message")
    async def tool_send_message(self, event: AstrMessageEvent, message: str) -> str:
        """向当前 Discord 对话绑定的 HAPI session 发送消息；需要用户在 Discord 中审批。

        Args:
            message(string): 要发送给 coding agent 的消息内容。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_send_message(event, message)

    @filter.llm_tool(name="dhapi_coding_switch_session")
    async def tool_switch_session(self, event: AstrMessageEvent, target: str) -> str:
        """切换当前 Discord 频道正在操作的 HAPI session；需要用户审批。

        Args:
            target(string): session 序号如 1，或 session ID 前缀如 abc12345。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_switch_session(event, target)

    @filter.llm_tool(name="dhapi_coding_create_session")
    async def tool_create_session(
        self,
        event: AstrMessageEvent,
        directory: str,
        agent: str,
        machine_id: str = "",
        session_type: str = "simple",
        yolo: bool = False,
        model_reasoning_effort: str = "",
    ) -> str:
        """创建新的 HAPI coding session，成功后绑定到当前 Discord 频道；需要用户审批。

        Args:
            directory(string): 工作目录路径。
            agent(string): 代理类型，claude/codex/gemini/opencode。
            machine_id(string): 机器 ID，可选，多机器在线时必填。
            session_type(string): session 类型，simple 或 worktree，默认 simple。
            yolo(boolean): 是否自动批准该 session 内所有权限，默认 false。
            model_reasoning_effort(string): 仅 Codex 可选；留空继承默认，可选 none/minimal/low/medium/high/xhigh。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_create_session(
            event,
            directory,
            agent,
            machine_id,
            session_type,
            yolo,
            model_reasoning_effort,
        )

    @filter.llm_tool(name="dhapi_coding_change_config")
    async def tool_change_config(
        self, event: AstrMessageEvent, config_name: str, value: str
    ) -> str:
        """修改 HAPI Discord Connector 配置；调用前先用 dhapi_coding_get_config_status 查看可改项。

        Args:
            config_name(string): 配置项名称。
            value(string): 新值。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_change_config(event, config_name, value)

    @filter.llm_tool(name="dhapi_coding_stop_message")
    async def tool_stop_message(self, event: AstrMessageEvent) -> str:
        """停止当前 Discord 对话绑定 session 的消息生成；需要用户审批。"""
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_stop_message(event)

    @filter.llm_tool(name="dhapi_coding_archive_session")
    async def tool_archive_session(self, event: AstrMessageEvent) -> str:
        """归档当前 Discord 对话绑定的 HAPI session；危险操作，需要用户审批。"""
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_archive_session(event)

    @filter.llm_tool(name="dhapi_coding_delete_session")
    async def tool_delete_session(self, event: AstrMessageEvent) -> str:
        """删除当前 Discord 对话绑定的 HAPI session；危险操作，需要用户审批。"""
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_delete_session(event)

    @filter.llm_tool(name="dhapi_coding_execute_command")
    async def tool_execute_command(self, event: AstrMessageEvent, command: str) -> str:
        """兼容旧工具名；文本子命令已废弃，请改用专用 dhapi_coding_* 工具。

        Args:
            command(string): 已废弃的文本命令内容。
        """
        if not self._is_discord_event(event):
            return "dhapi 工具仅支持 Discord 平台。"
        return await self.llm_integration.tool_execute_command(event, command)

    # ──── 辅助方法 ────

    async def _refresh_sessions(self):
        """刷新 HAPI session 缓存。"""
        try:
            self.sessions_cache[:] = await session_ops.fetch_sessions(self.client)
        except Exception as e:
            logger.warning("刷新 session 列表失败: %s", e)
            raise

    def _conn_warning(self) -> str | None:
        """SSE 连接异常时返回警告文本，正常时返回 None"""
        was_hibernated = self.sse_listener._hibernated
        self.sse_listener.wake_up()
        if was_hibernated:
            return "💤 SSE 已从休眠中唤醒，正在后台重连...\n请等待连接恢复通知后，使用 /dhapi 打开面板查看连接状态\n"
        n = self.sse_listener.conn_fail_count
        if n > 0:
            return f"⚠ SSE 连接已连续失败 {n} 次，正在后台重连...\n"
        return None

    # ──── 生命周期 ────

    async def initialize(self):
        """插件初始化：打开 client、加载用户状态、启动 SSE"""
        registry = _process_registry()
        old_listener = registry.get("sse_listener")
        old_plugin = registry.get("plugin")
        if old_listener is not None and old_listener is not self.sse_listener:
            logger.warning("[dhapi] 检测到旧 HAPI Discord Connector SSE，正在停止")
            try:
                setattr(old_listener, "_stopped", True)
                await old_listener.stop()
            except Exception as exc:
                logger.warning("[dhapi] 停止旧 SSE listener 失败: %s", exc)
        if old_plugin is not None and old_plugin is not self:
            old_client = getattr(old_plugin, "client", None)
            if old_client is not None and old_client is not self.client:
                try:
                    await old_client.close()
                except Exception as exc:
                    logger.warning("[dhapi] 关闭旧 HAPI client 失败: %s", exc)

        await _cancel_stale_sse_tasks()

        generation_id = int(registry.get("generation") or 0) + 1
        registry["generation"] = generation_id
        registry["plugin"] = self
        registry["sse_listener"] = self.sse_listener
        self._generation_id = generation_id
        logger.info("[dhapi] activate singleton generation=%s", generation_id)

        await self.client.init()

        # 从 KV 加载状态
        await self.state_mgr.load_all()

        # 执行数据迁移
        await self.state_mgr.migrate_to_capture_model()

        # 加载 session 缓存
        try:
            self.sessions_cache[:] = await session_ops.fetch_sessions(self.client)
        except Exception as e:
            logger.warning("初始化加载 session 列表失败: %s", e)

        # 加载已有的待审批请求（重启/断联后恢复）
        await self.sse_listener.load_compact_state()
        await self.sse_listener.load_existing_pending()

        # 启动 SSE
        output_level = self.config.get("output_level", "simple")
        remind = self.config.get("remind_pending", True)
        remind_interval = self.config.get("remind_interval", 180)
        auto_approve = self.config.get("auto_approve_enabled", False)
        max_reconnect = self.config.get("max_reconnect_attempts", 30)
        self.sse_listener.start(
            output_level,
            remind_pending=remind,
            remind_interval=remind_interval,
            auto_approve_enabled=auto_approve,
            summary_msg_count=self._summary_msg_count,
            max_reconnect_attempts=max_reconnect,
            generation_id=generation_id,
            is_current_generation=_is_current_generation,
        )
        logger.info("HAPI Discord Connector 已初始化，SSE 输出级别: %s", output_level)

    async def terminate(self):
        """插件销毁：停止 SSE、关闭 client"""
        registry = _process_registry()
        if registry.get("sse_listener") is self.sse_listener:
            registry["sse_listener"] = None
        if registry.get("plugin") is self:
            registry["plugin"] = None
        if int(registry.get("generation") or 0) == int(
            getattr(self, "_generation_id", 0)
        ):
            registry["generation"] = int(registry.get("generation") or 0) + 1
        await self.sse_listener.stop()
        await self.client.close()
        logger.info("HAPI Discord Connector 已销毁")

    # ──── Discord UI 入口 ────

    @filter.command("dhapi")
    async def handle_hapi(self, event: AstrMessageEvent):
        """打开 HAPI Discord 原生交互面板。"""
        if not self._is_discord_event(event):
            return

        event.call_llm = True
        webhook = getattr(event, "interaction_followup_webhook", None)
        if webhook is None:
            logger.info(
                "忽略非 slash /dhapi 文本调用：仅支持 Discord slash command UI。"
            )
            event.stop_event()
            return

        from .discord_ui import DhapiMainView

        if not self._is_admin(event):
            await webhook.send(
                content="⚠️ 只有 AstrBot 管理员可以打开 DHAPI 面板。",
                wait=True,
                ephemeral=True,
            )
            event.stop_event()
            return

        await self.state_mgr.ensure_primary_session(event)
        await self.state_mgr.set_user_state(event)
        logger.info(
            "[dhapi] UI panel open umo=%s session_id=%s sender=%s followup=%s",
            event.unified_msg_origin,
            event.session_id,
            event.get_sender_id(),
            webhook is not None,
        )
        await self._refresh_sessions()

        view = DhapiMainView(self, event)
        embed = DhapiMainView.panel_embed(self, event)
        message = await webhook.send(
            embeds=[embed],
            view=view,
            wait=True,
            ephemeral=True,
        )
        logger.info(
            "DHAPI Discord UI panel sent: message_id=%s user=%s",
            getattr(message, "id", None),
            event.get_sender_id(),
        )
        event.stop_event()
