"""Tests for FeishuCCApp — runtime helpers, command handling."""

from __future__ import annotations

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
