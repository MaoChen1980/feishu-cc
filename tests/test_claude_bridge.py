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


class TestSendMessageFormat:
    def test_user_message_format(self) -> None:
        msg = {"type": "user", "message": {"role": "user", "content": "hello"}}
        serialized = json.dumps(msg, ensure_ascii=False)
        parsed = json.loads(serialized)
        assert parsed["type"] == "user"
        assert parsed["message"]["content"] == "hello"
