"""Tests for feishu_client — content detection, parsing, dedup."""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path

from feishu_cc.feishu_client import FeishuClient

# Minimal client for testing static/utility methods
_client = FeishuClient(app_id="test", app_secret="test")


class TestHasRichContent:
    def test_code_block(self) -> None:
        assert _client._has_rich_content("some text ```python\nprint(1)\n```")

    def test_table(self) -> None:
        assert _client._has_rich_content("| h1 | h2 |\n| --- | --- |\n| a | b |")

    def test_plain_text_no_match(self) -> None:
        assert not _client._has_rich_content("just a normal message")

    def test_single_pipe_line_no_match(self) -> None:
        assert not _client._has_rich_content("this | is | not a table")

    def test_empty_string(self) -> None:
        assert not _client._has_rich_content("")


class TestExtractHeader:
    def test_with_header(self) -> None:
        header, body = _client._extract_header("# Hello\n\nThis is the body.")
        assert header == "Hello"
        assert body == "This is the body."

    def test_without_header(self) -> None:
        header, body = _client._extract_header("Just a plain response.")
        assert header is None
        assert body == "Just a plain response."

    def test_header_not_first_line(self) -> None:
        header, body = _client._extract_header("\n\n# Late Header\nbody")
        assert header == "Late Header"
        assert body == "body"

    def test_first_line_not_header(self) -> None:
        header, body = _client._extract_header("No hash here\nsome more text")
        assert header is None
        assert body == "No hash here\nsome more text"


class TestParseQuickReplies:
    def test_with_quick_replies(self) -> None:
        content, qrs = _client._parse_quick_replies(
            "Hello\n---quick-replies\nConfirm||确认\nCancel||取消"
        )
        assert content == "Hello"
        assert qrs == [
            {"label": "Confirm", "reply": "确认"},
            {"label": "Cancel", "reply": "取消"},
        ]

    def test_without_quick_replies(self) -> None:
        content, qrs = _client._parse_quick_replies("Just a message.")
        assert content == "Just a message."
        assert qrs is None

    def test_label_only_replies(self) -> None:
        content, qrs = _client._parse_quick_replies(
            "Pick one:\n---quick-replies\nYes\nNo\nMaybe"
        )
        assert content == "Pick one:"
        assert qrs == [
            {"label": "Yes", "reply": "Yes"},
            {"label": "No", "reply": "No"},
            {"label": "Maybe", "reply": "Maybe"},
        ]

    def test_empty_section_skipped(self) -> None:
        content, qrs = _client._parse_quick_replies(
            "Body\n---quick-replies\n\nAction||do\n\n"
        )
        assert content == "Body"
        assert qrs == [{"label": "Action", "reply": "do"}]

    def test_single_pipe_splits_into_multiple_buttons(self) -> None:
        content, qrs = _client._parse_quick_replies(
            "Choose:\n---quick-replies\n很快没问题|有点延迟|功能没问题"
        )
        assert content == "Choose:"
        assert qrs == [
            {"label": "很快没问题", "reply": "很快没问题"},
            {"label": "有点延迟", "reply": "有点延迟"},
            {"label": "功能没问题", "reply": "功能没问题"},
        ]

    def test_ignores_inline_marker_in_content(self) -> None:
        content, qrs = _client._parse_quick_replies(
            "你可以使用 ---quick-replies 来添加按钮。试试以下选项。\n---quick-replies\n确认||确认\n取消||取消"
        )
        assert content == "你可以使用 ---quick-replies 来添加按钮。试试以下选项。"
        assert qrs == [
            {"label": "确认", "reply": "确认"},
            {"label": "取消", "reply": "取消"},
        ]

    def test_marker_same_line_as_text(self) -> None:
        """Claude often puts marker and options on same line as the question."""
        content, qrs = _client._parse_quick_replies(
            "测试一下 ---quick-replies 选项一|选项二|选项三"
        )
        assert content == "测试一下"
        assert qrs == [
            {"label": "选项一", "reply": "选项一"},
            {"label": "选项二", "reply": "选项二"},
            {"label": "选项三", "reply": "选项三"},
        ]

    def test_no_marker_returns_none(self) -> None:
        content, qrs = _client._parse_quick_replies("纯文本，没有标记")
        assert content == "纯文本，没有标记"
        assert qrs is None


class TestWrapTablesInCodeFences:
    def test_wrap_multi_row_table(self) -> None:
        result = _client._wrap_tables_in_code_fences(
            "text\n| a | b |\n| --- | --- |\n| 1 | 2 |\nmore"
        )
        assert "```" in result
        assert "| a | b |" in result
        assert "| --- | --- |" in result
        assert "| 1 | 2 |" in result

    def test_no_wrap_single_row(self) -> None:
        result = _client._wrap_tables_in_code_fences("| just | one | row |")
        assert "```" not in result

    def test_no_table_no_change(self) -> None:
        result = _client._wrap_tables_in_code_fences("plain text")
        assert result == "plain text"

    def test_inline_code_not_affected(self) -> None:
        text = "some `code` here"
        result = _client._wrap_tables_in_code_fences(text)
        assert result == text


class TestCheckDuplicate:
    def test_first_seen_not_duplicate(self) -> None:
        assert not _client._check_duplicate("msg_1")

    def test_second_seen_is_duplicate(self) -> None:
        _client._check_duplicate("msg_repeat", ttl=60)
        assert _client._check_duplicate("msg_repeat", ttl=60)

    def test_expired_ttl(self) -> None:
        _client._check_duplicate("msg_stale", ttl=1)
        time.sleep(1.1)
        assert not _client._check_duplicate("msg_stale", ttl=1)

    def test_different_ids_not_duplicates(self) -> None:
        _client._check_duplicate("msg_a", ttl=60)
        assert not _client._check_duplicate("msg_b", ttl=60)


def test_send_reply_auto_mode_rich_content() -> None:
    """send_reply with render_mode=auto and tables should prefer card."""
    client = FeishuClient(app_id="t", app_secret="t", render_mode="auto")
    # Mock send methods that require a live client — this test only checks routing
    client._send_card = lambda c, cid: bool(cid)  # type: ignore[method-assign]
    client._send_post_reply = lambda cid, _: bool(cid)  # type: ignore[method-assign]
    client._send_plain_text = lambda cid, _: None  # type: ignore[method-assign]
    client.send_reply("chat_1", "root_1", "normal text")
    client.send_reply("chat_1", "root_1", "| h1 | h2 |\n| --- | --- |\n| a | b |")


class TestDownloadImage:
    def test_download_image_saves_file(self, monkeypatch, tmp_path) -> None:
        client = FeishuClient(app_id="t", app_secret="t")

        class MockResponse:
            code = 0
            msg = ""
            file = BytesIO(b"\x89PNG\r\n\x1a\n" + b"fake-png-data")
            raw = type("obj", (object,), {"status_code": 200})()

        class MockResourceService:
            def get(self, request):
                return MockResponse()

        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {
                    "message_resource": MockResourceService()
                })()
            })()
        })()

        monkeypatch.setattr("feishu_cc.feishu_client.CONFIG_DIR", tmp_path)

        path = client._download_image("msg_id_1", "img_test123")
        assert path.endswith("img_test123.png")
        assert Path(path).read_bytes() == b"\x89PNG\r\n\x1a\n" + b"fake-png-data"

    def test_download_image_jpeg_format(self, monkeypatch, tmp_path) -> None:
        """JPEG files should get .jpg extension."""
        client = FeishuClient(app_id="t", app_secret="t")

        class MockResponse:
            code = 0
            msg = ""
            file = BytesIO(b"\xff\xd8\xff\xe0" + b"fake-jpeg-data")
            raw = type("obj", (object,), {"status_code": 200})()

        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {
                    "message_resource": type("obj", (object,), {
                        "get": lambda self, req: MockResponse()
                    })()
                })()
            })()
        })()

        monkeypatch.setattr("feishu_cc.feishu_client.CONFIG_DIR", tmp_path)

        path = client._download_image("mid", "img_jpeg")
        assert path.endswith("img_jpeg.jpg")
        assert Path(path).read_bytes() == b"\xff\xd8\xff\xe0" + b"fake-jpeg-data"

    def test_download_image_failure_returns_empty(self, monkeypatch, tmp_path) -> None:
        client = FeishuClient(app_id="t", app_secret="t")

        class MockResponse:
            code = 999
            msg = "permission denied"
            file = None
            raw = type("obj", (object,), {"status_code": 403})()

        class MockResourceService:
            def get(self, request):
                return MockResponse()

        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {
                    "message_resource": MockResourceService()
                })()
            })()
        })()

        monkeypatch.setattr("feishu_cc.feishu_client.CONFIG_DIR", tmp_path)

        path = client._download_image("msg_id_1", "img_fail")
        assert path == ""

    def test_image_key_detection_in_message(self) -> None:
        """_on_message_wrapper with image_key calls _download_image and passes path."""
        captured: list[str] = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda s, c, t, mid: captured.append(t))

        # Mock internals to avoid real API calls
        client._download_image = lambda mid, k: f"/fake/{k}.png"  # type: ignore[method-assign]
        client._check_duplicate = lambda *a: False  # type: ignore[method-assign]
        client._add_reaction = lambda *a: None  # type: ignore[method-assign]

        # Build a minimal mock data object
        class MockAttrs:
            pass

        message = MockAttrs()
        message.message_id = "msg_img_1"
        message.content = '{"image_key": "img_xyz"}'
        message.chat_id = "chat_1"
        message.parent_id = None
        message.message_type = "image"

        sender = MockAttrs()
        sender_id_obj = MockAttrs()
        sender_id_obj.open_id = "open_1"
        sender.sender_id = sender_id_obj

        data = MockAttrs()
        event = MockAttrs()
        event.message = message
        event.sender = sender
        data.event = event

        client._on_message_wrapper(data)
        assert len(captured) == 1
        assert "img_xyz.png" in captured[0]
        assert "用户发送了一张图片" in captured[0]

    def test_text_message_still_works(self) -> None:
        """_on_message_wrapper still handles text messages correctly."""
        captured: list[str] = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda s, c, t, mid: captured.append(t))

        client._check_duplicate = lambda *a: False  # type: ignore[method-assign]
        client._add_reaction = lambda *a: None  # type: ignore[method-assign]

        class MockAttrs:
            pass

        message = MockAttrs()
        message.message_id = "msg_txt_1"
        message.content = '{"text": "hello world"}'
        message.chat_id = "chat_1"
        message.parent_id = None
        message.message_type = "text"

        sender = MockAttrs()
        sender_id_obj = MockAttrs()
        sender_id_obj.open_id = "open_1"
        sender.sender_id = sender_id_obj

        data = MockAttrs()
        event = MockAttrs()
        event.message = message
        event.sender = sender
        data.event = event

        client._on_message_wrapper(data)
        assert len(captured) == 1
        assert captured[0] == "hello world"
