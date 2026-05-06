import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    class AstrMessageEvent:
        pass

    sys.modules["astrbot.api"].logger = _Logger()
    sys.modules["astrbot.api.event"].AstrMessageEvent = AstrMessageEvent


def _load_fakepkg_modules():
    _install_astrbot_stubs()
    pkg = types.ModuleType("fakepkg")
    pkg.__path__ = [str(ROOT)]
    sys.modules["fakepkg"] = pkg

    for name in ["binding_manager", "state_manager"]:
        spec = importlib.util.spec_from_file_location(
            f"fakepkg.{name}", ROOT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"fakepkg.{name}"] = module
        spec.loader.exec_module(module)

    return sys.modules["fakepkg.binding_manager"], sys.modules["fakepkg.state_manager"]


binding_mod, state_mod = _load_fakepkg_modules()
BindingManager = binding_mod.BindingManager
StateManager = state_mod.StateManager


class KV:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    async def get_kv_data(self, key, default=None):
        return self.data.get(key, default)

    async def put_kv_data(self, key, value):
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value


class Event:
    unified_msg_origin = "umo-A"

    def get_sender_id(self):
        return "user-1"


def test_binding_manager_many_to_many_join_leave_idempotent():
    mgr = BindingManager()

    mgr.join("sid-1", "umo-A", "codex")
    mgr.join("sid-1", "umo-A", "codex")
    mgr.join("sid-1", "umo-B", "codex")
    mgr.join("sid-2", "umo-A", "gemini")

    assert mgr.get_owners("sid-1") == ["umo-A", "umo-B"]
    assert mgr.get_window_sessions("umo-A") == ["sid-1", "sid-2"]
    assert mgr.get_session_flavor("sid-2") == "gemini"

    mgr.leave("sid-1", "umo-A")
    mgr.leave("sid-1", "umo-A")
    assert mgr.get_owners("sid-1") == ["umo-B"]
    assert mgr.get_window_sessions("umo-A") == ["sid-2"]

    released = mgr.leave_session("sid-1")
    assert released == ["umo-B"]
    assert mgr.get_owners("sid-1") == []
    assert mgr.get_window_sessions("umo-B") == []


def test_resolve_target_sid_no_session_single_ambiguous():
    mgr = BindingManager()
    sm = StateManager(KV(), mgr)
    event = Event()

    assert sm.resolve_target_sid(event, "explicit") == ("explicit", "ok")
    assert sm.resolve_target_sid(event, "") == (None, "no_session")

    mgr.join("sid-1", "umo-A", "codex")
    assert sm.resolve_target_sid(event, None) == ("sid-1", "ok")

    mgr.join("sid-2", "umo-A", "gemini")
    assert sm.resolve_target_sid(event, "") == (None, "ambiguous")


def test_load_legacy_session_owners_str_and_list_upgrades_to_join_model():
    kv = KV(
        {
            "dhapi_session_owners": {
                "sid-str": "umo-A",
                "sid-list": ["umo-B", "umo-C"],
            },
            "dhapi_primary_windows": [],
            "dhapi_flavor_primary_windows": {},
            "dhapi_known_users": [],
        }
    )
    mgr = BindingManager()
    sm = StateManager(kv, mgr)

    asyncio.run(sm.load_all())

    assert mgr.get_owners("sid-str") == ["umo-A"]
    assert mgr.get_owners("sid-list") == ["umo-B", "umo-C"]
    assert mgr.get_window_sessions("umo-C") == ["sid-list"]
