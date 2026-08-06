"""受控 cron 后台 session_id 直连回归测试。"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SID = "9484eecd-cd21-4890-8bb6-f0c9eb0cf55a"
SID_TWO = "9484ffff-cd21-4890-8bb6-f0c9eb0cf55a"
UMO_ONE = "discord:GroupMessage:window-one"
UMO_TWO = "discord:GroupMessage:window-two"


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _install_llm_stubs():
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    sys.modules.setdefault(
        "astrbot.api.provider", types.ModuleType("astrbot.api.provider")
    )

    class AstrMessageEvent:
        pass

    class MessageChain(list):
        def message(self, text):
            self.append(types.SimpleNamespace(text=text))
            return self

    class ProviderRequest:
        pass

    sys.modules["astrbot.api"].logger = _Logger()
    sys.modules["astrbot.api.event"].AstrMessageEvent = AstrMessageEvent
    sys.modules["astrbot.api.event"].MessageChain = MessageChain
    sys.modules["astrbot.api.provider"].ProviderRequest = ProviderRequest


def _load_llm_modules():
    _install_llm_stubs()
    package_name = "fakepkg_background"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package

    for name in [
        "binding_manager",
        "state_manager",
        "formatters",
        "hapi_client",
        "session_ops",
        "approval_ops",
        "pending_manager",
        "llm_integration",
    ]:
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.{name}", ROOT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package_name}.{name}"] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)

    ui_module = types.ModuleType(f"{package_name}.discord_ui")

    class ApprovalNoticeView:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    ui_module.ApprovalNoticeView = ApprovalNoticeView
    sys.modules[f"{package_name}.discord_ui"] = ui_module
    return {
        name: sys.modules[f"{package_name}.{name}"]
        for name in (
            "binding_manager",
            "state_manager",
            "formatters",
            "session_ops",
            "pending_manager",
            "llm_integration",
        )
    }


MODULES = _load_llm_modules()
BindingManager = MODULES["binding_manager"].BindingManager
StateManager = MODULES["state_manager"].StateManager
PendingManager = MODULES["pending_manager"].PendingManager
LLMIntegration = MODULES["llm_integration"].LLMIntegration
FORMATTERS = MODULES["formatters"]
SESSION_OPS = MODULES["session_ops"]


def _session(session_id, permission_mode="default"):
    return {
        "id": session_id,
        "active": True,
        "permissionMode": permission_mode,
        "metadata": {
            "flavor": "codex",
            "path": "/workspace",
            "summary": {"text": "scheduled session"},
        },
    }


class _KV:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


class _SSE:
    def __init__(self, auto_approve=False):
        self.pending = {}
        self._auto_approve_enabled = auto_approve
        self._index = 0

    def allocate_index(self):
        self._index += 1
        return self._index

    def free_index(self, index):
        pass

    def get_all_pending(self):
        return self.pending


class _NotificationManager:
    def __init__(self, plugin, approve_on_send=True):
        self.plugin = plugin
        self.approve_on_send = approve_on_send
        self.calls = []

    async def push_embed(self, umo, title, description, color, **kwargs):
        self.calls.append(
            {
                "umo": umo,
                "title": title,
                "description": description,
                "color": color,
                **kwargs,
            }
        )
        if self.approve_on_send:
            requests = self.plugin.sse_listener.pending[umo]
            request_id, request = next(iter(requests.items()))
            request["future"].set_result(True)
            self.plugin.pending_mgr.remove_entry(umo, request_id)


class _Plugin:
    def __init__(self, sessions, config=None, auto_approve=False):
        self.client = object()
        self.sessions_cache = list(sessions)
        self.config = config or {}
        self.binding_mgr = BindingManager()
        self.state_mgr = StateManager(_KV(), self.binding_mgr)
        self.sse_listener = _SSE(auto_approve)
        self.pending_mgr = PendingManager(self.sse_listener)
        self.notification_mgr = _NotificationManager(self)
        self.refresh_count = 0

    async def _refresh_sessions(self):
        self.refresh_count += 1

    def _select_background_approval_umo(self, session_id):
        owners = self.binding_mgr.get_owners(session_id)
        return owners[0] if owners else None


async def _bind(plugin, session_id, *umos):
    for umo in umos:
        await plugin.state_mgr.join_session(session_id, umo, "codex")


@pytest.mark.asyncio
async def test_background_status_and_history_accept_explicit_bound_prefix(monkeypatch):
    plugin = _Plugin([_session(SID)])
    await _bind(plugin, SID, UMO_ONE)
    integration = LLMIntegration(plugin)
    called = []

    async def fake_detail(client, session_id):
        called.append(("detail", session_id))
        return _session(session_id)

    async def fake_messages(client, session_id, limit):
        called.append(("messages", session_id, limit))
        return [{"content": {"role": "assistant", "content": "done"}}]

    monkeypatch.setattr(SESSION_OPS, "fetch_session_detail", fake_detail)
    monkeypatch.setattr(SESSION_OPS, "fetch_messages", fake_messages)
    monkeypatch.setattr(FORMATTERS, "format_session_status", lambda detail: "status-ok")
    monkeypatch.setattr(FORMATTERS, "split_into_rounds", lambda messages: [messages])
    monkeypatch.setattr(
        FORMATTERS, "format_round", lambda messages, index, total: "history-ok"
    )

    assert (
        await integration.tool_get_status_background(object(), "9484eecd")
        == "status-ok"
    )
    assert (
        await integration.tool_message_history_background(object(), 1, SID)
        == "history-ok"
    )
    assert called == [("detail", SID), ("messages", SID, 80)]
    assert plugin.refresh_count == 2


@pytest.mark.asyncio
async def test_background_rejects_missing_id_ambiguous_prefix_and_unbound_session():
    plugin = _Plugin([_session(SID), _session(SID_TWO)])
    await _bind(plugin, SID, UMO_ONE)
    await _bind(plugin, SID_TWO, UMO_ONE)
    integration = LLMIntegration(plugin)

    missing = await integration.tool_get_status_background(object(), "")
    ambiguous = await integration.tool_get_status_background(object(), "9484")
    await plugin.state_mgr.leave_session(SID_TWO, UMO_ONE)
    unbound = await integration.tool_get_status_background(object(), SID_TWO)

    assert "必须显式提供 session_id" in missing
    assert "匹配到 2 个" in ambiguous
    assert "已绑定 Discord 窗口" in unbound


@pytest.mark.asyncio
async def test_background_yolo_send_is_direct_only_with_explicit_opt_in(monkeypatch):
    plugin = _Plugin(
        [_session(SID, permission_mode="yolo")],
        {"background_direct_allow_yolo_send": True},
    )
    await _bind(plugin, SID, UMO_ONE)
    integration = LLMIntegration(plugin)
    sent = []

    async def fake_detail(client, session_id):
        return _session(session_id, permission_mode="yolo")

    async def fake_send(client, session_id, text, attachments=None):
        sent.append((session_id, text))
        return True, "sent"

    monkeypatch.setattr(SESSION_OPS, "fetch_session_detail", fake_detail)
    monkeypatch.setattr(SESSION_OPS, "send_message", fake_send)

    assert (
        await integration.tool_send_message_background(object(), "continue", SID)
        == "sent"
    )
    assert sent == [(SID, "continue")]
    assert plugin.notification_mgr.calls == []


@pytest.mark.asyncio
async def test_background_non_yolo_send_routes_one_card_to_first_bound_window(
    monkeypatch,
):
    plugin = _Plugin([_session(SID)])
    await _bind(plugin, SID, UMO_ONE, UMO_TWO)
    integration = LLMIntegration(plugin)
    sent = []

    async def fake_detail(client, session_id):
        return _session(session_id, permission_mode="default")

    async def fake_send(client, session_id, text, attachments=None):
        sent.append((session_id, text))
        return True, "sent-after-approval"

    monkeypatch.setattr(SESSION_OPS, "fetch_session_detail", fake_detail)
    monkeypatch.setattr(SESSION_OPS, "send_message", fake_send)

    result = await integration.tool_send_message_background(
        object(), "continue", "9484eecd"
    )

    assert result == "sent-after-approval"
    assert sent == [(SID, "continue")]
    assert [call["umo"] for call in plugin.notification_mgr.calls] == [UMO_ONE]
    assert "授权后台任务" in plugin.notification_mgr.calls[0]["description"]
    assert plugin.notification_mgr.calls[0]["view"] is not None


@pytest.mark.asyncio
async def test_background_global_auto_approve_requires_its_own_opt_in(monkeypatch):
    plugin = _Plugin(
        [_session(SID)],
        {"background_direct_allow_global_auto_approve_send": True},
        auto_approve=True,
    )
    await _bind(plugin, SID, UMO_ONE)
    integration = LLMIntegration(plugin)
    sent = []

    async def fake_send(client, session_id, text, attachments=None):
        sent.append((session_id, text))
        return True, "sent"

    monkeypatch.setattr(SESSION_OPS, "send_message", fake_send)

    assert (
        await integration.tool_send_message_background(object(), "continue", SID)
        == "sent"
    )
    assert sent == [(SID, "continue")]
    assert plugin.notification_mgr.calls == []


class _CronEvent:
    def __init__(self, job_id):
        self.job_id = job_id

    def get_platform_name(self):
        return "cron"

    def get_extra(self, key, default=None):
        return {"id": self.job_id} if key == "cron_job" else default


def _install_main_stubs():
    _install_llm_stubs()
    sys.modules.setdefault("astrbot.api.star", types.ModuleType("astrbot.api.star"))

    class _Filter:
        @staticmethod
        def on_llm_request():
            return lambda func: func

        @staticmethod
        def llm_tool(**kwargs):
            return lambda func: func

        @staticmethod
        def command(*args, **kwargs):
            return lambda func: func

    class Star:
        def __init__(self, context=None):
            self.context = context

    sys.modules["astrbot.api"].AstrBotConfig = dict
    sys.modules["astrbot.api.event"].filter = _Filter()
    sys.modules["astrbot.api.star"].Context = object
    sys.modules["astrbot.api.star"].Star = Star
    sys.modules["astrbot.api.star"].register = lambda *args, **kwargs: lambda cls: cls
    sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
    sys.modules.setdefault("astrbot.core.cron", types.ModuleType("astrbot.core.cron"))
    cron_events = types.ModuleType("astrbot.core.cron.events")
    cron_events.CronMessageEvent = _CronEvent
    sys.modules["astrbot.core.cron.events"] = cron_events

    package_name = "fakepkg_main_background"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package

    modules = {
        "session_ops": {},
        "binding_manager": {"BindingManager": object},
        "cf_access": {"CfAccessManager": object},
        "agent_final_trigger": {
            "AgentFinalPayload": object,
            "AgentFinalTrigger": object,
        },
        "hapi_client": {"AsyncHapiClient": object},
        "notification_manager": {"NotificationManager": object},
        "pending_manager": {"PendingManager": object},
        "sse_listener": {"SSEListener": object},
        "state_manager": {"StateManager": object},
    }
    for name, values in modules.items():
        module = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(module, key, value)
        sys.modules[f"{package_name}.{name}"] = module

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package_name}.main"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.HapiDiscordConnectorPlugin


PluginMain = _install_main_stubs()


class _ToolIntegration:
    def __init__(self):
        self.restricted = 0
        self.removed = 0
        self.calls = []

    def restrict_to_background_direct_tools(self, request):
        self.restricted += 1

    def _remove_hapi_tools(self, request, keep_basic):
        self.removed += 1

    async def tool_get_status_background(self, event, session_id):
        self.calls.append((event, session_id))
        return "reachable"


@pytest.mark.asyncio
async def test_cron_whitelist_controls_schema_and_wrapper_reachability():
    plugin = PluginMain.__new__(PluginMain)
    plugin.config = {
        "background_direct_enabled": True,
        "background_allowed_cron_job_ids": ["job-allow"],
    }
    plugin.llm_integration = _ToolIntegration()
    plugin.agent_final_trigger = types.SimpleNamespace(
        remember_event=lambda event: None
    )

    allowed = _CronEvent("job-allow")
    denied = _CronEvent("job-denied")

    assert plugin._background_direct_authorized(allowed)
    assert not plugin._background_direct_authorized(denied)
    assert not plugin._background_direct_authorized(
        types.SimpleNamespace(
            get_platform_name=lambda: "cron",
            get_extra=lambda key, default=None: {"id": "job-allow"},
        )
    )
    plugin.config = {}
    assert not plugin._background_direct_authorized(allowed)
    plugin.config = {
        "background_direct_enabled": True,
        "background_allowed_cron_job_ids": ["job-allow"],
    }
    await plugin.on_llm_request_hook(allowed, object())
    await plugin.on_llm_request_hook(denied, object())
    assert plugin.llm_integration.restricted == 1
    assert plugin.llm_integration.removed == 1
    assert await plugin.tool_get_status(allowed, SID) == "reachable"
    assert "未获授权" in await plugin.tool_get_status(denied, SID)


@pytest.mark.asyncio
async def test_dangerous_background_operation_stays_discord_only():
    plugin = PluginMain.__new__(PluginMain)
    plugin.config = {
        "background_direct_enabled": True,
        "background_allowed_cron_job_ids": ["job-allow"],
    }
    plugin.llm_integration = types.SimpleNamespace()

    assert "仅支持 Discord" in await plugin.tool_archive_session(
        _CronEvent("job-allow"), SID
    )


def test_background_route_requires_a_live_discord_adapter_and_keeps_join_order():
    plugin = PluginMain.__new__(PluginMain)
    plugin.binding_mgr = types.SimpleNamespace(
        get_owners=lambda session_id: ["qq:GroupMessage:old", UMO_ONE, UMO_TWO]
    )

    class Platform:
        def __init__(self, platform_id, platform_name):
            self._meta = types.SimpleNamespace(id=platform_id, name=platform_name)

        def meta(self):
            return self._meta

    plugin.context = types.SimpleNamespace(
        platform_manager=types.SimpleNamespace(
            platform_insts=[
                Platform("qq", "aiocqhttp"),
                Platform("discord", "discord"),
            ]
        )
    )

    assert plugin._select_background_approval_umo(SID) == UMO_ONE

    plugin.context.platform_manager.platform_insts = [Platform("qq", "aiocqhttp")]
    assert plugin._select_background_approval_umo(SID) is None


@pytest.mark.asyncio
async def test_discord_wrapper_keeps_existing_path():
    class DiscordEvent:
        def get_platform_name(self):
            return "discord"

    plugin = PluginMain.__new__(PluginMain)
    plugin.config = {}
    integration = _ToolIntegration()

    async def normal_status(event, session_id):
        integration.calls.append((event, session_id))
        return "discord-path"

    integration.tool_get_status = normal_status
    plugin.llm_integration = integration

    assert await plugin.tool_get_status(DiscordEvent(), SID) == "discord-path"
    assert len(integration.calls) == 1
    assert integration.calls[0][1] == SID
