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
    client.send_plain_text = lambda cid, _: None  # type: ignore[method-assign]
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


class TestNoop:
    def test_noop_does_nothing(self) -> None:
        assert _client._noop("anything") is None
        assert _client._noop(42) is None


class TestFormatPermissionValue:
    def test_flat_dict(self) -> None:
        r = _client._format_permission_value({"tool": "bash", "cmd": "ls"})
        assert "**tool**" in r and "`bash`" in r
        assert "**cmd**" in r and "`ls`" in r

    def test_nested_dict(self) -> None:
        r = _client._format_permission_value({
            "tool": "bash", "args": {"cmd": "ls", "timeout": "30"},
        })
        assert "**args**" in r
        assert "**cmd**" in r

    def test_list_values(self) -> None:
        r = _client._format_permission_value({"files": ["a.py", "b.py"]})
        assert "`a.py`" in r
        assert "`b.py`" in r

    def test_skips_redundant_tool_use(self) -> None:
        r = _client._format_permission_value({
            "tool_name": "bash",
            "tool_use": {"cmd": "ls"},
            "tool_args": '{"cmd": "ls"}',
        })
        assert "**tool_name**" in r
        assert "**tool_use**" not in r  # skipped as redundant
        assert "**tool_args**" in r


class TestOnCardActionWrapper:
    """_on_card_action_wrapper — card button click handling."""

    def _data(self, *, qr="confirm", cid="chat_1", open_id="open_1", action_name="btn_1"):
        class _M:
            pass
        act = _M()
        act.value = {"qr": qr, "cid": cid}
        act.name = action_name
        op = _M()
        op.open_id = open_id
        evt = _M()
        evt.action = act
        evt.operator = op
        d = _M()
        d.event = evt
        return d

    def test_normal(self) -> None:
        captured: list = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_card_action=lambda r, c, s: captured.append((r, c, s)))
        client._check_duplicate = lambda _: False  # type: ignore
        client._on_card_action_wrapper(self._data())
        assert captured == [("confirm", "chat_1", "open_1")]

    def test_missing_qr_early_return(self) -> None:
        captured: list = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_card_action=lambda *a: captured.append(a))
        client._check_duplicate = lambda _: False  # type: ignore
        client._on_card_action_wrapper(self._data(qr=""))
        assert captured == []

    def test_missing_cid_early_return(self) -> None:
        captured: list = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_card_action=lambda *a: captured.append(a))
        client._check_duplicate = lambda _: False  # type: ignore
        client._on_card_action_wrapper(self._data(cid=""))
        assert captured == []

    def test_missing_open_id_early_return(self) -> None:
        captured: list = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_card_action=lambda *a: captured.append(a))
        client._check_duplicate = lambda _: False  # type: ignore
        client._on_card_action_wrapper(self._data(open_id=""))
        assert captured == []

    def test_duplicate_skipped(self) -> None:
        captured: list = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_card_action=lambda *a: captured.append(a))
        client._check_duplicate = lambda _: True  # type: ignore
        client._on_card_action_wrapper(self._data())
        assert captured == []

    def test_callback_exception_caught(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t",
                              on_card_action=lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
        client._check_duplicate = lambda _: False  # type: ignore
        client._on_card_action_wrapper(self._data())  # should not raise

    def test_value_not_dict_empty_fallback(self) -> None:
        captured: list = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_card_action=lambda *a: captured.append(a))
        client._check_duplicate = lambda _: False  # type: ignore
        class _M:
            pass
        act = _M()
        act.value = "not_a_dict"
        act.name = "b"
        op = _M()
        op.open_id = "o"
        evt = _M()
        evt.action = act
        evt.operator = op
        d = _M()
        d.event = evt
        client._on_card_action_wrapper(d)
        assert captured == []


class TestSendPermissionCard:
    def test_basic(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        sent: list = []
        client._send_card = lambda c, cid: sent.append(c) or True  # type: ignore
        client.send_permission_card("chat_1", "Allow?", "req_1")
        els = sent[0]["body"]["elements"]
        assert len(els) == 4
        assert els[1]["text"]["content"] == "🟢 允许一次"
        assert els[2]["text"]["content"] == "允许本次"
        assert els[3]["text"]["content"] == "拒绝"

    def test_with_tool_value(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        sent: list = []
        client._send_card = lambda c, cid: sent.append(c) or True  # type: ignore
        client.send_permission_card("chat_1", "Allow?", "req_2", value={
            "tool_name": "Bash",
            "tool_args": {"cmd": "ls"},
            "permission_prompt": "Run shell?",
        })
        c = sent[0]["body"]["elements"][0]["content"]
        assert "Bash" in c and "Run shell?" in c

    def test_empty_value_uses_prompt(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        sent: list = []
        client._send_card = lambda c, cid: sent.append(c) or True  # type: ignore
        client.send_permission_card("chat_1", "Allow?", "req_3", value={})
        assert sent[0]["body"]["elements"][0]["content"] == "Allow?"


class TestSendCardReply:
    def test_with_header(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        sent: list = []
        client._send_card = lambda c, cid: sent.append((c, cid)) or True  # type: ignore
        client._send_card_reply("chat_1", "# Title\n\nBody")
        card, cid = sent[0]
        assert cid == "chat_1"
        assert card["header"]["title"]["content"] == "Title"

    def test_without_header(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        sent: list = []
        client._send_card = lambda c, cid: sent.append(c) or True  # type: ignore
        client._send_card_reply("chat_1", "No header")
        assert "header" not in sent[0]

    def test_with_quick_replies(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        sent: list = []
        client._send_card = lambda c, cid: sent.append(c) or True  # type: ignore
        client._send_card_reply("chat_1", "Pick:", quick_replies=[
            {"label": "Y", "reply": "yes"}, {"label": "N", "reply": "no"},
        ])
        els = sent[0]["body"]["elements"]
        assert len(els) == 3
        assert els[1]["text"]["content"] == "Y"
        assert els[2]["value"]["cid"] == "chat_1"


class TestSendReplyRouting:
    def test_plain_text_fallback(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t", render_mode="text")
        plain: list = []
        client.send_plain_text = lambda cid, t: plain.append(t)  # type: ignore
        client._send_post_reply = lambda cid, c: False  # type: ignore
        client.send_reply("chat_1", None, "hello")
        assert plain == ["hello"]

    def test_card_mode_uses_card(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t", render_mode="card")
        card_sent: list = []
        client._send_card_reply = lambda cid, c, **kw: card_sent.append((cid, c)) or True  # type: ignore
        client.send_reply("chat_1", None, "hello")
        assert card_sent == [("chat_1", "hello")]

    def test_auto_mode_with_table_uses_card(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t", render_mode="auto")
        card_sent: list = []
        client._send_card_reply = lambda cid, c, **kw: card_sent.append((cid, c)) or True  # type: ignore
        client.send_reply("chat_1", None, "| h1 | h2 |\n| --- | --- |\n| a | b |")
        assert len(card_sent) == 1


class TestSendImplementations:
    """_send_card, _send_post_reply, send_plain_text — low-level send via SDK."""

    def _mock_client(self, client, success: bool):
        def _make_resp():
            if success:
                return type("R", (), {"code": 0, "msg": "", "success": lambda self: True})()
            return type("R", (), {"code": 999, "msg": "fail", "success": lambda self: False})()
        class S:
            def create(self, req): return _make_resp()
        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {"message": S()})(),
            })(),
        })()

    def test_send_card_success(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        self._mock_client(client, True)
        assert client._send_card({"schema": "2.0"}, "chat_1") is True

    def test_send_card_failure(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        self._mock_client(client, False)
        assert client._send_card({"schema": "2.0"}, "chat_1") is False

    def test_send_post_success(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        self._mock_client(client, True)
        assert client._send_post_reply("chat_1", "hello") is True

    def test_send_post_failure(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        self._mock_client(client, False)
        assert client._send_post_reply("chat_1", "hello") is False

    def test_send_plain_text(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        calls = 0
        class R:
            code = 0
            def success(self): return True
        class S:
            def create(self, req):
                nonlocal calls
                calls += 1
                return R()
        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {"message": S()})(),
            })(),
        })()
        client.send_plain_text("chat_1", "hello")
        assert calls == 1


class TestAddRemoveReaction:
    def test_add_noop_when_emoji_empty(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        client._add_reaction("msg_1", "")
        client._add_reaction("msg_1", None)  # type: ignore

    def test_remove_reaction(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        class S:
            def delete(self, req): pass
        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {
                    "message_reaction": S(),
                })(),
            })(),
        })()
        client._remove_reaction("msg_1")  # should not raise


class TestCleanupTemp:
    def test_cleans_old_files(self, monkeypatch, tmp_path) -> None:
        import os, time as tm
        monkeypatch.setattr("feishu_cc.feishu_client.CONFIG_DIR", tmp_path)
        td = tmp_path / "temp"
        td.mkdir()
        old_f = td / "old.txt"
        old_f.write_text("x")
        os.utime(str(old_f), (tm.time() - 86400 * 2, tm.time() - 86400 * 2))
        new_f = td / "new.txt"
        new_f.write_text("x")
        FeishuClient(app_id="t", app_secret="t")._cleanup_temp()
        assert not old_f.exists()
        assert new_f.exists()

    def test_missing_temp_dir(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("feishu_cc.feishu_client.CONFIG_DIR", tmp_path)
        FeishuClient(app_id="t", app_secret="t")._cleanup_temp()  # no raise


class TestFetchQuotedMessage:
    def test_returns_text_on_success(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        class I:
            body = type("o", (), {"content": '{"text": "original msg"}'})()
        class R:
            def success(self): return True
            data = type("o", (), {"items": [I()]})()
        class S:
            def get(self, req): return R()
        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {"message": S()})(),
            })(),
        })()
        assert client._fetch_quoted_message("p1") == "original msg"

    def test_returns_empty_on_failure(self) -> None:
        client = FeishuClient(app_id="t", app_secret="t")
        class R:
            def success(self): return False
        class S:
            def get(self, req): return R()
        client._client = type("obj", (object,), {
            "im": type("obj", (object,), {
                "v1": type("obj", (object,), {"message": S()})(),
            })(),
        })()
        assert client._fetch_quoted_message("p1") == ""


class TestOnMessageWrapperEdgeCases:
    def test_duplicate_message_id_skipped(self) -> None:
        captured = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda *a: captured.append(a))
        client._check_duplicate = lambda _: True  # type: ignore
        class _M: pass
        d = type("o", (), {"event": type("o", (), {
            "message": type("o", (), {"message_id": "dup"})(),
            "sender": type("o", (), {"sender_id": type("o", (), {"open_id": "o"})()}),
        })()})()
        client._on_message_wrapper(d)
        assert captured == []

    def test_missing_message_id_returns_early(self) -> None:
        captured = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda *a: captured.append(a))
        class _M: pass
        d = type("o", (), {"event": type("o", (), {
            "message": type("o", (), {"message_id": None})(),
            "sender": type("o", (), {"sender_id": type("o", (), {"open_id": "o"})()}),
        })()})()
        client._on_message_wrapper(d)
        assert captured == []

    def test_quoted_message_appended(self) -> None:
        captured = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda s, c, t, m: captured.append(t))
        client._check_duplicate = lambda _: False  # type: ignore
        client._add_reaction = lambda *a: None  # type: ignore
        client._fetch_quoted_message = lambda m: "quoted text"  # type: ignore
        d = type("o", (), {"event": type("o", (), {
            "message": type("o", (), {
                "message_id": "mq", "content": '{"text": "reply"}',
                "chat_id": "c1", "parent_id": "p123",
            })(),
            "sender": type("o", (), {"sender_id": type("o", (), {"open_id": "o"})()}),
        })()})()
        client._on_message_wrapper(d)
        assert len(captured) == 1
        assert "[引用]quoted text" in captured[0]

    def test_no_sender_open_id_falls_back(self) -> None:
        captured = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda *a: captured.append(a))
        client._check_duplicate = lambda _: False  # type: ignore
        client._add_reaction = lambda *a: None  # type: ignore
        d = type("o", (), {"event": type("o", (), {
            "message": type("o", (), {
                "message_id": "mn", "content": '{"text": "hi"}',
                "chat_id": "c1", "parent_id": None,
            })(),
            "sender": type("o", (), {"sender_id": None})(),
        })()})()
        client._on_message_wrapper(d)
        assert captured[0][0] == ""  # sender_id empty fallback

    def test_image_download_failure_text(self) -> None:
        captured = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda s, c, t, m: captured.append(t))
        client._check_duplicate = lambda _: False  # type: ignore
        client._add_reaction = lambda *a: None  # type: ignore
        client._download_image = lambda m, k: ""  # type: ignore
        d = type("o", (), {"event": type("o", (), {
            "message": type("o", (), {
                "message_id": "mi", "content": '{"image_key": "img_x"}',
                "chat_id": "c1", "parent_id": None,
            })(),
            "sender": type("o", (), {"sender_id": type("o", (), {"open_id": "o"})()}),
        })()})()
        client._on_message_wrapper(d)
        assert "下载失败" in captured[0]

    def test_content_not_dict_uses_str(self) -> None:
        captured = []
        client = FeishuClient(app_id="t", app_secret="t",
                              on_message=lambda s, c, t, m: captured.append(t))
        client._check_duplicate = lambda _: False  # type: ignore
        client._add_reaction = lambda *a: None  # type: ignore
        d = type("o", (), {"event": type("o", (), {
            "message": type("o", (), {
                "message_id": "ms", "content": '"just a string"',
                "chat_id": "c1", "parent_id": None,
            })(),
            "sender": type("o", (), {"sender_id": type("o", (), {"open_id": "o"})()}),
        })()})()
        client._on_message_wrapper(d)
        assert captured[0] == "just a string"
