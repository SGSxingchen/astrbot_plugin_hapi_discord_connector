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


def test_hapi_html_reply_is_rendered_as_discord_markdown():
    content = {
        "content": {
            "role": "agent",
            "content": [
                {
                    "type": "text",
                    "text": """
                    <h2>v1.0.2 已发布完成</h2>
                    <ul>
                      <li>编译依赖：<code>net.minecraftforge:forge:1.20.1-47.4.6</code></li>
                      <li>构建：<strong>成功</strong></li>
                    </ul>
                    <h3>发布信息</h3>
                    <table>
                      <tr><td>仓库</td><td><a href="https://github.com/example/repo">example/repo</a></td></tr>
                      <tr><td>资产</td><td><code>mod.jar</code></td></tr>
                    </table>
                    """,
                }
            ],
        }
    }

    text = FORMATTERS.extract_text_preview(content, max_len=0)

    assert text is not None
    assert "<h2>" not in text
    assert "<li>" not in text
    assert "<table>" not in text
    assert "<a " not in text
    assert "## v1.0.2 已发布完成" in text
    assert "- 编译依赖：`net.minecraftforge:forge:1.20.1-47.4.6`" in text
    assert "构建：**成功**" in text
    assert "仓库 | [example/repo](https://github.com/example/repo)" in text
    assert "资产 | `mod.jar`" in text


def test_hapi_markdown_reply_is_not_changed():
    content = {
        "content": {
            "role": "agent",
            "content": [{"type": "text", "text": "## 完成\n\n- 保留 Markdown"}],
        }
    }

    assert FORMATTERS.extract_text_preview(content, max_len=0) == "## 完成\n\n- 保留 Markdown"
