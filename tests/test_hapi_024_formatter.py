"""HAPI 0.24+ 消息格式兼容回归测试。"""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dhapi_formatters", ROOT / "formatters.py")
FORMATTERS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FORMATTERS)


def _codex_content(event_type: str) -> dict:
    return {
        "content": {
            "role": "agent",
            "content": [
                {
                    "type": "codex",
                    "data": {"type": event_type, "id": "agent-run-1"},
                }
            ],
        }
    }


def test_hapi_subagent_internal_events_are_not_rendered():
    assert FORMATTERS.extract_text_preview(_codex_content("agent-run-trace")) is None
    assert FORMATTERS.extract_text_preview(_codex_content("agent-run-update")) is None


def test_hapi_subagent_internal_events_do_not_hide_real_reply():
    content = _codex_content("agent-run-trace")
    content["content"]["content"].append({"type": "text", "text": "交付已完成"})

    assert FORMATTERS.extract_text_preview(content) == "交付已完成"
