"""LLM 审批卡可读性回归测试。"""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dhapi_formatters", ROOT / "formatters.py")
FORMATTERS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FORMATTERS)


def test_send_message_approval_keeps_markdown_body():
    message = "# 继续完成\n\n- 构建 JAR\n- 发布 Release"

    detail = FORMATTERS.format_llm_approval_arguments(
        "dhapi_coding_send_message", {"message": message}
    )

    assert detail == f"**任务内容**\n{message}"
    assert "{'message'" not in detail


def test_create_session_approval_uses_readable_labels():
    detail = FORMATTERS.format_llm_approval_arguments(
        "dhapi_coding_create_session",
        {
            "machine_id": "cd4296da-c58f-4185-8d2c-d82f2fc3e286",
            "directory": "/root/workspace/handholding-forge-1.20.1",
            "agent": "codex",
            "session_type": "simple",
            "yolo": False,
            "model": "gpt-5.6-terra",
            "model_reasoning_effort": "max",
        },
    )

    assert "模型：`gpt-5.6-terra`" in detail
    assert "推理强度：`max`" in detail
    assert "会话模式：普通会话" in detail
    assert "自动批准：否（逐次审批）" in detail
    assert "machine_id=" not in detail
