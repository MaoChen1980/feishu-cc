"""Claude Code subprocess management via JSON stream protocol.

Uses ``subprocess.Popen`` with dedicated reader threads (not asyncio pipes)
to avoid Windows pipe-buffer deadlocks — Claude's SessionStart hook can
output many kilobytes to stdout before the session event, and async pipe
readers don't drain fast enough on Windows's tiny 4 KB pipe buffers.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from feishu_cc.config import CONFIG_DIR

DEFAULT_SYSTEM_PROMPT = """\
你通过飞书与用户对话。回复可以使用 `---quick-replies` 提供一键按钮。

按钮格式说明：
- 用 `Label||Reply` 格式可以让按钮显示 "Label"，点击后发送 "Reply" 给你
- 如果只写了 `Option` 没有 `||`，则按钮显示和发送内容都是 "Option"
- 多个选项可以用 `|` 分隔写在一行，也可以用换行分开
示例：
```
觉得如何？
---quick-replies
很好||analyze:positive
一般||analyze:neutral
很差||analyze:negative
```

不要截断你的回复，用户需要看到完整内容。
表格使用 markdown 格式即可。\
"""

_STDERR_TAIL_LINES = 80
_INIT_TIMEOUT = 45.0
_RESPONSE_TIMEOUT = 120.0


class ClaudeBridge:
    """Manages a Claude Code subprocess via JSON stream protocol.

    Uses ``--output-format stream-json --input-format stream-json``
    for structured JSON communication over stdin/stdout.

    A dedicated **thread** drains stdout and dispatches JSON events to the
    asyncio event loop via ``run_coroutine_threadsafe``.  This prevents the
    Windows pipe buffer from filling up during Claude's SessionStart hook
    (which can produce many kilobytes before the session event).

    Stderr goes directly to a rolling log file (no pipe).
    """

    def __init__(
        self,
        bot_name: str,
        claude_path: str = "claude",
        workspace: Optional[str] = None,
        system_prompt: Optional[str] = None,
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

        self._proc: subprocess.Popen | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_id: Optional[str] = None
        self._response_text: str = ""
        self._response_done = asyncio.Event()
        self._ready = asyncio.Event()
        self._alive = False
        self._send_lock = asyncio.Lock()
        self._session_file = CONFIG_DIR / "sessions" / f"{bot_name}.session"

        # Task summaries from Claude — semantic descriptions of what it did
        self._task_summaries: list[str] = []

        # Stderr diagnostics buffer (fed by drain thread, always in memory)
        self._stderr_buf: list[str] = []

        # Init failure flag: process died before _ready was set
        self._init_failed = False

        # Reader threads
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
    # -- lifecycle -----------------------------------------------------------

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    async def start(self) -> None:
        """Spawn the Claude Code subprocess and start reader threads."""
        resume_id = self._load_session()

        self._loop = asyncio.get_running_loop()
        self._ready.clear()
        self._response_done.clear()

        args = self._build_args(resume_id)

        env = {**os.environ, "CLAUDE_CODE_NON_INTERACTIVE": "true"}
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._workspace or os.getcwd(),
            env=env,
        )
        self._alive = True
        logger.info("[{}] Claude subprocess started (pid={})", self._bot_name, self._proc.pid)

        # Start both reader threads — stdout dispatches to event loop,
        # stderr stays in memory for diagnostics.
        self._start_stdout_thread()
        self._start_stderr_thread()

    def _build_args(self, resume_id: Optional[str]) -> list[str]:
        args = [
            self._claude_path,
            "--verbose",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--permission-prompt-tool", "stdio",
            "--append-system-prompt", self._system_prompt,
        ]
        if resume_id:
            args.extend(["--resume", resume_id])
            logger.info("[{}] Resuming session {}", self._bot_name, resume_id)
        args.extend(["-p", ""])
        return args

    def _start_stdout_thread(self) -> None:
        """Thread: drains stdout line by line, dispatches to event loop."""

        def _drain() -> None:
            for raw in iter(self._proc.stdout.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                asyncio.run_coroutine_threadsafe(
                    self._handle_line(text), self._loop
                )

            # Pipe closed → process exited
            self._alive = False
            if not self._ready.is_set():
                stderr = self._tail_stderr()
                logger.error(
                    "[{}] Claude process died during initialization. Stderr:\n{}",
                    self._bot_name, stderr
                )
                asyncio.run_coroutine_threadsafe(
                    self._handle_init_failure(), self._loop
                )
            else:
                stderr = self._tail_stderr()
                logger.error(
                    "[{}] Claude process crashed. Auto-restarting. Stderr:\n{}",
                    self._bot_name, stderr
                )
                asyncio.run_coroutine_threadsafe(
                    self._handle_crash(), self._loop
                )

        self._stdout_thread = threading.Thread(target=_drain, daemon=True)
        self._stdout_thread.start()

    def _start_stderr_thread(self) -> None:
        """Thread: drains stderr into in-memory buffer for diagnostics."""

        def _drain() -> None:
            for raw in iter(self._proc.stderr.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr_buf.append(text)
                    logger.debug("[{}] claude: {}", self._bot_name, text)

        self._stderr_thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread.start()

    async def _handle_init_failure(self) -> None:
        """Process died before sending session event — remove stale session and unblock."""
        if self._session_file.exists():
            logger.warning("[{}] Removing stale session file: {}", self._bot_name, self._session_file)
            self._session_file.unlink()
        self._init_failed = True
        self._ready.set()

    async def _handle_crash(self) -> None:
        """Process died during normal operation — unblock waiters and auto-restart."""
        # Unblock any send_message that was waiting for a response
        self._response_done.set()
        # Restart fresh (no resume)
        if self._session_file.exists():
            self._session_file.unlink()
        self._session_id = None
        logger.info("[{}] Auto-restarting Claude process...", self._bot_name)
        await self.start()
        await asyncio.wait_for(self._ready.wait(), timeout=_INIT_TIMEOUT)
        logger.info("[{}] Claude process auto-restarted", self._bot_name)

    async def stop(self) -> None:
        """Terminate the Claude subprocess (three-phase shutdown)."""
        self._alive = False
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return

        proc.stdin.close()

        for sig in ("terminate", "kill"):
            proc.wait(timeout=5)
            return

        proc.wait(timeout=5)

    # -- send ----------------------------------------------------------------

    async def send_message(self, text: str, timeout: float = _RESPONSE_TIMEOUT) -> str:
        """Send a user message to Claude and wait for the complete response."""
        async with self._send_lock:
            if not self._ready.is_set():
                logger.info("[{}] Waiting for Claude to finish initializing...", self._bot_name)
                try:
                    await asyncio.wait_for(self._ready.wait(), timeout=_INIT_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("[{}] Init timed out, attempting recovery...", self._bot_name)
                    self._init_failed = True

            # Recover from init failure (stale session, crashed process, etc.)
            if self._init_failed or not self._alive:
                logger.info("[{}] Recovering Claude process...", self._bot_name)
                if self._session_file.exists():
                    self._session_file.unlink()
                self._session_id = None
                self._init_failed = False
                await self.start()
                await asyncio.wait_for(self._ready.wait(), timeout=_INIT_TIMEOUT)

            self._response_text = ""
            self._response_done.clear()
            self._task_summaries.clear()

            msg = {"type": "user", "message": {"role": "user", "content": text}}
            await self._write_json(msg)

            await asyncio.wait_for(self._response_done.wait(), timeout=timeout)
            return self._response_text

    async def restart(self, workspace: str) -> None:
        logger.info("[{}] Switching workspace to {}", self._bot_name, workspace)
        self._workspace = workspace
        await self.stop()
        if self._session_file.exists():
            self._session_file.unlink()
        self._session_id = None
        await self.start()

    async def respond_permission(self, request_id: str, behavior: str) -> None:
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

    # -- internal: stdout line processing (runs on event loop) --------------

    async def _handle_line(self, text: str) -> None:
        """Parse a single JSON line from stdout."""
        event = json.loads(text)
        await self._handle_event(event)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a single JSON event from Claude."""
        event_type = event.get("type", "")

        if event_type == "system":
            if event.get("hook_id"):
                sid = event.get("session_id", "")
                if sid and not self._session_id:
                    self._session_id = sid
                    self._save_session(sid)
                if event.get("subtype") == "hook_response":
                    self._ready.set()
                    logger.info("[{}] Session established: {}", self._bot_name, self._session_id)
                return
            sid = event.get("session_id", "")
            if sid and sid != self._session_id:
                self._session_id = sid
                self._save_session(sid)
                logger.info("[{}] Session established: {}", self._bot_name, sid)
            if sid:
                self._ready.set()

            subtype = event.get("subtype", "")
            if subtype == "task_notification" and event.get("status") == "completed":
                summary = event.get("summary", "")
                if summary:
                    self._task_summaries.append(summary)
                return

            logger.debug("[{}] System event: {}", self._bot_name, event)

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
                elif block_type == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    if isinstance(inp, dict):
                        brief_inp = inp.get("command") or inp.get("path") or inp.get("file_path") or ""
                    else:
                        brief_inp = ""
                    logger.debug("[{}] Tool use: {}{}", self._bot_name, name, f" {brief_inp}" if brief_inp else "")

        elif event_type == "user":
            logger.debug("[{}] User event: {}", self._bot_name, event.get("message", {}).get("content", ""))

        elif event_type == "result":
            if self._task_summaries:
                summary_str = ", ".join(dict.fromkeys(self._task_summaries))
                self._response_text = f"> {summary_str}\n\n" + self._response_text
                self._task_summaries.clear()
            logger.debug("[{}] Result event: {}", self._bot_name, event)
            self._response_done.set()

        elif event_type == "control_request":
            request_id = event.get("request_id", "")
            prompt = event.get("prompt", "Permission requested")
            value = event.get("value", {})
            if self._on_permission_request:
                self._on_permission_request(request_id, prompt, value)

        else:
            logger.info("[{}] Unhandled event type: {} - {}", self._bot_name, event_type, event)

    # -- internal: stdin writer ----------------------------------------------

    async def _write_json(self, data: dict[str, Any]) -> None:
        """Write JSON to Claude's stdin (thread pool to avoid blocking)."""
        if not self._alive or not self._proc:
            stderr_text = self._tail_stderr()
            msg = f"Claude process is not running"
            if stderr_text:
                msg += f".\nStderr:\n{stderr_text}"
            raise ConnectionError(msg)

        line = json.dumps(data, ensure_ascii=False) + "\n"
        encoded = line.encode()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._proc.stdin.write, encoded)
        await loop.run_in_executor(None, self._proc.stdin.flush)

    # -- internal: stderr diagnostics ---------------------------------------

    def _tail_stderr(self, max_lines: int = _STDERR_TAIL_LINES) -> str:
        """Return last *max_lines* from the in-memory stderr buffer."""
        if self._stderr_buf:
            return "\n".join(self._stderr_buf[-max_lines:])
        return "(no stderr)"

    # -- session persistence -------------------------------------------------

    def _save_session(self, session_id: str) -> None:
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_file.write_text(session_id, encoding="utf-8")

    def _load_session(self) -> Optional[str]:
        if self._session_file.exists():
            sid = self._session_file.read_text(encoding="utf-8").strip()
            return sid if sid else None
        return None
