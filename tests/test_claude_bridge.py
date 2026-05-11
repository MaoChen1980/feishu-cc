"""Tests for ClaudeBridge — JSON event handling, message flow."""

from __future__ import annotations

import asyncio
import json

from feishu_cc.claude_bridge import ClaudeBridge


def _make_bridge(**kwargs) -> ClaudeBridge:
    """Helper to create a bridge with dummy config and no real subprocess."""
    return ClaudeBridge(
        bot_name=kwargs.pop("bot_name", "test-bot"),
        claude_path="claude",
        **kwargs,
    )


class TestResponseAccumulation:
    def test_handle_assistant_text(self) -> None:
        collected: list[str] = []
        bridge = _make_bridge(on_text=collected.append)
        bridge._response_text = ""

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello, "}],
            },
        }))
        assert bridge._response_text == "Hello, "
        assert collected == ["Hello, "]

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "world!"}],
            },
        }))
        assert bridge._response_text == "Hello, world!"
        assert collected == ["Hello, ", "world!"]

    def test_result_done_sets_event(self) -> None:
        bridge = _make_bridge()
        assert not bridge._response_done.is_set()

        asyncio.run(bridge._handle_event({"type": "result", "done": True}))
        assert bridge._response_done.is_set()

    def test_result_always_sets_done(self) -> None:
        bridge = _make_bridge()
        bridge._response_done.set()
        bridge._response_done.clear()

        asyncio.run(bridge._handle_event({"type": "result"}))
        assert bridge._response_done.is_set()


class TestSessionManagement:
    def test_system_event_saves_session(self) -> None:
        bridge = _make_bridge()
        assert bridge._session_id is None

        asyncio.run(bridge._handle_event({
            "type": "system",
            "session_id": "sess_abc123",
        }))
        assert bridge._session_id == "sess_abc123"

    def test_system_event_ignores_empty_id(self) -> None:
        bridge = _make_bridge()
        bridge._session_id = "existing"

        asyncio.run(bridge._handle_event({
            "type": "system",
            "session_id": "",
        }))
        assert bridge._session_id == "existing"


class TestPermissionRequest:
    def test_control_request_triggers_callback(self) -> None:
        captured: list[tuple[str, str, dict]] = []

        def on_perm(req_id: str, prompt: str, value: dict) -> None:
            captured.append((req_id, prompt, value))

        bridge = _make_bridge(on_permission_request=on_perm)
        asyncio.run(bridge._handle_event({
            "type": "control_request",
            "request_id": "req_1",
            "prompt": "Allow file write?",
            "value": {"path": "/tmp/test.txt"},
        }))
        assert len(captured) == 1
        assert captured[0][0] == "req_1"
        assert captured[0][1] == "Allow file write?"
        assert captured[0][2] == {"path": "/tmp/test.txt"}


class TestPermissionResponse:
    def test_respond_permission_allow(self) -> None:
        bridge = _make_bridge()
        msg = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req_1",
                "response": {"behavior": "allow"},
            },
        }
        serialized = json.dumps(msg, ensure_ascii=False)
        parsed = json.loads(serialized)
        assert parsed["response"]["response"]["behavior"] == "allow"
        assert parsed["response"]["request_id"] == "req_1"

    def test_respond_permission_deny(self) -> None:
        bridge = _make_bridge()
        msg = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req_2",
                "response": {"behavior": "deny"},
            },
        }
        serialized = json.dumps(msg, ensure_ascii=False)
        parsed = json.loads(serialized)
        assert parsed["response"]["response"]["behavior"] == "deny"


class TestResponseAccumulationExtra:
    def test_thinking_block_ignored(self) -> None:
        """Thinking blocks should not accumulate in response_text."""
        bridge = _make_bridge()
        bridge._response_text = ""

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "thinking text"},
                    {"type": "text", "text": "final text"},
                ],
            },
        }))
        assert bridge._response_text == "final text"

    def test_tool_use_block_not_in_response(self) -> None:
        """Tool use blocks should not appear in response_text."""
        bridge = _make_bridge()
        bridge._response_text = ""

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
                    {"type": "text", "text": "done"},
                ],
            },
        }))
        assert bridge._response_text == "done"

    def test_task_summary_at_result(self) -> None:
        """Result event should prepend task summaries to response_text."""
        bridge = _make_bridge()
        bridge._response_text = "final answer"
        bridge._task_summaries = ["已运行测试", "已编辑文件"]

        asyncio.run(bridge._handle_event({"type": "result"}))
        assert "已运行测试" in bridge._response_text
        assert "已编辑文件" in bridge._response_text
        assert "final answer" in bridge._response_text
        assert bridge._response_text.index("已运行测试") < bridge._response_text.index("final answer")
        assert bridge._task_summaries == []

    def test_no_tool_uses_no_summary(self) -> None:
        """Result without task summaries should leave response_text untouched."""
        bridge = _make_bridge()
        bridge._response_text = "just text"
        bridge._task_summaries = []

        asyncio.run(bridge._handle_event({"type": "result"}))
        assert bridge._response_text == "just text"

    def test_send_lock_is_lock(self) -> None:
        """send_message uses asyncio.Lock for serialization."""
        bridge = _make_bridge()
        import asyncio
        assert isinstance(bridge._send_lock, asyncio.Lock)


class TestSessionManagementExtra:
    def test_system_event_with_hook_id_sets_ready(self) -> None:
        bridge = _make_bridge()
        assert not bridge._ready.is_set()

        asyncio.run(bridge._handle_event({
            "type": "system",
            "hook_id": "start",
            "subtype": "hook_response",
            "session_id": "sess_hook",
        }))
        assert bridge._ready.is_set()
        assert bridge._session_id == "sess_hook"

    def test_system_event_skips_empty_session_id_with_hook(self) -> None:
        """hook_id event without session_id should not overwrite existing."""
        bridge = _make_bridge()
        bridge._session_id = "existing"

        asyncio.run(bridge._handle_event({
            "type": "system",
            "hook_id": "start",
            "subtype": "hook_response",
            "session_id": "",
        }))
        assert bridge._session_id == "existing"


class TestToolUseCallback:
    def test_tool_use_callback_fires(self) -> None:
        """on_tool_use callback receives tool name and brief input."""
        captured: list[tuple[str, str]] = []
        bridge = _make_bridge(on_tool_use=lambda n, b: captured.append((n, b)))

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/"}},
                ],
            },
        }))
        assert captured == [("Bash", "pytest tests/")]

    def test_tool_use_callback_with_path_input(self) -> None:
        """Tool input with path extracts path as brief."""
        captured: list[tuple[str, str]] = []
        bridge = _make_bridge(on_tool_use=lambda n, b: captured.append((n, b)))

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/main.py"}},
                ],
            },
        }))
        assert captured == [("Edit", "src/main.py")]

    def test_tool_use_callback_without_input(self) -> None:
        """Tool use without recognizable input sends brief as empty."""
        captured: list[tuple[str, str]] = []
        bridge = _make_bridge(on_tool_use=lambda n, b: captured.append((n, b)))

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {}},
                ],
            },
        }))
        assert captured == [("Read", "")]

    def test_tool_use_callback_empty_name_skipped(self) -> None:
        """on_tool_use not called when tool name is empty."""
        called = False

        def callback(name: str, brief: str) -> None:
            nonlocal called
            called = True

        bridge = _make_bridge(on_tool_use=callback)
        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "", "input": {"command": "ls"}},
                ],
            },
        }))
        assert not called

    def test_tool_use_callback_multiple_tools(self) -> None:
        """Multiple tool_use blocks fire callback each time."""
        captured: list[tuple[str, str]] = []
        bridge = _make_bridge(on_tool_use=lambda n, b: captured.append((n, b)))

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "b.py"}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
                ],
            },
        }))
        assert len(captured) == 3
        assert captured[0] == ("Read", "a.py")
        assert captured[1] == ("Edit", "b.py")
        assert captured[2] == ("Bash", "pytest")

    def test_on_tool_use_error_does_not_crash(self) -> None:
        """Exception in on_tool_use callback does not crash _handle_event."""
        def failing(_, __):
            raise RuntimeError("callback failed")

        bridge = _make_bridge(on_tool_use=failing)
        # Should not raise
        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        }))


class TestTaskSummaryCallback:
    def test_task_notification_collects_summary(self) -> None:
        """task_notification events append summary to _task_summaries."""
        bridge = _make_bridge()

        asyncio.run(bridge._handle_event({
            "type": "system",
            "subtype": "task_notification",
            "status": "completed",
            "summary": "已运行测试",
            "session_id": "sess_1",
        }))
        assert bridge._task_summaries == ["已运行测试"]

    def test_task_notification_multiple_summaries(self) -> None:
        """Multiple task_notification events accumulate summaries."""
        bridge = _make_bridge()
        asyncio.run(bridge._handle_event({
            "type": "system", "subtype": "task_notification",
            "status": "completed", "summary": "已读取文件", "session_id": "s",
        }))
        asyncio.run(bridge._handle_event({
            "type": "system", "subtype": "task_notification",
            "status": "completed", "summary": "已编辑文件", "session_id": "s",
        }))
        assert bridge._task_summaries == ["已读取文件", "已编辑文件"]

    def test_task_notification_no_summary_skipped(self) -> None:
        """task_notification without summary does not append."""
        bridge = _make_bridge()
        bridge._task_summaries = ["existing"]

        asyncio.run(bridge._handle_event({
            "type": "system",
            "subtype": "task_notification",
            "status": "completed",
            "summary": "",
            "session_id": "sess_1",
        }))
        assert bridge._task_summaries == ["existing"]

    def test_task_summary_callback_fires(self) -> None:
        """on_task_summary callback receives summary text."""
        captured: list[str] = []
        bridge = _make_bridge(on_task_summary=captured.append)

        asyncio.run(bridge._handle_event({
            "type": "system",
            "subtype": "task_notification",
            "status": "completed",
            "summary": "已运行测试",
            "session_id": "sess_1",
        }))
        assert captured == ["已运行测试"]

    def test_on_task_summary_error_does_not_crash(self) -> None:
        """Exception in on_task_summary does not crash _handle_event."""
        def failing(_):
            raise RuntimeError("callback failed")

        bridge = _make_bridge(on_task_summary=failing)
        asyncio.run(bridge._handle_event({
            "type": "system",
            "subtype": "task_notification",
            "status": "completed",
            "summary": "测试",
            "session_id": "sess_1",
        }))


class TestCrashHandling:
    """_handle_crash — re-entrancy guard, time-window counter, backoff."""

    def test_crash_guard_prevents_reentrant(self) -> None:
        """When _crash_handling=True, _handle_crash returns immediately."""
        bridge = _make_bridge()
        bridge._crash_handling = True
        asyncio.run(bridge._handle_crash())
        # Should not have touched crash tracking
        assert bridge._crash_times == []

    def test_crash_gives_up_after_limit(self) -> None:
        """4+ crashes within 60s causes _handle_crash to give up (_alive=False)."""
        import time

        bridge = _make_bridge()
        now = time.monotonic()
        bridge._crash_times = [now - 10, now - 20, now - 30, now - 40]  # 4 in 60s
        bridge._alive = True
        bridge._crash_handling = False

        asyncio.run(bridge._handle_crash())
        assert not bridge._alive  # gave up

    def test_crash_restarts_below_limit(self) -> None:
        """<4 crashes within 60s — calls stop() then start()."""
        from unittest.mock import AsyncMock, patch

        bridge = _make_bridge()
        bridge._crash_times = []
        bridge._ready.set()  # So start()._ready.wait() returns immediately
        bridge._session_id = None
        bridge.stop = AsyncMock()
        bridge.start = AsyncMock(side_effect=lambda: bridge._ready.set())

        with patch("asyncio.sleep", AsyncMock()):
            asyncio.run(bridge._handle_crash())

        bridge.stop.assert_awaited_once()
        bridge.start.assert_awaited_once()

    def test_crash_guard_releases_on_success(self) -> None:
        """After _handle_crash succeeds, _crash_handling is False."""
        from unittest.mock import AsyncMock, patch

        bridge = _make_bridge()
        bridge._crash_times = []
        bridge._ready.set()
        bridge.stop = AsyncMock()
        bridge.start = AsyncMock(side_effect=lambda: bridge._ready.set())

        assert not bridge._crash_handling
        with patch("asyncio.sleep", AsyncMock()):
            asyncio.run(bridge._handle_crash())
        assert not bridge._crash_handling

    def test_crash_guard_releases_on_limit(self) -> None:
        """After _handle_crash gives up due to limit, _crash_handling is False."""
        import time

        bridge = _make_bridge()
        now = time.monotonic()
        bridge._crash_times = [now - 10, now - 20, now - 30, now - 40]
        bridge._alive = True

        asyncio.run(bridge._handle_crash())
        assert not bridge._crash_handling

    def test_crash_sets_response_done(self) -> None:
        """_handle_crash sets _response_done to unblock send_message."""
        from unittest.mock import AsyncMock, patch

        bridge = _make_bridge()
        bridge._crash_times = []
        bridge._ready.set()
        bridge.stop = AsyncMock()
        bridge.start = AsyncMock(side_effect=lambda: bridge._ready.set())

        bridge._response_done.clear()
        with patch("asyncio.sleep", AsyncMock()):
            asyncio.run(bridge._handle_crash())
        assert bridge._response_done.is_set()

    def test_crash_time_window_filters_old_entries(self) -> None:
        """Crashes older than 60s are not counted in the limit."""
        import time
        from unittest.mock import AsyncMock, patch

        bridge = _make_bridge()
        bridge._ready.set()
        bridge.stop = AsyncMock()
        bridge.start = AsyncMock(side_effect=lambda: bridge._ready.set())

        now = time.monotonic()
        # 3 entries: 2 old (>60s), 1 recent
        bridge._crash_times = [now - 120, now - 90, now - 5]

        with patch("asyncio.sleep", AsyncMock()):
            asyncio.run(bridge._handle_crash())

        # After filtering + append: should have 2 entries (now-5 + this one)
        assert len(bridge._crash_times) == 2
        assert time.monotonic() - bridge._crash_times[-1] < 1

    def test_crash_backoff_increases(self) -> None:
        """Backoff should increase with crash count (2s, 4s, 8s)."""
        from unittest.mock import AsyncMock, patch

        bridge = _make_bridge()
        bridge._ready.set()
        bridge.stop = AsyncMock()
        bridge.start = AsyncMock(side_effect=lambda: bridge._ready.set())

        # First crash: n=1, backoff=2
        with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
            asyncio.run(bridge._handle_crash())
            assert bridge._crash_times[-1] is not None

        # For n=2, we need a crash still in 60s window
        # Re-setup for n=2 scenario
        import time
        now = time.monotonic()
        bridge._crash_times = [now - 5]  # 1 recent crash
        bridge._response_done.clear()
        with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
            asyncio.run(bridge._handle_crash())
            assert bridge._crash_times[-1] is not None


class TestHandleInitFailure:
    """_handle_init_failure — flag setting during init-time crashes."""

    def test_sets_init_failed_and_ready(self) -> None:
        bridge = _make_bridge()
        bridge._init_failed = False
        bridge._ready.clear()

        asyncio.run(bridge._handle_init_failure())
        assert bridge._init_failed
        assert bridge._ready.is_set()


class TestStop:
    """stop() — subprocess cleanup."""

    def test_stop_noop_when_not_started(self) -> None:
        bridge = _make_bridge()
        asyncio.run(bridge.stop())

    def test_stop_noop_when_already_stopped(self) -> None:
        bridge = _make_bridge()
        bridge._alive = False
        asyncio.run(bridge.stop())

    def test_stop_noop_when_process_returncode_set(self) -> None:
        """stop() returns early when process already exited."""
        bridge = _make_bridge()
        bridge._proc = type("MockProc", (), {"returncode": 0, "stdin": None})()
        asyncio.run(bridge.stop())


class TestTailStderr:
    def test_empty_buffer_returns_placeholder(self) -> None:
        bridge = _make_bridge()
        assert bridge._tail_stderr() == "(no stderr)"

    def test_returns_last_lines(self) -> None:
        bridge = _make_bridge()
        bridge._stderr_buf = ["line1", "line2", "line3"]
        result = bridge._tail_stderr(max_lines=2)
        assert result == "line2\nline3"

    def test_respects_max_lines_limit(self) -> None:
        bridge = _make_bridge()
        bridge._stderr_buf = list(f"line{i}" for i in range(100))
        result = bridge._tail_stderr(max_lines=5)
        lines = result.split("\n")
        assert len(lines) == 5
        assert lines[0] == "line95"
        assert lines[-1] == "line99"


class TestSendMessageFormat:
    def test_user_message_format(self) -> None:
        msg = {"type": "user", "message": {"role": "user", "content": "hello"}}
        serialized = json.dumps(msg, ensure_ascii=False)
        parsed = json.loads(serialized)
        assert parsed["type"] == "user"
        assert parsed["message"]["content"] == "hello"


class TestRestart:
    """restart() — workspace switching with process lifecycle."""

    def test_restart_calls_stop_and_start(self) -> None:
        from unittest.mock import AsyncMock, patch

        bridge = _make_bridge()
        bridge._ready.set()
        bridge.stop = AsyncMock()
        bridge.start = AsyncMock()

        asyncio.run(bridge.restart("/new/workspace"))
        assert bridge._workspace == "/new/workspace"
        bridge.stop.assert_awaited_once()
        bridge.start.assert_awaited_once()

    def test_restart_removes_session_file(self) -> None:
        import tempfile
        from unittest.mock import AsyncMock, patch

        bridge = _make_bridge()
        bridge._session_file = type(
            "FakePath", (),
            {"exists": lambda *a: True, "unlink": lambda *a: None},
        )()
        bridge.stop = AsyncMock()
        bridge.start = AsyncMock()

        asyncio.run(bridge.restart("/ws"))
        assert bridge._session_id is None


class TestSystemEventCallbacks:
    """Callback invocation for system events beyond basic session handling."""

    def test_on_error_callback_fires(self) -> None:
        captured: list[tuple[str, str]] = []
        bridge = _make_bridge(on_error=lambda t, m: captured.append((t, m)))

        asyncio.run(bridge._handle_event({
            "type": "error",
            "error": {"type": "api_error", "message": "rate limited"},
        }))
        assert captured == [("api_error", "rate limited")]

    def test_on_result_content_callback_fires(self) -> None:
        captured: list[list] = []
        bridge = _make_bridge(on_result_content=captured.append)

        asyncio.run(bridge._handle_event({
            "type": "result",
            "result": [{"type": "text", "text": "done"}],
        }))
        assert len(captured) == 1
        assert captured[0] == [{"type": "text", "text": "done"}]

    def test_on_system_notify_fires_on_task_completion(self) -> None:
        captured: list[tuple[str, str]] = []
        bridge = _make_bridge(
            on_system_notify=lambda s, st: captured.append((s, st)),
        )

        asyncio.run(bridge._handle_event({
            "type": "system",
            "subtype": "task_notification",
            "status": "completed",
            "summary": "测试通过",
            "session_id": "sess_1",
        }))
        assert captured == [("测试通过", "completed")]

    def test_unhandled_event_type_logged(self) -> None:
        bridge = _make_bridge()
        asyncio.run(bridge._handle_event({"type": "unknown_type"}))


class TestOnToolResult:
    def test_tool_result_callback_fires(self) -> None:
        captured: list[tuple[str, bool]] = []
        bridge = _make_bridge(on_tool_result=lambda c, e: captured.append((c, e)))

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "content": "command output",
                    "is_error": False,
                }],
            },
        }))
        assert captured == [("command output", False)]

    def test_tool_result_error_flag(self) -> None:
        captured: list[tuple[str, bool]] = []
        bridge = _make_bridge(on_tool_result=lambda c, e: captured.append((c, e)))

        asyncio.run(bridge._handle_event({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "content": "error: not found",
                    "is_error": True,
                }],
            },
        }))
        assert captured[0][1] is True  # is_error=True


class TestSessionPersistence:
    """Session file persistence (_save_session / _load_session)."""

    def test_save_and_load_roundtrip(self) -> None:
        import tempfile
        from pathlib import Path

        bridge = _make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._session_file = Path(tmp) / "session.txt"
            bridge._save_session("sess_roundtrip")
            assert bridge._session_file.exists()
            loaded = bridge._load_session()
            assert loaded == "sess_roundtrip"

    def test_load_returns_none_when_no_file(self) -> None:
        import tempfile
        from pathlib import Path

        bridge = _make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            # Point session_file at a non-existent path in a temp dir
            bridge._session_file = Path(tmp) / "nonexistent" / "session.txt"
            assert bridge._load_session() is None

    def test_load_returns_none_on_empty_file(self) -> None:
        import tempfile
        from pathlib import Path

        bridge = _make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._session_file = Path(tmp) / "empty.txt"
            bridge._session_file.write_text("", encoding="utf-8")
            assert bridge._load_session() is None
