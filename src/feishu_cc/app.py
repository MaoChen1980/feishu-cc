"""feishu-cc main application — wires Feishu WS to Claude Code subprocess."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Optional, Union

from loguru import logger

from feishu_cc.claude_bridge import ClaudeBridge
from feishu_cc.config import Config
from feishu_cc.feishu_client import FeishuClient


class _BotRuntime:
    """State for a single bot — owns its own asyncio loop + threads."""

    def __init__(self, name: str, feishu: FeishuClient, bridge: ClaudeBridge):
        self.name = name
        self.feishu = feishu
        self.bridge = bridge
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: threading.Thread | None = None

    def start_loop(self) -> None:
        """Run the asyncio event loop in a background thread."""
        self.loop = asyncio.new_event_loop()

        async def _init():
            await self.bridge.start()

        self.loop.run_until_complete(_init())

        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def stop_loop(self) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread:
            self._thread.join(timeout=3)

    def run_async(self, coro):
        """Schedule a coroutine on this bot's event loop and wait for the result,
        bridging from a synchronous (Feishu callback) thread.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result()

    def schedule(self, coro) -> None:
        """Fire-and-forget a coroutine on this bot's event loop."""
        asyncio.run_coroutine_threadsafe(coro, self.loop)


class FeishuCCApp:
    """Main application. Wires Feishu WS → Claude Bridge for each bot."""

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        self._config = Config.load(config_path)
        self._bots: list[_BotRuntime] = []

    def run(self) -> None:
        """Start all bots and block forever."""
        launch_dir = os.getcwd()
        for bot_cfg in self._config.bots:
            domain = (
                "https://open.feishu.cn"
                if self._config.domain == "feishu"
                else "https://open.larksuite.com"
            )

            # Track current chat on the bridge so permission callbacks
            # know where to send the permission card.
            current_chat: list[Optional[str]] = [None]

            bridge = ClaudeBridge(
                bot_name=bot_cfg.name,
                claude_path=self._config.claude_path,
                workspace=bot_cfg.workspace or launch_dir,
                system_prompt=bot_cfg.system_prompt,
                on_permission_request=lambda req_id, prompt, val: (
                    self._on_permission(feishu, bot_cfg.name, prompt, req_id, current_chat[0])
                ),
            )

            feishu = FeishuClient(
                app_id=bot_cfg.app_id,
                app_secret=bot_cfg.app_secret,
                domain=domain,
                render_mode=self._config.render_mode,
                react_emoji=self._config.react_emoji,
                done_emoji=self._config.done_emoji,
                on_message=lambda s, c, t, mid: (
                    self._set_chat(current_chat, c),
                    self._on_message(bot_cfg.name, bridge, feishu, c, t, mid),
                ),
                on_card_action=lambda r, c, s: self._on_card_action(bot_cfg.name, bridge, feishu, c, r, s),
            )

            rt = _BotRuntime(name=bot_cfg.name, feishu=feishu, bridge=bridge)
            self._bots.append(rt)
            logger.info("[{}] Bot initialized", bot_cfg.name)

        # Start all bot loops (launches Claude subprocess async)
        for rt in self._bots:
            rt.start_loop()

        # Start Feishu WS clients (each runs WS in its own thread)
        for rt in self._bots:
            rt.feishu.start()

        logger.info("All bots started — waiting for messages...")

        while True:
            time.sleep(10)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _set_chat(current_chat: list[Optional[str]], chat_id: str) -> None:
        current_chat[0] = chat_id

    # -- message routing -----------------------------------------------------

    def _on_message(self, bot_name: str, bridge: ClaudeBridge, feishu: FeishuClient,
                    chat_id: str, text: str, message_id: str) -> None:
        """Handle incoming Feishu message → send to Claude → reply."""
        logger.info("[{}] Message from {}: {}", bot_name, chat_id, text[:80])

        rt = _find_runtime(self._bots, bridge)
        if not rt:
            logger.error("[{}] Runtime not found", bot_name)
            return

        if text.startswith("/workspace "):
            new_workspace = text[len("/workspace "):].strip()
            if new_workspace:
                async def _restart():
                    await bridge.restart(new_workspace)
                    feishu.send_reply(chat_id, message_id, f"工作目录已切换到：{new_workspace}")
                rt.schedule(_restart())
            return

        async def _handle():
            response = await bridge.send_message(text)
            logger.info("[{}] Reply to {} ({} chars): {}", bot_name, chat_id, len(response), response)
            if response:
                feishu.send_reply(chat_id, message_id, response)
            if self._config.done_emoji:
                feishu._add_reaction(message_id, self._config.done_emoji)
            feishu._remove_reaction(message_id)

        rt.schedule(_handle())

    def _on_card_action(self, bot_name: str, bridge: ClaudeBridge, feishu: FeishuClient,
                        chat_id: str, reply_text: str, sender_id: str) -> None:
        """Handle card button click — permission response or normal reply."""
        rt = _find_runtime(self._bots, bridge)
        if not rt:
            logger.error("[{}] Runtime not found", bot_name)
            return

        if reply_text.startswith("__perm_allow__:"):
            request_id = reply_text.split(":", 1)[1]
            rt.schedule(bridge.respond_permission(request_id, "allow"))
            return
        if reply_text.startswith("__perm_deny__:"):
            request_id = reply_text.split(":", 1)[1]
            rt.schedule(bridge.respond_permission(request_id, "deny"))
            return

        # Normal card reply → forward to Claude as user message
        self._on_message(bot_name, bridge, feishu, chat_id, reply_text, "")

    def _on_permission(self, feishu: FeishuClient, bot_name: str,
                       prompt: str, request_id: str, chat_id: Optional[str]) -> None:
        """Send permission request to Feishu user."""
        logger.info("[{}] Permission requested: {}", bot_name, prompt)
        if chat_id:
            feishu.send_permission_card(chat_id, prompt, request_id)
        else:
            logger.warning("[{}] No chat_id for permission request", bot_name)


def _find_runtime(bots: list[_BotRuntime], bridge: ClaudeBridge) -> _BotRuntime | None:
    """Find the runtime that owns this bridge."""
    for rt in bots:
        if rt.bridge is bridge:
            return rt
    return None
