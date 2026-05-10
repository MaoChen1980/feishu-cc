"""Tests for feishu_client — content detection, parsing, dedup."""

from __future__ import annotations

import time

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
    client.send_reply("chat_1", "root_1", "normal text")
    client.send_reply("chat_1", "root_1", "| h1 | h2 |\n| --- | --- |\n| a | b |")
