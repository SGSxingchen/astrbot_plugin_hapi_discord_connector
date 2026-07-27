"""新版 HAPI 会话控制接口回归测试。"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_session_ops():
    pkg = types.ModuleType("fakepkg")
    pkg.__path__ = [str(ROOT)]
    sys.modules["fakepkg"] = pkg
    client_module = types.ModuleType("fakepkg.hapi_client")

    class AsyncHapiClient:
        pass

    client_module.AsyncHapiClient = AsyncHapiClient
    sys.modules["fakepkg.hapi_client"] = client_module
    spec = importlib.util.spec_from_file_location(
        "fakepkg.session_ops", ROOT / "session_ops.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["fakepkg.session_ops"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SESSION_OPS = _load_session_ops()


class Response:
    def __init__(self, data=None, *, ok=True, status=200):
        self.data = data or {}
        self.ok = ok
        self.status = status
        self.released = False

    async def json(self):
        return self.data

    async def text(self):
        return "error"

    def release(self):
        self.released = True


class Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, path, json):
        self.calls.append((path, json))
        return self.response


def test_service_tier_and_reasoning_use_hapi_024_routes():
    client = Client(Response())

    assert asyncio.run(SESSION_OPS.set_service_tier(client, "sid", "fast"))[0]
    assert asyncio.run(
        SESSION_OPS.set_codex_reasoning_effort(client, "sid", "max")
    )[0]

    assert client.calls == [
        ("/api/sessions/sid/service-tier", {"serviceTier": "fast"}),
        (
            "/api/sessions/sid/model-reasoning-effort",
            {"modelReasoningEffort": "max"},
        ),
    ]


def test_reopen_reads_nested_session_id():
    client = Client(Response({"session": {"id": "new-session"}}))

    ok, _msg, sid = asyncio.run(SESSION_OPS.reopen_session(client, "old-session"))

    assert ok
    assert sid == "new-session"
    assert client.calls == [("/api/sessions/old-session/reopen", {})]


def test_spawn_passes_model_and_reasoning_as_separate_hapi_fields():
    client = Client(Response({"type": "success", "sessionId": "new-session"}))

    ok, _msg, sid = asyncio.run(
        SESSION_OPS.spawn_session(
            client,
            "machine-1",
            "/workspace",
            "codex",
            model="gpt-5.6-terra",
            model_reasoning_effort="max",
        )
    )

    assert ok
    assert sid == "new-session"
    assert client.calls == [
        (
            "/api/machines/machine-1/spawn",
            {
                "directory": "/workspace",
                "agent": "codex",
                "sessionType": "simple",
                "yolo": False,
                "modelReasoningEffort": "max",
                "model": "gpt-5.6-terra",
            },
        )
    ]
