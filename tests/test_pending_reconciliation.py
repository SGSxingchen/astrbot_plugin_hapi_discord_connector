"""待审批权威对账、提醒节流与 Embed 去重回归测试。"""

import asyncio
import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "fakepkg_pending_reconciliation"


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _module(name: str, *, package: bool = False) -> types.ModuleType:
    module = sys.modules.setdefault(name, types.ModuleType(name))
    if package:
        module.__path__ = []
    return module


def _install_runtime_stubs():
    _module("astrbot", package=True)
    api = _module("astrbot.api", package=True)
    event = _module("astrbot.api.event", package=True)
    components = _module("astrbot.api.message_components", package=True)
    _module("astrbot.core", package=True)
    _module("astrbot.core.platform", package=True)
    _module("astrbot.core.platform.sources", package=True)
    _module("astrbot.core.platform.sources.discord", package=True)
    discord_components = _module(
        "astrbot.core.platform.sources.discord.components", package=True
    )

    class MessageChain(list):
        def message(self, text):
            self.append(types.SimpleNamespace(text=text))
            return self

    class BaseMessageComponent:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class DiscordEmbed(BaseMessageComponent):
        pass

    api.logger = _Logger()
    event.MessageChain = MessageChain
    components.BaseMessageComponent = BaseMessageComponent
    discord_components.DiscordEmbed = DiscordEmbed

    discord = _module("discord")

    class Embed:
        def __init__(self, **kwargs):
            self.title = kwargs.get("title")
            self.description = kwargs.get("description")
            self.color = kwargs.get("color")
            self.fields = []

        def add_field(self, **kwargs):
            self.fields.append(kwargs)

        def set_thumbnail(self, **kwargs):
            self.thumbnail = kwargs.get("url")

        def set_image(self, **kwargs):
            self.image = kwargs.get("url")

        def set_footer(self, **kwargs):
            self.footer = kwargs.get("text")

    class View:
        pass

    discord.Embed = Embed
    discord.ui = types.SimpleNamespace(View=View)


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.{name}", ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE_NAME}.{name}"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_pending_modules():
    _install_runtime_stubs()
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package

    hapi_client = types.ModuleType(f"{PACKAGE_NAME}.hapi_client")

    class AsyncHapiClient:
        pass

    class ContentTypeError(Exception):
        content_type = ""

    hapi_client.AsyncHapiClient = AsyncHapiClient
    hapi_client.ContentTypeError = ContentTypeError
    sys.modules[f"{PACKAGE_NAME}.hapi_client"] = hapi_client

    session_ops = types.ModuleType(f"{PACKAGE_NAME}.session_ops")

    async def fetch_session_detail(client, sid):
        raise AssertionError(f"测试未配置 session 详情: {sid}")

    async def fetch_messages(client, sid, limit=10):
        return []

    session_ops.fetch_session_detail = fetch_session_detail
    session_ops.fetch_messages = fetch_messages
    sys.modules[f"{PACKAGE_NAME}.session_ops"] = session_ops

    formatters = _load_module("formatters")
    sse_listener = _load_module("sse_listener")
    notification_manager = _load_module("notification_manager")
    return sse_listener, notification_manager, session_ops, formatters


SSE_MODULE, NOTIFICATION_MODULE, SESSION_OPS, _ = _load_pending_modules()
SSEListener = SSE_MODULE.SSEListener
NotificationManager = NOTIFICATION_MODULE.NotificationManager


def _session(sid: str, *, active: bool = True, pending_count: int = 0) -> dict:
    return {
        "id": sid,
        "active": active,
        "thinking": False,
        "pendingRequestsCount": pending_count,
        "metadata": {
            "flavor": "codex",
            "path": "/workspace",
            "summary": {"text": "审批测试会话"},
        },
    }


def _request(index: int = 1) -> dict:
    return {
        "tool": "CodexBash",
        "arguments": {"command": "echo test"},
        "index": index,
        "_dhapi_pending_source": "hub",
    }


def _detail(sid: str, requests: dict, *, active: bool = True) -> dict:
    return {
        "id": sid,
        "active": active,
        "agentState": {"requests": copy.deepcopy(requests)},
    }


def _listener(sessions=None, notifications=None, client=None):
    sent = notifications if notifications is not None else []

    async def notify(text, sid):
        sent.append((text, sid))

    listener = SSEListener(client or object(), sessions or [], notify)
    listener._stopped = False
    return listener, sent


@pytest.mark.asyncio
async def test_empty_pending_queue_never_sends_reminder():
    listener, sent = _listener()
    listener._remind_interval = 0

    await listener._remind_once()

    assert sent == []
    assert listener.pending == {}


@pytest.mark.asyncio
async def test_hub_empty_snapshot_clears_cached_pending_without_reminder(monkeypatch):
    sid = "session-hub-empty"
    listener, sent = _listener([_session(sid, pending_count=1)])
    listener.pending = {sid: {"request-old": _request(7)}}
    listener._remind_interval = 0

    async def fetch_detail(client, fetched_sid):
        assert fetched_sid == sid
        return _detail(sid, {})

    monkeypatch.setattr(SESSION_OPS, "fetch_session_detail", fetch_detail)

    await listener._remind_once()

    assert listener.pending == {}
    assert 7 in listener._free_indices
    assert sent == []


@pytest.mark.asyncio
async def test_same_session_and_request_id_is_reconciled_once():
    sid = "session-deduplicate"
    listener, _ = _listener([_session(sid)])

    first = await listener._replace_hub_pending(sid, {"request-1": _request()})
    first_index = listener.pending[sid]["request-1"]["index"]
    second = await listener._replace_hub_pending(sid, {"request-1": _request()})

    assert [rid for rid, _ in first] == ["request-1"]
    assert second == []
    assert list(listener.pending[sid]) == ["request-1"]
    assert listener.pending[sid]["request-1"]["index"] == first_index


@pytest.mark.asyncio
async def test_terminal_and_not_found_requests_are_removed_from_hub_cache(monkeypatch):
    sid = "session-terminal"
    listener, _ = _listener([_session(sid, pending_count=1)])
    listener.pending = {
        sid: {
            "approved": _request(1),
            "denied": _request(2),
        }
    }

    await listener._remove_hub_pending(sid, "approved")
    await listener._remove_hub_pending(sid, "denied")

    assert sid not in listener.pending

    listener.pending = {sid: {"already-gone": _request(3)}}

    async def fetch_detail(client, fetched_sid):
        assert fetched_sid == sid
        raise RuntimeError("HTTP 404: request session no longer exists")

    monkeypatch.setattr(SESSION_OPS, "fetch_session_detail", fetch_detail)

    checked = await listener.reconcile_hub_pending({sid})

    assert checked == {sid}
    assert listener.pending == {}
    assert {1, 2, 3}.issubset(listener._free_indices)


@pytest.mark.asyncio
async def test_inactive_session_event_removes_hub_pending():
    sid = "session-inactive"
    sessions = [_session(sid, active=True, pending_count=1)]
    listener, sent = _listener(sessions)
    listener.pending = {sid: {"stale-request": _request(4)}}

    await listener._handle(
        {
            "type": "session-updated",
            "sessionId": sid,
            "data": {"active": False, "thinking": False},
        }
    )

    assert listener.pending == {}
    assert sessions[0]["active"] is False
    assert sent == []


class _BlockingSSEClient:
    def __init__(self):
        self.block = asyncio.Event()

    async def subscribe_events_raw(self, all_events=True):
        await self.block.wait()
        raise AssertionError("监听任务不应在停止前返回")


@pytest.mark.asyncio
async def test_repeated_start_and_stop_do_not_leave_duplicate_reminder_tasks():
    sid = "session-reload"
    client = _BlockingSSEClient()
    listener, sent = _listener([_session(sid)], client=client)
    listener._stopped = True

    listener.start(remind_pending=True, remind_interval=60)
    first_listen_task = listener._task
    await asyncio.sleep(0)
    listener.start(remind_pending=True, remind_interval=60)

    assert listener._task is first_listen_task

    await listener._handle(
        {
            "type": "session-updated",
            "sessionId": sid,
            "data": {
                "active": True,
                "thinking": True,
                "agentState": {"requests": {"request-1": _request()}},
            },
        }
    )
    first_reminder_task = listener._remind_task
    listener.start(remind_pending=True, remind_interval=60)

    assert first_reminder_task is not None
    assert listener._remind_task is first_reminder_task

    await listener.stop()

    assert first_listen_task is not None and first_listen_task.done()
    assert first_reminder_task.done()
    assert listener._task is None
    assert listener._remind_task is None
    assert sent == []


@pytest.mark.asyncio
async def test_unchanged_pending_set_is_throttled_but_new_request_reminds(monkeypatch):
    sid = "session-throttle"
    requests = {"request-1": _request()}
    listener, sent = _listener([_session(sid, pending_count=1)])
    listener._remind_interval = 0

    async def fetch_detail(client, fetched_sid):
        assert fetched_sid == sid
        return _detail(sid, requests)

    monkeypatch.setattr(SESSION_OPS, "fetch_session_detail", fetch_detail)

    await listener._remind_once()
    await listener._remind_once()
    requests["request-2"] = _request()
    await listener._remind_once()

    assert len(sent) == 2
    assert "当前全局共 1 个待审批请求" in sent[0][0]
    assert "当前全局共 2 个待审批请求" in sent[1][0]
    assert set(listener.pending[sid]) == {"request-1", "request-2"}


class _NotificationContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class _NotificationState:
    def __init__(self):
        self.kv = types.SimpleNamespace(config={"embed_enabled": True})

    @staticmethod
    def select_notification_targets(session_id, sessions_cache):
        return ["discord:channel:approval-test"]


@pytest.mark.asyncio
async def test_reminder_embed_does_not_repeat_body_in_details_field():
    sid = "session-embed"
    context = _NotificationContext()
    manager = NotificationManager(context, _NotificationState())
    reminder = (
        "⏰ 提醒：该会话仍有 1 个待审批请求\n"
        "🏷️ session-embed\n\n"
        "当前全局共 1 个待审批请求，请及时处理以避免会话缓存失效\n"
        "  打开 /dhapi → 审批 面板处理"
    )

    await manager.push_notification(reminder, sid, [_session(sid, pending_count=1)])

    assert len(context.sent) == 1
    _, chain = context.sent[0]
    embed = chain[0]
    assert embed.title == "待审批提醒"
    assert embed.description.count("该会话仍有 1 个待审批请求") == 1
    assert embed.description.count("当前全局共 1 个待审批请求") == 1
    detail_text = "\n".join(
        str(field.get("value", "")) for field in (getattr(embed, "fields", None) or [])
    )
    assert "该会话仍有 1 个待审批请求" not in detail_text
    assert "当前全局共 1 个待审批请求" not in detail_text
