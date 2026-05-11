"""feishu-cc main application — wires Feishu WS to Claude Code subprocess."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from loguru import logger

from feishu_cc.claude_bridge import ClaudeBridge, _RESPONSE_TIMEOUT
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

    async def _shutdown_async(self):
        """Stop bridge (terminates Claude subprocess) on the event loop."""
        await self.bridge.stop()
        # Clean up stale session so next start is fresh
        if self.bridge._session_file.exists():
            self.bridge._session_file.unlink()

    def stop_loop(self) -> None:
        """Stop bridge, then stop the event loop."""
        if self.loop:
            # Run bridge shutdown on the event loop before stopping it
            fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self.loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass
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
        self._restart_args = sys.argv

    def run(self) -> None:
        """Start all bots and block forever."""
        self._launch_dir = os.getcwd()
        self._startup_time = time.strftime("%Y-%m-%d %H:%M:%S")
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
                workspace=bot_cfg.workspace or self._launch_dir,
                system_prompt=bot_cfg.system_prompt,
                on_permission_request=lambda req_id, prompt, val: (
                    self._on_permission(feishu, bot_cfg.name, prompt, val, req_id, current_chat[0])
                ),
                on_text=lambda text: (
                    feishu.send_text(current_chat[0], text)
                    if current_chat[0] else None
                ),
                on_thinking=lambda thinking: (
                    feishu.send_text(current_chat[0], f"💭 {thinking}")
                    if current_chat[0] and thinking.strip() else None
                ),
                on_tool_use=lambda name, brief: (
                    self._on_tool_notify(feishu, current_chat[0], name, brief)
                    if current_chat[0] else None
                ),
                on_task_summary=lambda summary: (
                    feishu.send_text(current_chat[0], f"✅ {summary}")
                    if current_chat[0] else None
                ),
                on_system_notify=lambda summary, status: (
                    feishu.send_text(current_chat[0], f"✅ [{status}] {summary}")
                    if current_chat[0] else None
                ),
                on_error=lambda err_type, err_msg: (
                    feishu.send_text(current_chat[0], f"❌ {err_type}: {err_msg}")
                    if current_chat[0] else None
                ),
                on_tool_result=lambda content, is_error: (
                    feishu.send_text(current_chat[0], f"{'❌' if is_error else '📊'} {content[:100]}{'…' if len(content) > 100 else ''}")
                    if current_chat[0] else None
                ),
                on_result_content=lambda content: (
                    feishu.send_text(current_chat[0], FeishuCCApp._format_result_content(content))
                    if current_chat[0] else None
                ),
            )

            bridge._pending_startup_info = True

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

        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            for rt in reversed(self._bots):
                rt.stop_loop()
            logger.info("Goodbye.")

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _set_chat(current_chat: list[Optional[str]], chat_id: str) -> None:
        current_chat[0] = chat_id

    _TOOL_EMOJI = {
        "Read": "📖",
        "Write": "📝",
        "Edit": "✏️",
        "Glob": "📁",
        "Grep": "🔍",
        "Bash": "💻",
        "Agent": "🤖",
        "TodoWrite": "📋",
        "WebSearch": "🌐",
        "WebFetch": "🌐",
        "AskUserQuestion": "❓",
        "Skill": "⚡",
    }

    @staticmethod
    def _on_tool_notify(feishu: FeishuClient, chat_id: str, name: str, brief: str) -> None:
        """Send a real-time tool_use notification to Feishu."""
        emoji = FeishuCCApp._TOOL_EMOJI.get(name, "🛠️")
        text = f"{emoji} {name}"
        if brief:
            text += f" {brief}"
        feishu.send_text(chat_id, text)

    @staticmethod
    def _format_result_content(content: list[dict[str, Any]]) -> str:
        """Extract a readable summary from result event content blocks."""
        texts = []
        for block in content:
            t = block.get("type", "")
            if t == "text":
                txt = block.get("text", "")
                if txt:
                    texts.append(txt[:80])
            elif t == "tool_use":
                texts.append(f"[{block.get('name', '?')}]")
            elif t == "thinking":
                texts.append("💭 …")
        if not texts:
            return "✅ 完成"
        summary = " | ".join(texts[:5])
        if len(texts) > 5:
            summary += f" … +{len(texts) - 5} 项"
        return f"✅ {summary}"

    # -- message routing -----------------------------------------------------

    def _on_message(self, bot_name: str, bridge: ClaudeBridge, feishu: FeishuClient,
                    chat_id: str, text: str, message_id: str) -> None:
        """Handle incoming Feishu message → send to Claude → reply."""
        logger.info("[{}] Message from {}: {}", bot_name, chat_id, text[:80])

        # Send startup info on first message
        if getattr(bridge, '_pending_startup_info', False):
            lines = [
                f"🚀 feishu-cc 已启动",
                f"🕐 {self._startup_time}",
                f"📂 启动目录: {self._launch_dir}",
            ]
            if bridge._startup_ws_warning:
                lines.append(f"⚠️ 配置的工作目录不存在：{bridge._startup_ws_warning}，已使用启动目录")
                bridge._startup_ws_warning = None
            else:
                lines.append(f"🔧 Workspace: {bridge._workspace}")
            feishu.send_text(chat_id, "\n".join(lines))
            bridge._pending_startup_info = False

        rt = _find_runtime(self._bots, bridge)
        if not rt:
            logger.error("[{}] Runtime not found", bot_name)
            return

        if text.strip() == "/restart":
            feishu.send_reply(chat_id, message_id, "🔄 feishu-cc 重启中...")
            self._restart_app()
            return

        if text.startswith("/workspace "):
            new_workspace = text[len("/workspace "):].strip()
            if new_workspace:
                if not os.path.isdir(new_workspace):
                    feishu.send_reply(chat_id, message_id, f"❌ 目录不存在：{new_workspace}")
                    return
                async def _restart():
                    try:
                        await bridge.restart(new_workspace)
                        feishu.send_reply(chat_id, message_id, f"工作目录已切换到：{new_workspace}")
                    except Exception:
                        logger.exception("[{}] Failed to switch workspace for {}", bot_name, chat_id)
                        feishu.send_plain_text(chat_id, "❌ 工作目录切换失败")
                rt.schedule(_restart())
            return

        async def _handle():
            try:
                response = await bridge.send_message(text)
                logger.info("[{}] Reply to {} ({} chars): {}", bot_name, chat_id, len(response), response)
                if response:
                    feishu.send_reply(chat_id, message_id, response)
                if message_id:
                    if self._config.done_emoji:
                        feishu._add_reaction(message_id, self._config.done_emoji)
                    feishu._remove_reaction(message_id)
                feishu.send_plain_text(chat_id, "✅ 空闲")
            except asyncio.TimeoutError:
                logger.warning("[{}] Timeout processing message from {} ({}s)", bot_name, chat_id, _RESPONSE_TIMEOUT)
                bridge._init_failed = True
                feishu.send_plain_text(chat_id, "⏰ 处理超时，请重试")
            except ConnectionError:
                logger.warning("[{}] Connection lost to Claude for {}", bot_name, chat_id)
                feishu.send_plain_text(chat_id, "🔄 Claude 连接断开，正在自动重连，请稍后重试")
            except Exception:
                logger.exception("[{}] Unhandled error processing message from {}", bot_name, chat_id)
                feishu.send_plain_text(chat_id, "❌ 处理消息时出错，请重试")

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

    def _restart_app(self) -> None:
        """Restart the entire feishu-cc process via os.execv."""
        logger.info("Restarting feishu-cc...")
        for rt in reversed(self._bots):
            try:
                rt.stop_loop()
            except Exception:
                logger.exception("[{}] Error stopping bot during restart", rt.name)
        try:
            os.execv(sys.executable, [sys.executable, "-m", "feishu_cc"] + self._restart_args[1:])
        except OSError:
            logger.exception("Failed to exec, falling back to exit")
            os._exit(1)

    def _on_permission(self, feishu: FeishuClient, bot_name: str,
                       prompt: str, value: dict, request_id: str, chat_id: Optional[str]) -> None:
        """Send permission request to Feishu user."""
        logger.info("[{}] Permission requested: {} {}", bot_name, prompt, value)
        if chat_id:
            feishu.send_permission_card(chat_id, prompt, request_id, value)
        else:
            logger.warning("[{}] No chat_id for permission request", bot_name)


def _find_runtime(bots: list[_BotRuntime], bridge: ClaudeBridge) -> _BotRuntime | None:
    """Find the runtime that owns this bridge."""
    for rt in bots:
        if rt.bridge is bridge:
            return rt
    return None
