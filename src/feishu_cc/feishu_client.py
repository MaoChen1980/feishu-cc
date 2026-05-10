"""Feishu WebSocket client — extracted and simplified from nanobot proxy."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from loguru import logger


class FeishuClient:
    """Feishu WS client. Calls *on_message* callback with (sender_id, chat_id, text)."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        domain: str = "https://open.feishu.cn",
        render_mode: str = "card",
        react_emoji: str = "THUMBSUP",
        done_emoji: str | None = None,
        encrypt_key: str = "",
        verification_token: str = "",
        *,
        on_message: Callable[[str, str, str, str], None] | None = None,
        on_card_action: Callable[[str, str, str], None] | None = None,
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._render_mode = render_mode
        self._react_emoji = react_emoji
        self._done_emoji = done_emoji
        self._encrypt_key = encrypt_key
        self._verification_token = verification_token
        self._on_message = on_message
        self._on_card_action = on_card_action

        self._client: Any = None
        self._thread_pool = ThreadPoolExecutor(max_workers=10)
        self._dedup: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Set up Feishu WebSocket client and enter the event loop."""
        import lark_oapi as lark

        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(self._domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        handler = (
            lark.EventDispatcherHandler.builder(
                self._encrypt_key or "",
                self._verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message_wrapper)
            .register_p2_im_message_reaction_created_v1(self._noop)
            .register_p2_card_action_trigger(self._on_card_action_wrapper)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._noop)
            .register_p2_im_message_message_read_v1(self._noop)
            .build()
        )

        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            domain=self._domain,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        def run_ws() -> None:
            import lark_oapi.ws as _lark_ws

            logger.info("Feishu WS loop starting, connecting to {}...", self._domain)
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            self._loop = ws_loop
            _lark_ws.client.loop = ws_loop
            try:
                ws_client.start()
            except Exception as e:
                logger.error("Feishu WS error: {}", e)
            finally:
                ws_loop.close()
                logger.info("Feishu WS loop ended")

        thread = threading.Thread(target=run_ws, daemon=True)
        thread.start()

    # -- event handlers ------------------------------------------------------

    def _noop(self, data: Any) -> None:
        pass

    def _on_message_wrapper(self, data: Any) -> None:
        """Sync callback from Feishu SDK."""
        logger.info("Feishu on_message: {}", type(data).__name__)
        try:
            event = data.event
            message = event.message
            sender = event.sender

            message_id = getattr(message, "message_id", None)
            if not message_id or self._check_duplicate(message_id):
                return

            content = getattr(message, "content", "")
            try:
                content_obj = json.loads(content)
                text = content_obj.get("text", "") if isinstance(content_obj, dict) else str(content_obj)
            except Exception:
                text = content

            sender_id_obj = getattr(sender, "sender_id", None)
            if sender_id_obj is not None and hasattr(sender_id_obj, "open_id"):
                sender_id = sender_id_obj.open_id
            else:
                sender_id = str(sender_id_obj or "")
            chat_id = getattr(message, "chat_id", "")

            parent_id = getattr(message, "parent_id", None)
            quoted_text = ""
            if parent_id:
                quoted_text = self._fetch_quoted_message(parent_id)

            full_text = text
            if quoted_text:
                full_text = f'[引用]{quoted_text}\n---\n{text}'

            self._add_reaction(message_id, self._react_emoji)

            if self._on_message:
                self._on_message(sender_id, chat_id, full_text, message_id)

        except Exception as e:
            logger.error("Feishu message handler error: {}", e)

    def _on_card_action_wrapper(self, data: Any) -> None:
        """Handle card button click."""
        try:
            event = data.event
            operator = event.operator
            action = event.action

            value: dict = action.value if isinstance(action.value, dict) else {}
            reply_text = value.get("qr", "")
            chat_id = value.get("cid", "")
            if not reply_text or not chat_id:
                logger.warning("Card action missing qr/cid: {}", value)
                return

            sender_id = operator.open_id if operator else ""
            if not sender_id:
                logger.warning("Card action missing open_id in operator")
                return

            unique_id = f"card_{action.name or ''}_{int(time.time())}"
            if self._check_duplicate(unique_id):
                return

            logger.info("Card action: {} -> {} (from {})", reply_text[:50], chat_id, sender_id)

            if self._on_card_action:
                self._on_card_action(reply_text, chat_id, sender_id)

        except Exception as e:
            logger.error("Failed to handle card action: {}", e)

    # -- fetch quoted message ------------------------------------------------

    def _fetch_quoted_message(self, message_id: str) -> str:
        try:
            from lark_oapi.api.im.v1 import GetMessageRequest

            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if response.success():
                items = response.data.items
                if items:
                    content_str = items[0].body.content
                    obj = json.loads(content_str)
                    if isinstance(obj, dict):
                        return obj.get("text", "") or obj.get("content", "") or str(obj)
                    return str(obj)
        except Exception as e:
            logger.debug("Failed to fetch quoted message {}: {}", message_id, e)
        return ""

    # -- deduplication -------------------------------------------------------

    def _check_duplicate(self, msg_id: str, ttl: int = 300) -> bool:
        now = time.time()
        if msg_id in self._dedup and now - self._dedup[msg_id] < ttl:
            return True
        self._dedup[msg_id] = now
        if len(self._dedup) > 1000:
            cutoff = now - max(ttl, 300)
            self._dedup = {k: v for k, v in self._dedup.items() if v > cutoff}
        return False

    # -- content detection ---------------------------------------------------

    @staticmethod
    def _has_rich_content(text: str) -> bool:
        if "```" in text:
            return True
        return bool(re.search(r'\|.+\|\r?\n\|[-:| ]+\|', text))

    @staticmethod
    def _extract_header(content: str) -> tuple[str | None, str]:
        lines = content.split("\n")
        for i, line in enumerate(lines[:10]):
            stripped = line.strip()
            if stripped:
                m = re.match(r"^#\s+(.+)$", stripped)
                if m:
                    body = "\n".join(lines[i + 1:]).strip()
                    return m.group(1), body
                break
        return None, content

    @staticmethod
    def _parse_quick_replies(content: str) -> tuple[str, list[dict[str, str]] | None]:
        marker = "---quick-replies"
        if marker not in content:
            return content, None
        before, section = content.split(marker, 1)
        cleaned = before.strip()
        quick_replies: list[dict[str, str]] = []
        for line in section.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "||" in line:
                label, reply = line.split("||", 1)
                quick_replies.append({"label": label.strip(), "reply": reply.strip()})
            else:
                quick_replies.append({"label": line, "reply": line})
        return cleaned, quick_replies or None

    @staticmethod
    def _wrap_tables_in_code_fences(content: str) -> str:
        lines = content.split("\n")
        result: list[str] = []
        table_lines: list[str] = []
        in_table = False
        for line in lines:
            stripped = line.strip()
            is_table = stripped.startswith("|") and stripped.endswith("|")
            if is_table:
                if not in_table:
                    in_table = True
                    table_lines = [line]
                else:
                    table_lines.append(line)
            else:
                if in_table:
                    if len(table_lines) > 2:
                        result.append("```")
                        result.extend(table_lines)
                        result.append("```")
                    else:
                        result.extend(table_lines)
                    in_table = False
                    table_lines = []
                result.append(line)
        if in_table:
            if len(table_lines) > 2:
                result.append("```")
                result.extend(table_lines)
                result.append("```")
            else:
                result.extend(table_lines)
        return "\n".join(result)

    # -- send reply ----------------------------------------------------------

    def send_reply(self, chat_id: str, root_id: str | None, content: str) -> None:
        cleaned, qrs = self._parse_quick_replies(content)
        use_card = qrs is not None or self._render_mode == "card" or (
            self._render_mode == "auto" and self._has_rich_content(cleaned)
        )
        if use_card:
            if self._send_card_reply(chat_id, cleaned, quick_replies=qrs):
                return
        processed = self._wrap_tables_in_code_fences(cleaned)
        if self._send_post_reply(chat_id, processed):
            return
        self._send_plain_text(chat_id, processed)

    def send_permission_card(self, chat_id: str, prompt: str, request_id: str) -> None:
        """Send a permission request card with allow/deny buttons."""
        elements = [
            {"tag": "markdown", "content": f"**Claude 需要权限**\n{prompt}"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "允许"},
                "type": "primary",
                "behaviors": [{
                    "type": "callback",
                    "value": {"qr": f"__perm_allow__:{request_id}", "cid": chat_id},
                }],
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "拒绝"},
                "type": "danger",
                "behaviors": [{
                    "type": "callback",
                    "value": {"qr": f"__perm_deny__:{request_id}", "cid": chat_id},
                }],
            },
        ]
        card = {"schema": "2.0", "config": {"width_mode": "fill"}, "body": {"elements": elements}}
        self._send_card(card, chat_id)

    # -- send implementations ------------------------------------------------

    def _send_card_reply(
        self, chat_id: str, content: str,
        quick_replies: list[dict[str, str]] | None = None,
    ) -> bool:
        try:
            header_text, body = self._extract_header(content)
            elements: list[dict[str, Any]] = [
                {"tag": "markdown", "content": body or content},
            ]
            if quick_replies:
                for qr in quick_replies:
                    elements.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": qr["label"]},
                        "type": "default",
                        "behaviors": [{"type": "callback", "value": {"qr": qr["reply"], "cid": chat_id}}],
                    })
            card: dict[str, Any] = {
                "schema": "2.0",
                "config": {"width_mode": "fill"},
                "body": {"elements": elements},
            }
            if header_text:
                card["header"] = {
                    "title": {"tag": "plain_text", "content": header_text},
                    "template": "blue",
                }
            return self._send_card(card, chat_id)
        except Exception as e:
            logger.error("Card send exception: {}", e)
            return False

    def _send_card(self, card: dict[str, Any], chat_id: str) -> bool:
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(json.dumps(card))
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.message.create(request)
            if resp.success():
                return True
            logger.warning("Card send failed ({}): {}", resp.code, resp.msg)
        except Exception as e:
            logger.error("Card send exception: {}", e)
        return False

    def _send_post_reply(self, chat_id: str, content: str) -> bool:
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            payload = {"zh_cn": {"content": [[{"tag": "md", "text": content}]]}}
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("post")
                    .content(json.dumps(payload))
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.message.create(request)
            if resp.success():
                return True
            logger.warning("Post send failed ({}): {}", resp.code, resp.msg)
        except Exception as e:
            logger.error("Post send exception: {}", e)
        return False

    def _send_plain_text(self, chat_id: str, content: str) -> None:
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": content}))
                    .build()
                )
                .build()
            )
            self._client.im.v1.message.create(request)
        except Exception as e:
            logger.error("Plain-text fallback failed: {}", e)

    # -- reactions -----------------------------------------------------------

    def _add_reaction(self, message_id: str, emoji: str) -> None:
        if not emoji:
            return
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageReactionRequest, CreateMessageReactionRequestBody, Emoji,
            )
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji).build())
                    .build()
                )
                .build()
            )
            self._client.im.v1.message_reaction.create(request)
        except Exception as e:
            logger.debug("Failed to add reaction: {}", e)

    def _remove_reaction(self, message_id: str) -> None:
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

            request = DeleteMessageReactionRequest.builder().message_id(message_id).build()
            self._client.im.v1.message_reaction.delete(request)
        except Exception as e:
            logger.debug("Failed to remove reaction: {}", e)
