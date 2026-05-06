"""Discord-native UI for HAPI Discord Connector."""

from __future__ import annotations

import asyncio
from typing import Any

import discord

from astrbot.api import logger

from . import formatters, session_ops
from .formatters import is_compact_request

BRAND = 0x5865F2
OK = 0x2ECC71
WARN = 0xF1C40F
ERR = 0xE74C3C
PAGE_SIZE = 25


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def _session_title(session: dict) -> str:
    meta = session.get("metadata") or {}
    summary = (meta.get("summary") or {}).get("text", "") or "(无标题)"
    flavor = meta.get("flavor", "?")
    sid = session.get("id", "?")[:8]
    return _clip(f"{flavor} · {summary} · {sid}", 100)


def _session_desc(session: dict) -> str:
    meta = session.get("metadata") or {}
    path = meta.get("path", "") or "(无路径)"
    status = (
        "思考中"
        if session.get("thinking")
        else "运行中"
        if session.get("active")
        else "已关闭"
    )
    pending = session.get("pendingRequestsCount", 0)
    extra = f" · {pending} 待审批" if pending else ""
    return _clip(f"{status}{extra} · {path}", 100)


def make_embed(title: str, description: str = "", color: int = BRAND) -> discord.Embed:
    return discord.Embed(
        title=_clip(title, 256), description=_clip(description, 3900), color=color
    )


def _parse_bool(text: str) -> bool:
    value = text.strip().lower()
    return value in {"1", "true", "yes", "y", "on", "是", "开启"}


class DhapiBaseView(discord.ui.View):
    def __init__(self, plugin, event, *, timeout: float | None = 600):
        super().__init__(timeout=timeout)
        self.plugin = plugin
        self.event = event
        self.umo = event.unified_msg_origin
        self.owner_id = str(event.get_sender_id())

    def is_admin_id(self, user_id: int | str) -> bool:
        cfg = self.plugin.context.get_config(self.umo)
        return str(user_id) in [str(x) for x in cfg.get("admins_id", [])]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.is_admin_id(interaction.user.id):
            await interaction.response.send_message(
                "⚠️ 只有 AstrBot 管理员可以操作 DHAPI 面板。", ephemeral=True
            )
            return False
        return True

    async def on_error(
        self, error: Exception, item, interaction: discord.Interaction
    ) -> None:
        logger.error("DHAPI Discord UI callback failed: %s", error, exc_info=True)
        content = f"⚠️ 操作失败：{type(error).__name__}: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(content=content[:1900], ephemeral=True)
        else:
            await interaction.response.send_message(
                content=content[:1900], ephemeral=True
            )

    async def edit(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
    ):
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def refresh_sessions(self):
        await asyncio.wait_for(self.plugin._refresh_sessions(), timeout=15)

    def current_sid(self) -> str | None:
        sessions = self.plugin.binding_mgr.get_window_sessions(self.umo)
        return sessions[0] if len(sessions) == 1 else None

    def joined_sids(self) -> list[str]:
        return self.plugin.binding_mgr.get_window_sessions(self.umo)

    def session_by_id(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        return next((s for s in self.plugin.sessions_cache if s.get("id") == sid), None)

    def visible_sessions(self) -> list[dict]:
        visible = self.plugin.state_mgr.visible_sessions_for_window(
            self.event, self.plugin.sessions_cache
        )
        return visible or self.plugin.sessions_cache

    def visible_pending_items(self) -> list[tuple[str, str, dict]]:
        visible_sids = {
            s.get("id")
            for s in self.plugin.state_mgr.visible_sessions_for_window(
                self.event, self.plugin.sessions_cache
            )
            if s.get("id")
        }
        visible_sids.add(self.umo)
        return self.plugin.pending_mgr.flatten_pending(self.event, visible_sids)


AGENT_OPTIONS = ("claude", "codex", "gemini", "opencode")
SESSION_TYPE_OPTIONS = ("simple", "worktree")
REASONING_EFFORT_OPTIONS = ("none", "minimal", "low", "medium", "high", "xhigh")


def _machine_id(machine: dict) -> str:
    return str(machine.get("id") or machine.get("machineId") or "")


def _machine_label(machine: dict) -> str:
    name = machine.get("name") or machine.get("hostname") or machine.get("host")
    mid = _machine_id(machine)
    return _clip(f"{name or 'machine'} · {mid[:8]}", 100)


def _recent_paths(plugin) -> list[str]:
    paths = ["/root"]
    for session in plugin.sessions_cache:
        path = (session.get("metadata") or {}).get("path", "")
        if path and path not in paths:
            paths.append(path)
    return paths[:25]


class CreateMachineSelect(discord.ui.Select):
    def __init__(self, machines: list[dict], selected: str | None):
        options = []
        for machine in machines[:25]:
            mid = _machine_id(machine)
            if not mid:
                continue
            options.append(
                discord.SelectOption(
                    label=_machine_label(machine),
                    value=mid,
                    description=_clip(
                        str(machine.get("status") or "在线 machine"), 100
                    ),
                    default=mid == selected,
                )
            )
        if not options:
            options = [discord.SelectOption(label="没有在线 machine", value="__none__")]
        super().__init__(
            placeholder="选择 machine",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="dhapi:create:machine",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: CreateSessionView = self.view  # type: ignore[assignment]
        if self.values[0] != "__none__":
            view.machine_id = self.values[0]
        await view.replace(interaction)


class CreateAgentSelect(discord.ui.Select):
    def __init__(self, selected: str):
        super().__init__(
            placeholder="选择 agent",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=agent, value=agent, default=agent == selected
                )
                for agent in AGENT_OPTIONS
            ],
            custom_id="dhapi:create:agent",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: CreateSessionView = self.view  # type: ignore[assignment]
        view.agent = self.values[0]
        if view.agent != "codex":
            view.reasoning_effort = "none"
        await view.replace(interaction)


class CreateSessionTypeSelect(discord.ui.Select):
    def __init__(self, selected: str):
        super().__init__(
            placeholder="选择 session_type",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=typ, value=typ, default=typ == selected)
                for typ in SESSION_TYPE_OPTIONS
            ],
            custom_id="dhapi:create:type",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        view: CreateSessionView = self.view  # type: ignore[assignment]
        view.session_type = self.values[0]
        await view.replace(interaction)


class CreateReasoningSelect(discord.ui.Select):
    def __init__(self, selected: str, disabled: bool):
        super().__init__(
            placeholder="Codex reasoning_effort（非 Codex 不适用）",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=effort,
                    value=effort,
                    default=(effort == selected if not disabled else effort == "none"),
                )
                for effort in REASONING_EFFORT_OPTIONS
            ],
            custom_id="dhapi:create:reasoning",
            row=3,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        view: CreateSessionView = self.view  # type: ignore[assignment]
        view.reasoning_effort = self.values[0]
        await view.replace(interaction)


class CreateDirectoryModal(discord.ui.Modal):
    def __init__(self, source: CreateSessionView):
        super().__init__(title="填写工作目录", custom_id="dhapi:create:directory")
        self.source = source
        self.directory = discord.ui.InputText(
            label="directory",
            value=source.directory or "/root",
            required=True,
            max_length=500,
        )
        self.add_item(self.directory)

    async def callback(self, interaction: discord.Interaction):
        if not self.source.is_admin_id(interaction.user.id):
            await interaction.response.send_message(
                "⚠️ 只有 AstrBot 管理员可以创建 Session。", ephemeral=True
            )
            return
        self.source.directory = str(self.directory.value or "").strip() or "/root"
        await interaction.response.defer(ephemeral=True)
        view = ConfirmCreateView.from_create_view(self.source)
        await interaction.edit_original_response(embed=view.build_embed(), view=view)


class CreateSessionView(DhapiBaseView):
    def __init__(
        self,
        plugin,
        event,
        machines: list[dict],
        *,
        machine_id: str | None = None,
        agent: str = "codex",
        session_type: str = "simple",
        yolo: bool = False,
        reasoning_effort: str = "none",
        directory: str = "/root",
    ):
        super().__init__(plugin, event)
        self.machines = machines
        first_mid = _machine_id(machines[0]) if machines else None
        self.machine_id = machine_id or first_mid
        self.agent = agent if agent in AGENT_OPTIONS else "codex"
        self.session_type = (
            session_type if session_type in SESSION_TYPE_OPTIONS else "simple"
        )
        self.yolo = yolo
        self.reasoning_effort = reasoning_effort or "none"
        self.directory = directory or "/root"
        self._add_controls()

    async def replace(self, interaction: discord.Interaction):
        view = CreateSessionView(
            self.plugin,
            self.event,
            self.machines,
            machine_id=self.machine_id,
            agent=self.agent,
            session_type=self.session_type,
            yolo=self.yolo,
            reasoning_effort=self.reasoning_effort,
            directory=self.directory,
        )
        await self.edit(interaction, view.build_embed(), view)

    def _add_controls(self):
        self.add_item(CreateMachineSelect(self.machines, self.machine_id))
        self.add_item(CreateAgentSelect(self.agent))
        self.add_item(CreateSessionTypeSelect(self.session_type))
        self.add_item(
            CreateReasoningSelect(self.reasoning_effort, self.agent != "codex")
        )
        for item in self.children:
            custom_id = getattr(item, "custom_id", "")
            if custom_id == "dhapi:create:yolo":
                item.label = "Yolo：开" if self.yolo else "Yolo：关"
                item.style = (
                    discord.ButtonStyle.danger
                    if self.yolo
                    else discord.ButtonStyle.secondary
                )
            elif custom_id == "dhapi:create:confirm":
                item.disabled = not bool(self.machine_id and self.directory)

    def build_embed(self, note: str = "") -> discord.Embed:
        reasoning = self.reasoning_effort if self.agent == "codex" else "不适用"
        lines = [
            "按顺序选择创建参数；路径使用默认值或点按钮手填。",
            f"Machine：`{_clip(self.machine_id or '未选择', 120)}`",
            f"Agent：`{self.agent}`",
            f"Session Type：`{self.session_type}`",
            f"Yolo：`{self.yolo}`",
            f"Reasoning：`{reasoning}`",
            f"Directory：`{_clip(self.directory, 300)}`",
        ]
        if note:
            lines.append(f"\n{note}")
        return make_embed("创建 Session", "\n".join(lines), BRAND)

    @discord.ui.button(
        label="Yolo：关",
        style=discord.ButtonStyle.secondary,
        emoji="🛡️",
        custom_id="dhapi:create:yolo",
        row=4,
    )
    async def yolo_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        self.yolo = not self.yolo
        button.label = "Yolo：开" if self.yolo else "Yolo：关"
        button.style = (
            discord.ButtonStyle.danger if self.yolo else discord.ButtonStyle.secondary
        )
        await self.replace(interaction)

    @discord.ui.button(
        label="手填路径并确认",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="dhapi:create:directory_button",
        row=4,
    )
    async def directory_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await interaction.response.send_modal(CreateDirectoryModal(self))

    @discord.ui.button(
        label="确认创建",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="dhapi:create:confirm",
        row=4,
    )
    async def confirm_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = ConfirmCreateView.from_create_view(self)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="刷新 Machine",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="dhapi:create:refresh",
        row=4,
    )
    async def refresh_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        try:
            machines = await session_ops.fetch_machines(self.plugin.client)
        except Exception as exc:
            logger.warning("DHAPI create session fetch machines failed: %s", exc)
            await self.edit(
                interaction,
                self.build_embed(f"❌ 获取在线机器失败：{type(exc).__name__}: {exc}"),
                self,
            )
            return
        view = CreateSessionView(
            self.plugin,
            self.event,
            machines,
            machine_id=self.machine_id,
            agent=self.agent,
            session_type=self.session_type,
            yolo=self.yolo,
            reasoning_effort=self.reasoning_effort,
            directory=self.directory,
        )
        await self.edit(interaction, view.build_embed("已刷新 machine 列表。"), view)

    @discord.ui.button(
        label="取消",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:create:cancel",
        row=4,
    )
    async def cancel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = DhapiMainView(self.plugin, self.event)
        await self.edit(
            interaction, DhapiMainView.panel_embed(self.plugin, self.event), view
        )


class ConfirmCreateView(DhapiBaseView):
    def __init__(
        self,
        plugin,
        event,
        *,
        machines: list[dict],
        machine_id: str,
        agent: str,
        session_type: str,
        yolo: bool,
        reasoning_effort: str,
        directory: str,
    ):
        super().__init__(plugin, event)
        self.machines = machines
        self.machine_id = machine_id
        self.agent = agent
        self.session_type = session_type
        self.yolo = yolo
        self.reasoning_effort = reasoning_effort
        self.directory = directory

    @classmethod
    def from_create_view(cls, view: CreateSessionView) -> ConfirmCreateView:
        return cls(
            view.plugin,
            view.event,
            machines=view.machines,
            machine_id=view.machine_id or "",
            agent=view.agent,
            session_type=view.session_type,
            yolo=view.yolo,
            reasoning_effort=view.reasoning_effort,
            directory=view.directory,
        )

    def build_embed(self, note: str = "") -> discord.Embed:
        reasoning = self.reasoning_effort if self.agent == "codex" else "不适用"
        lines = [
            "请确认创建参数：",
            f"Machine：`{_clip(self.machine_id, 120)}`",
            f"Agent：`{self.agent}`",
            f"Session Type：`{self.session_type}`",
            f"Yolo：`{self.yolo}`",
            f"Reasoning：`{reasoning}`",
            f"Directory：`{_clip(self.directory, 500)}`",
        ]
        if note:
            lines.append(f"\n{note}")
        return make_embed("确认创建 Session", "\n".join(lines), WARN)

    @discord.ui.button(
        label="确认创建",
        style=discord.ButtonStyle.success,
        emoji="🚀",
        custom_id="dhapi:create:do_create",
    )
    async def confirm_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        if not self.machine_id:
            await self.edit(interaction, self.build_embed("❌ 未选择 machine。"), self)
            return
        reasoning = self.reasoning_effort if self.agent == "codex" else None
        if reasoning == "none":
            reasoning = None
        ok, msg, sid = await session_ops.spawn_session(
            self.plugin.client,
            self.machine_id,
            self.directory,
            self.agent,
            session_type=self.session_type,
            yolo=self.yolo,
            model_reasoning_effort=reasoning,
        )
        if not ok or not sid:
            await self.edit(interaction, self.build_embed(f"❌ {msg}"), self)
            return

        await self.refresh_sessions()
        session = self.session_by_id(sid)
        flavor = ((session or {}).get("metadata") or {}).get("flavor") or self.agent
        await self.plugin.state_mgr.join_session(sid, self.umo, flavor)
        await self.plugin.state_mgr.set_user_state(self.event)
        logger.info(
            "[dhapi] UI create joined sid=%s umo=%s flavor=%s event_session=%s",
            sid[:8],
            self.umo,
            flavor,
            self.event.session_id,
        )
        view = SessionActionView(self.plugin, self.event, sid)
        await self.edit(interaction, view.build_embed(f"✅ {msg}"), view)

    @discord.ui.button(
        label="返回修改",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:create:back_edit",
    )
    async def back_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = CreateSessionView(
            self.plugin,
            self.event,
            self.machines,
            machine_id=self.machine_id,
            agent=self.agent,
            session_type=self.session_type,
            yolo=self.yolo,
            reasoning_effort=self.reasoning_effort,
            directory=self.directory,
        )
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="取消",
        style=discord.ButtonStyle.secondary,
        emoji="✖️",
        custom_id="dhapi:create:abort",
    )
    async def cancel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = DhapiMainView(self.plugin, self.event)
        await self.edit(
            interaction, DhapiMainView.panel_embed(self.plugin, self.event), view
        )


class SendMessageModal(discord.ui.Modal):
    def __init__(self, source: DhapiBaseView, sid: str):
        super().__init__(title="发送消息到 Session", custom_id="dhapi:send:modal")
        self.source = source
        self.sid = sid
        self.message = discord.ui.InputText(
            label="message",
            style=discord.InputTextStyle.long,
            required=True,
            max_length=1900,
        )
        self.add_item(self.message)

    async def callback(self, interaction: discord.Interaction):
        if not self.source.is_admin_id(interaction.user.id):
            await interaction.response.send_message(
                "⚠️ 只有 AstrBot 管理员可以发送消息。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        text = str(self.message.value or "").strip()
        if not text:
            await interaction.followup.send(content="❌ 消息为空。", ephemeral=True)
            return
        ok, msg = await session_ops.send_message(
            self.source.plugin.client, self.sid, text
        )
        if ok:
            await self.source.refresh_sessions()
        view = SessionActionView(self.source.plugin, self.source.event, self.sid)
        note = ("✅ 已发送。" if ok else "❌ 发送失败。") + f"\n`{_clip(msg, 300)}`"
        await interaction.edit_original_response(
            embed=view.build_embed(note), view=view
        )


class DhapiMainView(DhapiBaseView):
    @staticmethod
    def panel_embed(plugin, event) -> discord.Embed:
        joined = plugin.binding_mgr.get_window_sessions(event.unified_msg_origin)
        if len(joined) == 1:
            session = next((s for s in plugin.sessions_cache if s.get("id") == joined[0]), None)
            joined_text = _session_title(session) if session else joined[0][:8]
        elif joined:
            joined_text = f"{len(joined)} 个（操作时需显式选择/传 session_id）"
        else:
            joined_text = "未加入"
        pending_count = len(plugin.pending_mgr.flatten_pending(None, None))
        desc = f"本窗口已加入 Session：`{joined_text}`\n待审批：`{pending_count}`\n\n使用下方按钮操作 HAPI。"
        return make_embed("HAPI Discord 控制台", desc, BRAND)

    @discord.ui.button(
        label="创建 Session",
        style=discord.ButtonStyle.success,
        emoji="➕",
        custom_id="dhapi:main:create",
        row=0,
    )
    async def create_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        try:
            machines = await session_ops.fetch_machines(self.plugin.client)
        except Exception as exc:
            logger.warning("DHAPI create session fetch machines failed: %s", exc)
            await self.edit(
                interaction,
                make_embed(
                    "创建 Session",
                    f"❌ 获取在线机器失败：{type(exc).__name__}: {exc}",
                    ERR,
                ),
                self,
            )
            return
        view = CreateSessionView(self.plugin, self.event, machines)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="列表",
        style=discord.ButtonStyle.primary,
        emoji="📋",
        custom_id="dhapi:main:list",
        row=0,
    )
    async def list_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.refresh_sessions()
        view = SessionListView(self.plugin, self.event)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="当前状态",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="dhapi:main:status",
        row=0,
    )
    async def status_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        joined = self.joined_sids()
        if len(joined) != 1:
            await self.edit(
                interaction,
                make_embed(
                    "当前状态",
                    "本窗口未加入 session 或已加入多个 session。请进入「列表」打开具体 session。",
                    WARN,
                ),
                self,
            )
            return
        await self.refresh_sessions()
        await show_session_status(self, interaction, joined[0], back="main")

    @discord.ui.button(
        label="审批",
        style=discord.ButtonStyle.secondary,
        emoji="✅",
        custom_id="dhapi:main:approval",
        row=0,
    )
    async def approval_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.refresh_sessions()
        view = ApprovalView(self.plugin, self.event)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="配置",
        style=discord.ButtonStyle.secondary,
        emoji="⚙️",
        custom_id="dhapi:main:config",
        row=1,
    )
    async def config_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = ConfigView(self.plugin, self.event)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="关闭",
        style=discord.ButtonStyle.danger,
        emoji="✖️",
        custom_id="dhapi:main:close",
        row=1,
    )
    async def close_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.edit(
            interaction,
            make_embed(
                "DHAPI 面板已关闭", "重新使用 `/dhapi` 可打开新面板。", 0x95A5A6
            ),
            None,
        )


class SessionSelect(discord.ui.Select):
    def __init__(self, sessions: list[dict], current_sid: str | None, page: int):
        options = []
        start = page * PAGE_SIZE
        for session in sessions[start : start + PAGE_SIZE]:
            sid = session.get("id", "")
            options.append(
                discord.SelectOption(
                    label=_session_title(session),
                    value=sid,
                    description=_session_desc(session),
                    default=False,
                )
            )
        if not options:
            options = [
                discord.SelectOption(
                    label="没有可用 session", value="__none__", description="请稍后刷新"
                )
            ]
        super().__init__(
            placeholder="选择一个 session",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="dhapi:sessions:select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: SessionListView = self.view  # type: ignore[assignment]
        sid = self.values[0]
        if sid == "__none__":
            await interaction.response.defer()
            return
        action_view = SessionActionView(view.plugin, view.event, sid)
        await view.edit(interaction, action_view.build_embed(), action_view)


class SessionListView(DhapiBaseView):
    def __init__(self, plugin, event, page: int = 0):
        super().__init__(plugin, event)
        sessions = self.plugin.sessions_cache
        max_page = max(0, (len(sessions) - 1) // PAGE_SIZE)
        self.page = min(max(page, 0), max_page)
        self.add_item(SessionSelect(sessions, self.current_sid(), self.page))
        self._sync_buttons(max_page)

    def _sync_buttons(self, max_page: int):
        for item in self.children:
            custom_id = getattr(item, "custom_id", "")
            if custom_id == "dhapi:sessions:open_current":
                item.disabled = len(self.joined_sids()) != 1
            elif custom_id == "dhapi:sessions:prev":
                item.disabled = self.page <= 0
            elif custom_id == "dhapi:sessions:next":
                item.disabled = self.page >= max_page

    def build_embed(self, note: str = "") -> discord.Embed:
        sessions = self.plugin.sessions_cache
        visible_count = len(self.visible_sessions())
        max_page = max(0, (len(sessions) - 1) // PAGE_SIZE)
        lines = [
            f"全部 Session：`{len(sessions)}` 个",
            f"当前窗口可见：`{visible_count}` 个",
            f"页码：`{self.page + 1}/{max_page + 1}`（每页最多 25 个）",
        ]
        joined = self.joined_sids()
        if joined:
            preview = ", ".join(sid[:8] for sid in joined[:5])
            suffix = "…" if len(joined) > 5 else ""
            lines.append(f"本窗口已加入：`{preview}{suffix}`")
        if not sessions:
            lines.append("暂无 session。")
        return make_embed("Session 列表", "\n".join(lines), BRAND)

    @discord.ui.button(
        label="打开已加入",
        style=discord.ButtonStyle.primary,
        emoji="🎯",
        custom_id="dhapi:sessions:open_current",
        row=1,
    )
    async def open_current_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        joined = self.joined_sids()
        if len(joined) != 1:
            await self.edit(
                interaction,
                self.build_embed(
                    "本窗口未加入 session 或已加入多个 session。请先从下拉框打开一个 session。"
                ),
                self,
            )
            return
        view = SessionActionView(self.plugin, self.event, joined[0])
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="上一页",
        style=discord.ButtonStyle.secondary,
        emoji="◀️",
        custom_id="dhapi:sessions:prev",
        row=1,
    )
    async def prev_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = SessionListView(self.plugin, self.event, self.page - 1)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="下一页",
        style=discord.ButtonStyle.secondary,
        emoji="▶️",
        custom_id="dhapi:sessions:next",
        row=1,
    )
    async def next_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = SessionListView(self.plugin, self.event, self.page + 1)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="刷新",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="dhapi:sessions:refresh",
        row=1,
    )
    async def refresh_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.refresh_sessions()
        view = SessionListView(self.plugin, self.event, self.page)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="返回",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:sessions:back",
        row=1,
    )
    async def back_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = DhapiMainView(self.plugin, self.event)
        await self.edit(
            interaction, DhapiMainView.panel_embed(self.plugin, self.event), view
        )


class SessionActionView(DhapiBaseView):
    def __init__(self, plugin, event, sid: str):
        super().__init__(plugin, event)
        self.sid = sid
        self._sync_join_button()

    def _is_joined(self) -> bool:
        return self.sid in self.plugin.binding_mgr.get_window_sessions(self.umo)

    def _sync_join_button(self):
        joined = self._is_joined()
        for item in self.children:
            if getattr(item, "custom_id", "") == "dhapi:session:join_leave":
                item.label = "从此窗口退出" if joined else "在此窗口加入"
                item.style = (
                    discord.ButtonStyle.secondary
                    if joined
                    else discord.ButtonStyle.success
                )
                item.emoji = "➖" if joined else "✅"

    def build_embed(self, note: str = "") -> discord.Embed:
        session = self.session_by_id(self.sid)
        if not session:
            return make_embed("Session", f"未找到 session：`{self.sid[:8]}`", ERR)
        meta = session.get("metadata") or {}
        desc = [
            f"`{self.sid}`",
            f"**{_session_title(session)}**",
            _session_desc(session),
        ]
        owners = self.plugin.binding_mgr.get_owners(self.sid)
        if owners:
            if len(owners) <= 2:
                owner_text = ", ".join(_clip(owner, 40) for owner in owners)
            else:
                owner_text = ", ".join(_clip(owner, 30) for owner in owners[:2]) + f" +{len(owners) - 2}"
            desc.append(f"订阅窗口：{owner_text}")
        else:
            desc.append("订阅窗口：0")
        if meta.get("path"):
            desc.append(f"路径：`{_clip(meta.get('path'), 500)}`")
        if note:
            desc.append(f"\n{note}")
        return make_embed("Session 操作", "\n".join(desc), BRAND)

    @discord.ui.button(
        label="在此窗口加入",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="dhapi:session:join_leave",
        row=0,
    )
    async def join_leave_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        if self._is_joined():
            await self.plugin.state_mgr.leave_session(self.sid, self.umo)
            await self.plugin.state_mgr.set_user_state(self.event)
            logger.info(
                "[dhapi] UI leave sid=%s umo=%s event_session=%s",
                self.sid[:8],
                self.umo,
                self.event.session_id,
            )
            self._sync_join_button()
            await self.edit(
                interaction,
                self.build_embed(f"已从当前窗口退出 `{self.sid[:8]}`。"),
                self,
            )
            return

        session = self.session_by_id(self.sid)
        flavor = ((session or {}).get("metadata") or {}).get("flavor", "claude")
        await self.plugin.state_mgr.join_session(self.sid, self.umo, flavor)
        await self.plugin.state_mgr.set_user_state(self.event)
        logger.info(
            "[dhapi] UI join sid=%s umo=%s flavor=%s event_session=%s",
            self.sid[:8],
            self.umo,
            flavor,
            self.event.session_id,
        )
        self._sync_join_button()
        await self.edit(
            interaction,
            self.build_embed(
                f"已在当前窗口加入 `{self.sid[:8]}`；通知会投递到所有已加入窗口。"
            ),
            self,
        )

    @discord.ui.button(
        label="发送消息",
        style=discord.ButtonStyle.primary,
        emoji="💬",
        custom_id="dhapi:session:send",
        row=0,
    )
    async def send_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await interaction.response.send_modal(SendMessageModal(self, self.sid))

    @discord.ui.button(
        label="查看状态",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="dhapi:session:status",
        row=0,
    )
    async def status_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await show_session_status(self, interaction, self.sid, back="session")

    @discord.ui.button(
        label="刷新",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="dhapi:session:refresh",
        row=1,
    )
    async def refresh_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.refresh_sessions()
        await self.edit(interaction, self.build_embed("已刷新。"), self)

    @discord.ui.button(
        label="停止",
        style=discord.ButtonStyle.danger,
        emoji="⏹️",
        custom_id="dhapi:session:stop",
        row=1,
    )
    async def stop_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        ok, msg = await session_ops.abort_session(self.plugin.client, self.sid)
        if ok:
            await self.refresh_sessions()
        await self.edit(
            interaction, self.build_embed(("✅ " if ok else "❌ ") + msg), self
        )

    @discord.ui.button(
        label="归档",
        style=discord.ButtonStyle.secondary,
        emoji="🗄️",
        custom_id="dhapi:session:archive",
        row=1,
    )
    async def archive_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = ConfirmArchiveView(self.plugin, self.event, self.sid)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="删除",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="dhapi:session:delete",
        row=2,
    )
    async def delete_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = ConfirmDeleteView(self.plugin, self.event, self.sid)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="返回",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:session:back",
        row=2,
    )
    async def back_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = SessionListView(self.plugin, self.event)
        await self.edit(interaction, view.build_embed(), view)


class ConfirmArchiveView(DhapiBaseView):
    def __init__(self, plugin, event, sid: str):
        super().__init__(plugin, event)
        self.sid = sid

    def build_embed(self) -> discord.Embed:
        session = self.session_by_id(self.sid)
        title = _session_title(session) if session else self.sid[:8]
        desc = (
            f"确认归档 Session：**{_clip(title, 120)}**\n"
            f"`{self.sid}`\n\n"
            "归档通常会让该 session 从常用列表中隐藏。请确认不是误点。"
        )
        return make_embed("确认归档", desc, WARN)

    @discord.ui.button(
        label="确认归档",
        style=discord.ButtonStyle.danger,
        emoji="🗄️",
        custom_id="dhapi:archive:confirm",
    )
    async def confirm_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        if not hasattr(session_ops, "archive_session"):
            # 需要 HAPI 后端提供归档接口后才能真正实现。
            view = SessionActionView(self.plugin, self.event, self.sid)
            await self.edit(
                interaction,
                view.build_embed("当前 HAPI API 暂未发现归档接口。"),
                view,
            )
            return
        ok, msg = await session_ops.archive_session(self.plugin.client, self.sid)
        await self.refresh_sessions()
        view = (
            SessionListView(self.plugin, self.event)
            if ok
            else SessionActionView(self.plugin, self.event, self.sid)
        )
        await self.edit(
            interaction,
            view.build_embed(("✅ " if ok else "❌ ") + msg),
            view,
        )

    @discord.ui.button(
        label="取消",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:archive:cancel",
    )
    async def cancel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = SessionActionView(self.plugin, self.event, self.sid)
        await self.edit(interaction, view.build_embed("已取消归档。"), view)


class ConfirmDeleteView(DhapiBaseView):
    def __init__(self, plugin, event, sid: str):
        super().__init__(plugin, event)
        self.sid = sid

    def build_embed(self) -> discord.Embed:
        session = self.session_by_id(self.sid)
        title = _session_title(session) if session else self.sid[:8]
        desc = (
            f"确认删除 Session：**{_clip(title, 120)}**\n"
            f"`{self.sid}`\n\n"
            "删除是危险操作，可能不可恢复。不会用 stop 伪装 delete。"
        )
        return make_embed("确认删除", desc, ERR)

    @discord.ui.button(
        label="确认删除",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="dhapi:delete:confirm",
    )
    async def confirm_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        if not hasattr(session_ops, "delete_session"):
            # 需要 HAPI 后端提供删除接口后才能真正实现；不要把 stop 当 delete。
            view = SessionActionView(self.plugin, self.event, self.sid)
            await self.edit(
                interaction,
                view.build_embed("当前 HAPI API 暂未发现删除接口。"),
                view,
            )
            return
        ok, msg = await session_ops.delete_session(self.plugin.client, self.sid)
        if ok:
            await self.plugin.state_mgr.leave_all_session_owners(self.sid)
        await self.refresh_sessions()
        view = (
            SessionListView(self.plugin, self.event)
            if ok
            else SessionActionView(self.plugin, self.event, self.sid)
        )
        await self.edit(
            interaction,
            view.build_embed(("✅ " if ok else "❌ ") + msg),
            view,
        )

    @discord.ui.button(
        label="取消",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:delete:cancel",
    )
    async def cancel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = SessionActionView(self.plugin, self.event, self.sid)
        await self.edit(interaction, view.build_embed("已取消删除。"), view)


class StatusView(DhapiBaseView):
    def __init__(self, plugin, event, back: str = "main", sid: str | None = None):
        super().__init__(plugin, event)
        self.back = back
        self.sid = sid

    @discord.ui.button(
        label="返回",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:status:back",
    )
    async def back_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        if self.back == "session" and self.sid:
            view = SessionActionView(self.plugin, self.event, self.sid)
            await self.edit(interaction, view.build_embed(), view)
        else:
            view = DhapiMainView(self.plugin, self.event)
            await self.edit(
                interaction, DhapiMainView.panel_embed(self.plugin, self.event), view
            )


async def show_session_status(
    view: DhapiBaseView, interaction: discord.Interaction, sid: str, back: str
):
    detail = await session_ops.fetch_session_detail(view.plugin.client, sid)
    text = formatters.format_session_status(detail)
    embed = make_embed("当前状态", f"```text\n{_clip(text, 1800)}\n```", BRAND)
    await view.edit(
        interaction, embed, StatusView(view.plugin, view.event, back=back, sid=sid)
    )


def _pending_label(item: tuple[str, str, dict], sessions_cache: list[dict]) -> str:
    sid, _rid, req = item
    tool = req.get("tool") or req.get("type") or "request"
    idx = req.get("index", "?")
    return _clip(f"#{idx} {tool} · {sid[:8]}", 100)


def _pending_desc(req: dict) -> str:
    args = req.get("arguments") or {}
    if isinstance(args, dict):
        raw = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
    else:
        raw = str(args)
    return _clip(raw or "待处理", 100)


class PendingSelect(discord.ui.Select):
    def __init__(self, items: list[tuple[str, str, dict]], sessions_cache: list[dict]):
        options = []
        for i, item in enumerate(items[:25]):
            _sid, _rid, req = item
            options.append(
                discord.SelectOption(
                    label=_pending_label(item, sessions_cache),
                    value=str(i),
                    description=_pending_desc(req),
                )
            )
        if not options:
            options = [
                discord.SelectOption(
                    label="没有待审批请求", value="__none__", description="请稍后刷新"
                )
            ]
        super().__init__(
            placeholder="选择待审批请求",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="dhapi:pending:select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: ApprovalView = self.view  # type: ignore[assignment]
        value = self.values[0]
        if value == "__none__":
            await interaction.response.defer()
            return
        view.selected_index = int(value)
        await view.edit(interaction, view.build_embed(), view)


class ApprovalView(DhapiBaseView):
    def __init__(self, plugin, event, selected_index: int = 0):
        super().__init__(plugin, event)
        self.items = self.visible_pending_items()
        self.selected_index = min(max(selected_index, 0), max(0, len(self.items) - 1))
        self.add_item(PendingSelect(self.items, self.plugin.sessions_cache))

    def selected_item(self) -> tuple[str, str, dict] | None:
        if not self.items or self.selected_index >= len(self.items):
            return None
        return self.items[self.selected_index]

    def build_embed(self, note: str = "") -> discord.Embed:
        item = self.selected_item()
        if not item:
            desc = "没有待审批请求。"
        else:
            sid, rid, req = item
            desc_lines = [
                f"共 `{len(self.items)}` 个待审批。",
                f"当前：**{_pending_label(item, self.plugin.sessions_cache)}**",
                f"Session：`{sid}`",
                f"Request ID：`{rid}`",
            ]
            args = req.get("arguments") or {}
            desc_lines.append(f"参数：```text\n{_clip(args, 1200)}\n```")
            desc = "\n".join(desc_lines)
        if note:
            desc += f"\n\n{note}"
        return make_embed("审批", desc, WARN if item else BRAND)

    async def _approve_item(self, sid: str, rid: str, req: dict) -> tuple[bool, str]:
        if is_compact_request(req):
            ok, _ = await session_ops.send_message(self.plugin.client, sid, "/compact")
            if ok:
                self.plugin.pending_mgr.remove_entry(sid, rid)
            return ok, "/compact"
        if self.plugin.pending_mgr.is_llm_tool_request(req):
            original_req = self.plugin.sse_listener.pending.get(sid, {}).get(rid, {})
            future = original_req.get("future")
            if future and not future.done():
                future.set_result(True)
            self.plugin.pending_mgr.remove_entry(sid, rid)
            return True, req.get("tool", "LLM tool")
        ok, msg = await session_ops.approve_permission(self.plugin.client, sid, rid)
        if ok:
            self.plugin.pending_mgr.remove_entry(sid, rid)
        return ok, msg

    async def _deny_item(self, sid: str, rid: str, req: dict) -> tuple[bool, str]:
        if is_compact_request(req):
            self.plugin.pending_mgr.remove_entry(sid, rid)
            if hasattr(self.plugin.sse_listener, "mark_compact_denied"):
                self.plugin.sse_listener.mark_compact_denied(sid, 60)
            return True, "/compact 已取消"
        if self.plugin.pending_mgr.is_llm_tool_request(req):
            original_req = self.plugin.sse_listener.pending.get(sid, {}).get(rid, {})
            future = original_req.get("future")
            if future and not future.done():
                future.set_result(False)
            self.plugin.pending_mgr.remove_entry(sid, rid)
            return True, req.get("tool", "LLM tool")
        ok, msg = await session_ops.deny_permission(self.plugin.client, sid, rid)
        if ok:
            self.plugin.pending_mgr.remove_entry(sid, rid)
        return ok, msg

    @discord.ui.button(
        label="批准",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="dhapi:approval:approve",
        row=1,
    )
    async def approve_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        item = self.selected_item()
        if not item:
            await self.edit(interaction, self.build_embed("没有可批准的请求。"), self)
            return
        ok, msg = await self._approve_item(*item)
        view = ApprovalView(self.plugin, self.event)
        await self.edit(
            interaction,
            view.build_embed(("✅ 已批准：" if ok else "❌ 批准失败：") + msg),
            view,
        )

    @discord.ui.button(
        label="拒绝",
        style=discord.ButtonStyle.danger,
        emoji="🚫",
        custom_id="dhapi:approval:deny",
        row=1,
    )
    async def deny_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        item = self.selected_item()
        if not item:
            await self.edit(interaction, self.build_embed("没有可拒绝的请求。"), self)
            return
        ok, msg = await self._deny_item(*item)
        view = ApprovalView(self.plugin, self.event)
        await self.edit(
            interaction,
            view.build_embed(("✅ 已拒绝：" if ok else "❌ 拒绝失败：") + msg),
            view,
        )

    @discord.ui.button(
        label="批准全部",
        style=discord.ButtonStyle.success,
        emoji="☑️",
        custom_id="dhapi:approval:approve_all",
        row=1,
    )
    async def approve_all_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        candidates = [
            item
            for item in list(self.items)
            if not formatters.is_question_request(item[2])
        ]
        if not candidates:
            await self.edit(
                interaction,
                self.build_embed("没有可批量批准的请求（question 请求需逐项处理）。"),
                self,
            )
            return
        results = []
        for item in candidates:
            ok, msg = await self._approve_item(*item)
            results.append((ok, msg))
        success = sum(1 for ok, _ in results if ok)
        failed = len(results) - success
        note = f"✅ 已批准 {success} 项"
        if failed:
            note += f"，❌ 失败 {failed} 项"
        view = ApprovalView(self.plugin, self.event)
        await self.edit(interaction, view.build_embed(note), view)

    @discord.ui.button(
        label="刷新",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="dhapi:approval:refresh",
        row=1,
    )
    async def refresh_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.refresh_sessions()
        view = ApprovalView(self.plugin, self.event)
        await self.edit(interaction, view.build_embed(), view)

    @discord.ui.button(
        label="返回",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:approval:back",
        row=2,
    )
    async def back_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = DhapiMainView(self.plugin, self.event)
        await self.edit(
            interaction, DhapiMainView.panel_embed(self.plugin, self.event), view
        )


class ApprovalNoticeView(DhapiBaseView):
    """Lightweight approval buttons attached to a single approval notification."""

    def __init__(self, plugin, event, sid: str, rid: str):
        super().__init__(plugin, event)
        self.sid = sid
        self.rid = rid

    def _current_item(self) -> tuple[str, str, dict] | None:
        req = self.plugin.sse_listener.pending.get(self.sid, {}).get(self.rid)
        if not req:
            return None
        return self.sid, self.rid, req

    async def _edit_panel(self, interaction: discord.Interaction, note: str = ""):
        await self.refresh_sessions()
        view = ApprovalView(self.plugin, self.event)
        await self.edit(interaction, view.build_embed(note), view)

    async def _approve_current(self) -> tuple[bool, str]:
        item = self._current_item()
        if not item:
            return False, "请求已处理或不存在。"
        return await ApprovalView._approve_item(self, *item)

    async def _deny_current(self) -> tuple[bool, str]:
        item = self._current_item()
        if not item:
            return False, "请求已处理或不存在。"
        return await ApprovalView._deny_item(self, *item)

    @discord.ui.button(
        label="批准",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="dhapi:notice:approve",
    )
    async def approve_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        ok, msg = await self._approve_current()
        await self._edit_panel(
            interaction, ("✅ 已批准：" if ok else "❌ 批准失败：") + msg
        )

    @discord.ui.button(
        label="拒绝",
        style=discord.ButtonStyle.danger,
        emoji="🚫",
        custom_id="dhapi:notice:deny",
    )
    async def deny_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        ok, msg = await self._deny_current()
        await self._edit_panel(
            interaction, ("✅ 已拒绝：" if ok else "❌ 拒绝失败：") + msg
        )

    @discord.ui.button(
        label="打开/刷新审批面板",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="dhapi:notice:panel",
    )
    async def panel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self._edit_panel(interaction)


class ConfigView(DhapiBaseView):
    def __init__(self, plugin, event):
        super().__init__(plugin, event)
        self._sync_button_labels()

    def _sync_button_labels(self):
        auto_on = bool(self.plugin.config.get("auto_approve_enabled", False))
        remind_on = bool(self.plugin.config.get("remind_pending", True))
        for item in self.children:
            custom_id = getattr(item, "custom_id", "")
            if custom_id == "dhapi:config:auto_approve":
                item.label = "自动审批：开" if auto_on else "自动审批：关"
                item.style = (
                    discord.ButtonStyle.danger
                    if auto_on
                    else discord.ButtonStyle.secondary
                )
            elif custom_id == "dhapi:config:remind":
                item.label = "提醒：开" if remind_on else "提醒：关"
                item.style = (
                    discord.ButtonStyle.primary
                    if remind_on
                    else discord.ButtonStyle.secondary
                )

    def build_embed(self, note: str = "") -> discord.Embed:
        cfg = self.plugin.config
        joined = self.joined_sids()
        current = (
            joined[0]
            if len(joined) == 1
            else "未加入"
            if not joined
            else f"{len(joined)} 个"
        )
        primary = self.plugin.state_mgr.primary_umo(self.event) or "未设置"
        flavor_umos = self.plugin.state_mgr.flavor_primary_umos(self.event)
        session_owner = "未设置"
        if len(joined) == 1:
            owners = self.plugin.binding_mgr.get_owners(joined[0])
            if owners:
                session_owner = f"{len(owners)} 个窗口"
        pending_count = len(self.plugin.pending_mgr.flatten_pending(None, None))
        owners_count = len(self.plugin.binding_mgr._session_owners)
        sse_status = "休眠" if self.plugin.sse_listener._hibernated else "运行中"
        if self.plugin.sse_listener.conn_fail_count:
            sse_status += f" / 失败 {self.plugin.sse_listener.conn_fail_count} 次"

        lines = [
            f"HAPI Endpoint：`{_clip(cfg.get('hapi_endpoint', ''), 120)}`",
            f"Proxy：`{_clip(cfg.get('proxy_url', '') or '未设置', 120)}`",
            f"Output Level：`{cfg.get('output_level', 'simple')}`",
            f"Embed：`{cfg.get('embed_enabled', True)}`",
            f"待审批提醒：`{cfg.get('remind_pending', True)}` / `{cfg.get('remind_interval', 180)}s`",
            f"自动审批：`{cfg.get('auto_approve_enabled', False)}`（开启后 24 小时生效，持久化到插件配置）",
            f"SSE：`{sse_status}`",
            f"本窗口已加入 Session：`{_clip(current, 120)}`",
            f"该 Session 订阅窗口：`{_clip(session_owner, 120)}`",
            f"主通知窗口：`{_clip(primary, 120)}`",
            f"Flavor 通知窗口：`{_clip(flavor_umos or '未设置', 500)}`",
            f"Session 绑定数：`{owners_count}`",
            f"待审批数：`{pending_count}`",
        ]
        return make_embed("DHAPI 配置（只读）", "\n".join(lines), BRAND)

    @discord.ui.button(
        label="自动审批：关",
        style=discord.ButtonStyle.secondary,
        emoji="🤖",
        custom_id="dhapi:config:auto_approve",
    )
    async def auto_approve_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        new_value = not bool(self.plugin.config.get("auto_approve_enabled", False))
        self.plugin.config["auto_approve_enabled"] = new_value
        self.plugin.config.save_config()
        self.plugin.sse_listener._auto_approve_enabled = new_value
        view = ConfigView(self.plugin, self.event)
        await self.edit(
            interaction,
            view.build_embed(f"自动审批已{'开启' if new_value else '关闭'}。"),
            view,
        )

    @discord.ui.button(
        label="提醒：开",
        style=discord.ButtonStyle.primary,
        emoji="🔔",
        custom_id="dhapi:config:remind",
    )
    async def remind_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        new_value = not bool(self.plugin.config.get("remind_pending", True))
        self.plugin.config["remind_pending"] = new_value
        self.plugin.config.save_config()
        self.plugin.sse_listener._remind_enabled = new_value
        view = ConfigView(self.plugin, self.event)
        await self.edit(
            interaction,
            view.build_embed(f"待审批提醒已{'开启' if new_value else '关闭'}。"),
            view,
        )

    @discord.ui.button(
        label="设为主通知窗口",
        style=discord.ButtonStyle.success,
        emoji="📌",
        custom_id="dhapi:config:set_primary",
    )
    async def set_primary_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.plugin.state_mgr.set_primary_window(self.event, self.umo)
        view = ConfigView(self.plugin, self.event)
        await self.edit(
            interaction, view.build_embed("已将当前窗口设为主通知窗口。"), view
        )

    @discord.ui.button(
        label="刷新",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="dhapi:config:refresh",
    )
    async def refresh_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        await self.refresh_sessions()
        await self.edit(interaction, self.build_embed(), self)

    @discord.ui.button(
        label="返回",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="dhapi:config:back",
    )
    async def back_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        view = DhapiMainView(self.plugin, self.event)
        await self.edit(
            interaction, DhapiMainView.panel_embed(self.plugin, self.event), view
        )
