"""Tests for FeishuCCApp — runtime helpers, command handling."""

from __future__ import annotations

import threading

import pytest

from feishu_cc.app import _BotRuntime, _find_runtime
from feishu_cc.claude_bridge import ClaudeBridge
from feishu_cc.feishu_client import FeishuClient


def _make_runtime(name: str = "test") -> _BotRuntime:
    feishu = FeishuClient(app_id="test", app_secret="test")
    bridge = ClaudeBridge(bot_name=name, claude_path="claude")
    return _BotRuntime(name=name, feishu=feishu, bridge=bridge)


class TestFindRuntime:
    def test_finds_by_bridge(self) -> None:
        r1 = _make_runtime("bot1")
        r2 = _make_runtime("bot2")
        bots = [r1, r2]

        assert _find_runtime(bots, r1.bridge) is r1
        assert _find_runtime(bots, r2.bridge) is r2

    def test_returns_none_if_not_found(self) -> None:
        r1 = _make_runtime("bot1")
        bots = [r1]
        orphan_bridge = ClaudeBridge(bot_name="orphan", claude_path="claude")

        assert _find_runtime(bots, orphan_bridge) is None

    def test_empty_bots_list(self) -> None:
        bridge = ClaudeBridge(bot_name="x", claude_path="claude")
        assert _find_runtime([], bridge) is None


class TestSetChat:
    def test_updates_current_chat(self) -> None:
        from feishu_cc.app import FeishuCCApp

        container: list[str | None] = [None]
        FeishuCCApp._set_chat(container, "chat_123")
        assert container[0] == "chat_123"

        FeishuCCApp._set_chat(container, "chat_456")
        assert container[0] == "chat_456"


class TestBotRuntime:
    def test_stop_loop_idempotent(self) -> None:
        rt = _make_runtime()
        rt.stop_loop()

    def test_run_async_on_unstarted_loop_raises(self) -> None:
        rt = _make_runtime()

        async def dummy() -> str:
            return "ok"

        with pytest.raises(Exception):
            rt.run_async(dummy())

    def test_schedule_does_not_block(self) -> None:
        """schedule is fire-and-forget, returns immediately."""
        rt = _make_runtime()
        rt.start_loop()

        async def dummy():
            return "ok"

        # schedule() should not await the coroutine, just schedule it
        rt.schedule(dummy())

    def test_stop_loop_cleans_up_session_file(self) -> None:
        """stop_loop should call bridge.stop and remove the session file."""
        from unittest.mock import AsyncMock

        rt = _make_runtime()
        rt.start_loop()

        # Create a fake session file on disk
        session_file = rt.bridge._session_file
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("stale-session-id", encoding="utf-8")
        assert session_file.exists()

        # Mock bridge.stop to avoid actually killing a process
        rt.bridge.stop = AsyncMock()

        rt.stop_loop()

        # Session file should be removed, bridge.stop should have been called
        assert not session_file.exists()
        rt.bridge.stop.assert_awaited_once()

        # Clean up the session dir if empty
        try:
            session_file.parent.rmdir()
        except OSError:
            pass

    def test_stop_loop_on_unstarted_skips_cleanup(self) -> None:
        """stop_loop on an unstarted runtime should not crash."""
        rt = _make_runtime()
        # No loop started, no session file — should be a no-op
        rt.stop_loop()


class TestOnToolNotify:
    def test_sends_tool_name_and_input(self) -> None:
        from feishu_cc.app import FeishuCCApp

        sent: list[str] = []
        feishu = type("MockFeishu", (), {"send_text": lambda self, cid, t: sent.append(t)})()
        FeishuCCApp._on_tool_notify(feishu, "chat_1", "Bash", "pytest tests/")
        assert sent == ["💻 Bash pytest tests/"]

    def test_sends_tool_name_only_without_input(self) -> None:
        from feishu_cc.app import FeishuCCApp

        sent: list[str] = []
        feishu = type("MockFeishu", (), {"send_text": lambda self, cid, t: sent.append(t)})()
        FeishuCCApp._on_tool_notify(feishu, "chat_1", "Read", "")
        assert sent == ["📖 Read"]


class TestOnMessage:
    def test_empty_message_id_skips_reactions(self) -> None:
        """_on_message with empty message_id schedules _handle without crashing."""
        import asyncio
        from feishu_cc.app import FeishuCCApp
        from feishu_cc.config import Config, BotConfig
        from feishu_cc.claude_bridge import ClaudeBridge

        app = FeishuCCApp.__new__(FeishuCCApp)
        app._config = Config(
            bots=[BotConfig(name="test", app_id="t", app_secret="s")],
            domain="feishu",
            claude_path="claude",
            render_mode="post",
            react_emoji="",
            done_emoji="",
        )

        class MockFeishu:
            def send_reply(self, *a, **kw):
                pass
            def _add_reaction(self, *a, **kw):
                raise RuntimeError("should not be called")
            def _remove_reaction(self, *a, **kw):
                raise RuntimeError("should not be called")

        bridge = ClaudeBridge(bot_name="test", claude_path="claude")
        bridge._ready.set()
        mock = MockFeishu()

        app._bots = []
        app._on_message("test", bridge, mock, "chat_1", "hello", "")


class TestRunShutdown:
    def test_keyboard_interrupt_triggers_shutdown(self) -> None:
        """run() should catch KeyboardInterrupt and stop all bots."""
        from unittest.mock import MagicMock, patch
        from feishu_cc.app import FeishuCCApp

        app = FeishuCCApp.__new__(FeishuCCApp)
        app._config = MagicMock()
        app._bots = []
        app._restart_requested = threading.Event()
        app._heal_state = None
        app._chat_ctx = {}

        mock_rt = MagicMock()
        mock_rt.name = "test"
        mock_rt.feishu = MagicMock()
        mock_rt.bridge = MagicMock()
        app._bots = [mock_rt]

        # Patch time.sleep in app module to raise KeyboardInterrupt on first call
        with patch("feishu_cc.app.time.sleep", side_effect=KeyboardInterrupt):
            app.run()

        mock_rt.stop_loop.assert_called_once()
