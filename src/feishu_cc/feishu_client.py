"""Feishu WebSocket client — extracted and simplified from nanobot proxy."""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
import time
from typing import Any, Callable, Optional

from requests.exceptions import RequestException

from loguru import logger

from feishu_cc.config import CONFIG_DIR


class FeishuClient:
    """Feishu WS client. Calls *on_message* callback with (sender_id, chat_id, text)."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        domain: str = "https://open.feishu.cn",
        render_mode: str = "card",
        react_emoji: str = "THUMBSUP",
        done_emoji: Optional[str] = None,
        encrypt_key: str = "",
        verification_token: str = "",
        *,
        on_message: Optional[Callable[[str, str, str, str], None]] = None,
        on_card_action: Optional[Callable[[str, str, str], None]] = None,
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
        self._dedup: dict[str, float] = {}
        self._clicked_buttons: dict[str, str] = {}
        self._card_seq: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Set up Feishu WebSocket client and enter the event loop."""
        self._cleanup_temp()

        import lark_oapi as lark

        self._client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(self._domain)
            .timeout(30.0)
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
            ws_client.start()
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
        event = data.event
        message = event.message
        sender = event.sender

        message_id = getattr(message, "message_id", None)
        if not message_id or self._check_duplicate(message_id):
            return

        content = getattr(message, "content", "")
        logger.info("Raw message content: {}", content)
        content_obj = json.loads(content)
        if isinstance(content_obj, dict):
            image_key = content_obj.get("image_key", "")
            if image_key:
                logger.info("Detected image with key: {}", image_key)
                local_path = self._download_image(message_id, image_key)
                if local_path:
                    text = f"用户发送了一张图片，文件路径：{local_path}"
                else:
                    logger.warning("Image download returned empty, check previous warning for details")
                    text = "用户发送了一张图片，但下载失败"
            elif content_obj.get("file_key"):
                file_key = content_obj["file_key"]
                logger.info("Detected file with key: {}", file_key)
                local_path = self._download_file(message_id, file_key)
                if local_path:
                    text = f"用户发送了一个文件，文件路径：{local_path}"
                else:
                    logger.warning("File download returned empty")
                    text = "用户发送了一个文件，但下载失败"
            else:
                text = content_obj.get("text", "")
        else:
            text = str(content_obj)

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
            try:
                self._on_message(sender_id, chat_id, full_text, message_id)
            except Exception:
                logger.exception("on_message callback failed")

    def _on_card_action_wrapper(self, data: Any) -> None:
        """Handle card button click."""
        logger.debug("Card action raw data: {}", data)
        event = data.event
        operator = event.operator
        action = event.action

        value: dict = action.value if isinstance(action.value, dict) else {}
        reply_text = value.get("qr", "")
        chat_id = value.get("cid", "")
        label = value.get("label", reply_text)
        card_id = value.get("card_id", "")
        if not reply_text or not chat_id:
            logger.warning("Card action missing qr/cid: {}", value)
            return

        sender_id = operator.open_id if operator else ""
        if not sender_id:
            logger.warning("Card action missing open_id in operator")
            return

        # Dedup per-card: same button on a different card is allowed
        button_key = f"{chat_id}:{card_id}:{reply_text}"
        prev_label = self._clicked_buttons.get(button_key)
        if prev_label is not None:
            logger.info("Card button already clicked: {} -> {}", button_key, prev_label)
            self.send_plain_text(chat_id, f"您已经选择了「{prev_label}」")
            return
        self._clicked_buttons[button_key] = label

        unique_id = f"card_{action.name or ''}_{int(time.time())}"
        if self._check_duplicate(unique_id):
            logger.info("Card action duplicate, skipped: {}", unique_id)
            return

        logger.info("Card action: {} -> {} (from {})", reply_text[:50], chat_id, sender_id)

        if self._on_card_action:
            try:
                self._on_card_action(reply_text, chat_id, sender_id)
            except Exception:
                logger.exception("on_card_action callback failed")

    # -- image handling -------------------------------------------------------

    @staticmethod
    def _cleanup_temp() -> None:
        """Remove temp files older than 24 hours."""
        temp_dir = CONFIG_DIR / "temp"
        if not temp_dir.exists():
            return
        cutoff = time.time() - 86400
        for f in temp_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                logger.debug("Cleaned up temp file: {}", f)

    def _download_image(self, message_id: str, image_key: str) -> str:
        """Download image from a Feishu message resource, save to temp dir."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()
        try:
            response = self._http_retry(lambda: self._client.im.v1.message_resource.get(request))
        except RequestException:
            logger.exception("Failed to download image {}", image_key)
            return ""
        if response.code != 0 or not response.file:
            status = response.raw.status_code if response.raw else "N/A"
            logger.warning("Failed to download image {}: code={}, msg={}, http_status={}",
                           image_key, response.code, response.msg, status)
            return ""

        data = response.file.read()

        # Detect image format from magic bytes
        ext = "jpg"
        if data[:4] == b"\x89PNG":
            ext = "png"
        elif data[:2] in (b"\xff\xd8",):
            ext = "jpg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ext = "gif"

        temp_dir = CONFIG_DIR / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / f"{image_key}.{ext}"
        with open(path, "wb") as f:
            f.write(data)
        logger.info("Image {} saved to {} ({}b, {})", image_key, path, len(data), ext)
        return str(path)

    def _download_file(self, message_id: str, file_key: str) -> str:
        """Download a file from a Feishu message resource, save to temp dir."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()
        try:
            response = self._http_retry(lambda: self._client.im.v1.message_resource.get(request))
        except RequestException:
            logger.exception("Failed to download file {}", file_key)
            return ""
        if response.code != 0 or not response.file:
            status = response.raw.status_code if response.raw else "N/A"
            logger.warning("Failed to download file {}: code={}, msg={}, http_status={}",
                           file_key, response.code, response.msg, status)
            return ""

        data = response.file.read()
        temp_dir = CONFIG_DIR / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / file_key
        with open(path, "wb") as f:
            f.write(data)
        logger.info("File {} saved to {} ({}b)", file_key, path, len(data))
        return str(path)

    # -- fetch quoted message ------------------------------------------------

    def _fetch_quoted_message(self, message_id: str) -> str:
        from lark_oapi.api.im.v1 import GetMessageRequest

        request = GetMessageRequest.builder().message_id(message_id).build()
        try:
            response = self._http_retry(lambda: self._client.im.v1.message.get(request))
        except RequestException:
            logger.exception("Failed to fetch quoted message {}", message_id)
            return ""
        if response.success():
            items = response.data.items
            if items:
                content_str = items[0].body.content
                obj = json.loads(content_str)
                if isinstance(obj, dict):
                    return obj.get("text", "") or obj.get("content", "") or str(obj)
                return str(obj)
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
    def _extract_header(content: str) -> tuple[Optional[str], str]:
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
    def _parse_quick_replies(content: str) -> tuple[str, Optional[list[dict[str, str]]]]:
        marker = "---quick-replies"
        idx = content.rfind(marker)
        if idx == -1:
            return content, None
        before = content[:idx]
        section = content[idx + len(marker):]
        cleaned = before.strip()
        quick_replies: list[dict[str, str]] = []
        for line in section.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "||" in line:
                label, reply = line.split("||", 1)
                quick_replies.append({"label": label.strip(), "reply": reply.strip()})
            elif "|" in line:
                for item in line.split("|"):
                    item = item.strip()
                    if item:
                        quick_replies.append({"label": item, "reply": item})
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

    # -- HTTP retry ----------------------------------------------------------

    @staticmethod
    def _http_retry(request_fn, max_retries=2, base_delay=1.0):
        """Execute HTTP request with exponential backoff on network errors."""
        for attempt in range(max_retries + 1):
            try:
                return request_fn()
            except RequestException as e:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning("HTTP request failed, retry {}/{} in {:.1f}s: {}",
                                   attempt + 1, max_retries, delay, e)
                    time.sleep(delay)
                else:
                    logger.error("HTTP request failed after {} retries: {}", max_retries, e)
                    raise

    # -- send reply ----------------------------------------------------------

    def send_text(self, chat_id: str, text: str) -> None:
        """Send a plain text message — no card/post processing, for notifications."""
        self.send_plain_text(chat_id, text)

    def send_reply(self, chat_id: str, root_id: Optional[str], content: str) -> None:
        cleaned, qrs = self._parse_quick_replies(content)
        has_table = self._has_rich_content(cleaned)
        use_card = qrs is not None or self._render_mode == "card" or (
            self._render_mode == "auto" and has_table
        )

        if use_card:
            if self._send_card_reply(chat_id, cleaned, quick_replies=qrs):
                return

        processed = self._wrap_tables_in_code_fences(cleaned)
        if self._send_post_reply(chat_id, processed):
            return
        self.send_plain_text(chat_id, processed)

    @staticmethod
    def _format_permission_value(value: dict, indent: int = 0) -> str:
        """递归格式化权限 value dict 为可读文本，处理嵌套结构。"""
        lines: list[str] = []
        prefix = "  " * indent
        for k, v in value.items():
            if isinstance(v, dict):
                # 如果已有 tool_name + tool_args，跳过冗余的 tool_use
                if k == "tool_use" and "tool_name" in value:
                    continue
                nested = FeishuClient._format_permission_value(v, indent + 1)
                lines.append(f"{prefix}**{k}**:")
                lines.append(nested)
            elif isinstance(v, list):
                items = "\n".join(
                    f"{prefix}  - `{i}`" for i in v if isinstance(i, str)
                )
                lines.append(f"{prefix}**{k}**:\n{items}" if items else f"{prefix}**{k}**: `{v}`")
            else:
                lines.append(f"{prefix}**{k}**: `{v}`")
        return "\n".join(lines)

    def send_permission_card(self, chat_id: str, prompt: str, request_id: str,
                             value: Optional[dict] = None) -> None:
        """Send a permission request card with allow_once/allow_this_time/deny buttons."""
        logger.debug("[{}] Permission value: {}", chat_id, value)

        display_text = ""
        tool_name = ""
        tool_args: dict = {}
        if value:
            display_text = (
                value.get("permission_prompt")
                or value.get("prompt")
                or ""
            )
            tool_name = value.get("tool_name", "")
            tool_args = value.get("tool_args", {}) or {}

        desc = ""
        if display_text:
            desc = display_text
        if tool_args:
            arg_lines: list[str] = []
            for k, v in tool_args.items():
                if isinstance(v, (dict, list)):
                    v = str(v)
                arg_lines.append(f"**{k}**: `{v}`")
            args_block = "\n".join(arg_lines)
            desc = f"{desc}\n\n{args_block}" if desc else args_block
        if not desc:
            details = FeishuClient._format_permission_value(value or {})
            desc = details or prompt

        content = f"# {tool_name}\n\n{desc}" if tool_name else f"# 权限请求\n\n{desc}"

        quick_replies = [
            {"label": "允许本次", "reply": f"__perm_allow_once__:{request_id}"},
            {"label": "始终允许", "reply": f"__perm_allow__:{request_id}"},
            {"label": "拒绝", "reply": f"__perm_deny__:{request_id}"},
        ]
        self._send_card_reply(chat_id, content, quick_replies=quick_replies)

    # -- send implementations ------------------------------------------------

    def _send_card_reply(
        self, chat_id: str, content: str,
        quick_replies: list[dict[str, str]] | None = None,
    ) -> bool:
        self._card_seq += 1
        card_id = str(self._card_seq)
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
                    "value": {"qr": qr["reply"], "cid": chat_id, "label": qr["label"], "card_id": card_id},
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

    def _send_card(self, card: dict[str, Any], chat_id: str) -> bool:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        payload = json.dumps(card)
        logger.debug("Sending card to {}: {}", chat_id, payload)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(payload)
                .build()
            )
            .build()
        )
        try:
            resp = self._http_retry(lambda: self._client.im.v1.message.create(request))
            if resp.success():
                return True
            logger.warning("Card send failed ({}): {}", resp.code, resp.msg)
        except RequestException:
            logger.exception("Failed to send card to {}", chat_id)
        return False

    def _send_post_reply(self, chat_id: str, content: str) -> bool:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        payload = {"zh_cn": {"content": [[{"tag": "md", "text": content}]]}}
        payload_str = json.dumps(payload)
        logger.debug("Sending post to {}: {}", chat_id, payload_str)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("post")
                .content(payload_str)
                .build()
            )
            .build()
        )
        try:
            resp = self._http_retry(lambda: self._client.im.v1.message.create(request))
            if resp.success():
                return True
            logger.warning("Post send failed ({}): {}", resp.code, resp.msg)
        except RequestException:
            logger.exception("Failed to send post to {}", chat_id)
        return False

    def send_plain_text(self, chat_id: str, content: str) -> None:
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
        logger.debug("Sending plain text to {}: {}", chat_id, content)
        try:
            resp = self._http_retry(lambda: self._client.im.v1.message.create(request))
            if resp.code != 0:
                logger.warning("Send plain text failed ({}): {}", resp.code, resp.msg)
        except RequestException:
            logger.exception("Failed to send plain text to {}", chat_id)

    # -- reactions -----------------------------------------------------------

    def _add_reaction(self, message_id: str, emoji: str) -> None:
        if not emoji:
            return
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
        try:
            self._http_retry(lambda: self._client.im.v1.message_reaction.create(request))
        except RequestException:
            logger.exception("Failed to add reaction to {}", message_id)

    def _remove_reaction(self, message_id: str) -> None:
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        request = DeleteMessageReactionRequest.builder().message_id(message_id).build()
        try:
            self._client.im.v1.message_reaction.delete(request)
        except RequestException:
            logger.exception("Failed to remove reaction from {}", message_id)
