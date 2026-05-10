"""Claude Code subprocess management via JSON stream protocol."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from feishu_cc.config import CONFIG_DIR

# Default system prompt injected into Claude for Feishu context
DEFAULT_SYSTEM_PROMPT = """\
你通过飞书与用户对话。回复可以使用 `---quick-replies` 提供一键按钮。
不要截断你的回复，用户需要看到完整内容。
表格使用 markdown 格式即可。\
"""


class ClaudeBridge:
    """Manages a Claude Code subprocess via JSON stream protocol.

    Uses ``--output-format stream-json --input-format stream-json``
    for structured JSON communication over stdin/stdout.
    """

    def __init__(
        self,
        bot_name: str,
        claude_path: str = "claude",
        workspace: str | None = None,
        system_prompt: str | None = None,
        *,
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_permission_request: Callable[[str, str, dict], None] | None = None,
    ):
        self._bot_name = bot_name
        self._claude_path = claude_path
        self._workspace = workspace
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_permission_request = on_permission_request

        self._process: asyncio.subprocess.Process | None = None
        self._session_id: str | None = None
        self._response_text: str = ""
        self._response_done = asyncio.Event()
        self._running = False
        self._session_file = CONFIG_DIR / "sessions" / f"{bot_name}.session"

    # -- lifecycle -----------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def start(self) -> None:
        """Spawn the Claude Code subprocess."""
        resume_id = self._load_session()
        args = [
            self._claude_path,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--permission-prompt-tool", "stdio",
            "--append-system-prompt", self._system_prompt,
        ]
        if resume_id:
            args.extend(["--resume", resume_id])
            logger.info("[{}] Resuming session {}", self._bot_name, resume_id)

        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace or os.getcwd(),
        )
        self._running = True
        logger.info("[{}] Claude subprocess started (pid={})", self._bot_name, self._process.pid)

        # Start stdout reader and stderr logger in background
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

    async def stop(self) -> None:
        """Terminate the Claude subprocess."""
        self._running = False
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    # -- send ----------------------------------------------------------------

    async def send_message(self, text: str) -> str:
        """Send a user message to Claude and wait for the complete response.

        Returns the accumulated response text.
        """
        self._response_text = ""
        self._response_done.clear()

        msg = {"type": "user", "message": {"role": "user", "content": text}}
        await self._write_json(msg)
        logger.debug("[{}] Sent user message", self._bot_name)

        await self._response_done.wait()
        return self._response_text

    async def respond_permission(self, request_id: str, behavior: str) -> None:
        """Respond to a permission request (allow/deny)."""
        msg = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {"behavior": behavior},
            },
        }
        await self._write_json(msg)
        logger.info("[{}] Permission {} for request {}", self._bot_name, behavior, request_id)

    # -- internal: stdout reader --------------------------------------------

    async def _read_stdout(self) -> None:
        """Read and parse JSON events from Claude's stdout."""
        try:
            async for line in self._process.stdout:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[{}] Non-JSON stdout: {}", self._bot_name, line[:100])
                    continue

                await self._handle_event(event)
        except Exception as e:
            logger.error("[{}] Stdout reader error: {}", self._bot_name, e)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a single JSON event from Claude."""
        event_type = event.get("type", "")

        if event_type == "system":
            sid = event.get("session_id", "")
            if sid:
                self._session_id = sid
                self._save_session(sid)
                logger.info("[{}] Session established: {}", self._bot_name, sid)

        elif event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "")
                    self._response_text += text
                    if self._on_text:
                        self._on_text(text)
                elif block_type == "thinking":
                    thinking = block.get("thinking", "")
                    if self._on_thinking:
                        self._on_thinking(thinking)

        elif event_type == "user":
            pass

        elif event_type == "result":
            if event.get("done"):
                logger.debug("[{}] Response complete", self._bot_name)
                self._response_done.set()

        elif event_type == "control_request":
            request_id = event.get("request_id", "")
            prompt = event.get("prompt", "Permission requested")
            value = event.get("value", {})
            if self._on_permission_request:
                self._on_permission_request(request_id, prompt, value)

    # -- internal: stderr reader --------------------------------------------

    async def _read_stderr(self) -> None:
        """Log Claude's stderr output for debugging."""
        try:
            async for line in self._process.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("[{}] claude: {}", self._bot_name, text)
        except Exception:
            pass

    # -- internal: stdin writer ----------------------------------------------

    async def _write_json(self, data: dict[str, Any]) -> None:
        """Write a JSON message to Claude's stdin."""
        line = json.dumps(data, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    # -- session persistence -------------------------------------------------

    def _save_session(self, session_id: str) -> None:
        """Persist session_id to disk for resume support."""
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_file.write_text(session_id, encoding="utf-8")

    def _load_session(self) -> str | None:
        """Load persisted session_id for resume."""
        if self._session_file.exists():
            sid = self._session_file.read_text(encoding="utf-8").strip()
            return sid if sid else None
        return None
