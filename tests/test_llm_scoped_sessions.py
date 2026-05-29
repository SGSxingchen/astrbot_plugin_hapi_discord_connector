import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    sys.modules.setdefault("astrbot.api.provider", types.ModuleType("astrbot.api.provider"))

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

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


def _load_fakepkg_modules():
    _install_astrbot_stubs()
    pkg_name = "fakepkg_llm"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT)]
    sys.modules[pkg_name] = pkg

    for name in [
        "binding_manager",
        "state_manager",
        "formatters",
        "hapi_client",
        "session_ops",
        "llm_integration",
    ]:
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{name}", ROOT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{name}"] = module
        spec.loader.exec_module(module)

    return (
        sys.modules[f"{pkg_name}.binding_manager"],
        sys.modules[f"{pkg_name}.state_manager"],
        sys.modules[f"{pkg_name}.session_ops"],
        sys.modules[f"{pkg_name}.llm_integration"],
    )


binding_mod, state_mod, session_ops_mod, llm_mod = _load_fakepkg_modules()
BindingManager = binding_mod.BindingManager
StateManager = state_mod.StateManager
LLMIntegration = llm_mod.LLMIntegration


OLD_SID = "old00000-1111-2222-3333-444444444444"
NEW_SID = "new00000-aaaa-bbbb-cccc-dddddddddddd"


def _session(sid, *, flavor="codex", path="/repo", summary="session"):
    return {
        "id": sid,
        "active": True,
        "thinking": False,
        "pendingRequestsCount": 0,
        "metadata": {
            "flavor": flavor,
            "path": path,
            "summary": {"text": summary},
        },
    }


class KV:
    def __init__(self):
        self.data = {}

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        self.data[key] = value


class Event:
    unified_msg_origin = "umo-A"

    def get_sender_id(self):
        return "user-1"


class Client:
    def __init__(self):
        self.posts = []

    async def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        raise AssertionError("client.post should not be called for rejected targets")


class Plugin:
    def __init__(self, sessions):
        self.client = Client()
        self.sessions_cache = list(sessions)
        self.binding_mgr = BindingManager()
        self.state_mgr = StateManager(KV(), self.binding_mgr)
        self.pending_mgr = types.SimpleNamespace()
        self.refresh_calls = 0

    async def _refresh_sessions(self):
        self.refresh_calls += 1


def make_integration(sessions=None):
    plugin = Plugin(sessions or [_session(OLD_SID), _session(NEW_SID)])
    integ = LLMIntegration(plugin)

    async def approve(*args, **kwargs):
        return True, "approved"

    integ._require_approval = approve
    return plugin, integ, Event()


@pytest.mark.asyncio
async def test_send_rejects_old_full_uuid_when_current_window_joined_new_only():
    plugin, integ, event = make_integration()
    await plugin.state_mgr.join_session(NEW_SID, event.unified_msg_origin, "codex")

    result = await integ.tool_send_message(event, "hello", session_id=OLD_SID)

    assert "未加入当前 Discord 窗口" in result
    assert plugin.client.posts == []


@pytest.mark.asyncio
async def test_joined_prefix_resolves_but_global_non_joined_prefix_is_rejected(monkeypatch):
    plugin, integ, event = make_integration()
    await plugin.state_mgr.join_session(NEW_SID, event.unified_msg_origin, "codex")
    sent = []

    async def fake_send_message(client, sid, text, attachments=None):
        sent.append((sid, text))
        return True, f"sent -> [{sid[:8]}]"

    monkeypatch.setattr(session_ops_mod, "send_message", fake_send_message)

    ok_result = await integ.tool_send_message(event, "hello", session_id="new000")
    rejected_result = await integ.tool_send_message(event, "hello", session_id="old000")

    assert "sent -> [new00000]" == ok_result
    assert sent == [(NEW_SID, "hello")]
    assert "未加入当前 Discord 窗口" in rejected_result


@pytest.mark.asyncio
async def test_create_refreshes_joins_and_returns_omit_session_id_guidance(monkeypatch):
    created_sid = "crt00000-aaaa-bbbb-cccc-dddddddddddd"
    plugin, integ, event = make_integration([])

    async def fake_fetch_machines(client):
        return [{"id": "machine-1", "active": True, "metadata": {"host": "host"}}]

    async def fake_spawn_session(*args, **kwargs):
        return True, f"创建成功! Session ID: {created_sid}", created_sid

    monkeypatch.setattr(session_ops_mod, "fetch_machines", fake_fetch_machines)
    monkeypatch.setattr(session_ops_mod, "spawn_session", fake_spawn_session)

    result = await integ.tool_create_session(event, "/repo", "codex")

    assert plugin.refresh_calls == 1
    assert plugin.binding_mgr.get_window_sessions(event.unified_msg_origin) == [created_sid]
    assert any(s.get("id") == created_sid for s in plugin.sessions_cache)
    assert "请省略 session_id" in result
    assert "旧完整 UUID" in result
    assert created_sid not in result


@pytest.mark.asyncio
async def test_leave_archive_delete_reject_non_joined_targets(monkeypatch):
    plugin, integ, event = make_integration()
    await plugin.state_mgr.join_session(NEW_SID, event.unified_msg_origin, "codex")

    async def fail_approval(*args, **kwargs):
        raise AssertionError("approval should not be requested for non-joined target")

    async def fail_op(*args, **kwargs):
        raise AssertionError("session operation should not run for non-joined target")

    integ._require_approval = fail_approval
    monkeypatch.setattr(session_ops_mod, "archive_session", fail_op)
    monkeypatch.setattr(session_ops_mod, "delete_session", fail_op)

    leave_result = await integ.tool_leave_session(event, session_id=OLD_SID)
    archive_result = await integ.tool_archive_session(event, session_id=OLD_SID)
    delete_result = await integ.tool_delete_session(event, session_id=OLD_SID)

    assert "未加入当前 Discord 窗口" in leave_result
    assert "未加入当前 Discord 窗口" in archive_result
    assert "未加入当前 Discord 窗口" in delete_result
    assert plugin.binding_mgr.get_window_sessions(event.unified_msg_origin) == [NEW_SID]
