import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_astrbot_stubs():
    modules = [
        "astrbot",
        "astrbot.api",
        "astrbot.api.message_components",
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.platform.astr_message_event",
        "astrbot.core.platform.astrbot_message",
        "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.discord",
        "astrbot.core.platform.sources.discord.discord_platform_adapter",
        "astrbot.core.platform.sources.discord.discord_platform_event",
    ]
    for name in modules:
        sys.modules.setdefault(name, types.ModuleType(name))

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    class Plain:
        def __init__(self, text):
            self.text = text

    class MessageSession:
        @classmethod
        def from_str(cls, value):
            raise NotImplementedError

    class AstrBotMessage:
        pass

    class MessageMember:
        def __init__(self, user_id, nickname):
            self.user_id = user_id
            self.nickname = nickname

    class DiscordPlatformAdapter:
        pass

    class DiscordPlatformEvent:
        pass

    sys.modules["astrbot.api"].logger = _Logger()
    sys.modules["astrbot.api.message_components"].Plain = Plain
    sys.modules["astrbot.core.platform.astr_message_event"].MessageSession = MessageSession
    sys.modules["astrbot.core.platform.astrbot_message"].AstrBotMessage = AstrBotMessage
    sys.modules["astrbot.core.platform.astrbot_message"].MessageMember = MessageMember
    sys.modules[
        "astrbot.core.platform.sources.discord.discord_platform_adapter"
    ].DiscordPlatformAdapter = DiscordPlatformAdapter
    sys.modules[
        "astrbot.core.platform.sources.discord.discord_platform_event"
    ].DiscordPlatformEvent = DiscordPlatformEvent


_install_astrbot_stubs()
agent_final_trigger = importlib.import_module("agent_final_trigger")
AgentFinalPayload = agent_final_trigger.AgentFinalPayload
AgentFinalTrigger = agent_final_trigger.AgentFinalTrigger


class DummyPlugin:
    def __init__(self, config):
        self.config = config


def make_trigger(tmp_path, **config):
    defaults = {
        "max_content_chars": 12000,
        "agent_final_preview_chars": 20,
        "agent_final_file_ttl_days": 7,
        "agent_final_use_file_when_chars": 50,
    }
    defaults.update(config)
    trigger = AgentFinalTrigger(DummyPlugin(defaults))
    trigger._agent_final_dir = lambda: tmp_path / "dhapi_agent_final"
    return trigger


def payload(content, event_id="evt/1"):
    return AgentFinalPayload(
        session_id="abcdef1234567890",
        agent="codex",
        event_id=event_id,
        content=content,
    )


def test_short_final_uses_message_str_without_file(tmp_path):
    trigger = make_trigger(tmp_path, agent_final_use_file_when_chars=100)
    text, meta = trigger._prepare_final_message(payload("short final"))

    assert text == "short final"
    assert meta["dhapi_final_chars"] == len("short final")
    assert meta["dhapi_final_preview_chars"] == len("short final")
    assert "dhapi_final_file" not in meta
    assert not (tmp_path / "dhapi_agent_final").exists()


def test_long_final_writes_file_and_uses_preview(tmp_path):
    content = "first line\nsecond line is longer\n" + "x" * 80
    trigger = make_trigger(
        tmp_path,
        agent_final_use_file_when_chars=30,
        agent_final_preview_chars=24,
    )

    text, meta = trigger._prepare_final_message(payload(content, event_id="evt:long"))

    file_path = meta["dhapi_final_file"]
    assert file_path.endswith("abcdef12-evt-long.md")
    assert open(file_path, encoding="utf-8").read() == content
    assert meta["dhapi_final_chars"] == len(content)
    assert 1 <= meta["dhapi_final_preview_chars"] <= 26
    assert "完整内容已写入文件：" in text
    assert file_path in text
    assert content not in text


def test_ttl_cleanup_removes_old_files_on_prepare(tmp_path):
    trigger = make_trigger(
        tmp_path,
        agent_final_use_file_when_chars=10,
        agent_final_file_ttl_days=1,
    )
    final_dir = tmp_path / "dhapi_agent_final"
    final_dir.mkdir()
    old_file = final_dir / "old.md"
    old_file.write_text("old", encoding="utf-8")
    old_mtime = 1_000_000_000
    import os

    os.utime(old_file, (old_mtime, old_mtime))

    text, meta = trigger._prepare_final_message(payload("x" * 40, event_id="new"))

    assert not old_file.exists()
    assert open(meta["dhapi_final_file"], encoding="utf-8").read() == "x" * 40
    assert "完整内容已写入文件" in text


def test_final_metadata_is_appended_to_raw_message_and_extra(tmp_path):
    trigger = make_trigger(tmp_path)
    p = payload("hello")
    meta = {
        "dhapi_final_file": "/tmp/final.md",
        "dhapi_final_chars": 5,
        "dhapi_final_preview_chars": 5,
    }

    raw = trigger._raw_message(p, "discord:group:1", meta)
    assert raw["source"] == "dhapi_agent"
    assert raw["dhapi_final_file"] == "/tmp/final.md"

    class Event:
        def __init__(self):
            self.extras = {}

        def clear_result(self):
            pass

        def set_extra(self, key, value):
            self.extras[key] = value

    event = Event()
    trigger._refresh_event_flags(event, p, meta)
    assert event.extras["source"] == "dhapi_agent"
    assert event.extras["dhapi_final_chars"] == 5
