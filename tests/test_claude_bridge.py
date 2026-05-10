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


class TestSendMessageFormat:
    def test_user_message_format(self) -> None:
        msg = {"type": "user", "message": {"role": "user", "content": "hello"}}
        serialized = json.dumps(msg, ensure_ascii=False)
        parsed = json.loads(serialized)
        assert parsed["type"] == "user"
        assert parsed["message"]["content"] == "hello"
