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
import time
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from feishu_cc.config import CONFIG_DIR

# On Windows, create child in its own process group (Ctrl+C isolation) and
# allow it to break away from any parent job so we can assign it to our own
# job object for KILL_ON_JOB_CLOSE protection.
_CREATION_FLAGS = 0
if os.name == "nt":
    _CREATION_FLAGS = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_BREAKAWAY_FROM_JOB
    )

# Windows Job Object with KILL_ON_JOB_CLOSE: when feishu-cc exits for ANY
# reason (normal exit, crash, os._exit, segfault), Windows auto-terminates
# all Claude subprocesses assigned to this job, preventing orphans.
_WIN32_JOB_HANDLE = None
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WIN32_JOB_HANDLE = _kernel32.CreateJobObjectW(None, None)
    if _WIN32_JOB_HANDLE:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.ULONG),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.ULONG),
                ("Affinity", ctypes.c_size_t),
                ("ChildProcessRateControlToken", wintypes.ULONG),
                ("ExtendedFlags", wintypes.ULONG),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if not _kernel32.SetInformationJobObject(
            _WIN32_JOB_HANDLE,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(_WIN32_JOB_HANDLE)
            _WIN32_JOB_HANDLE = None
            logger.debug("Failed to set KILL_ON_JOB_CLOSE on job object")
    else:
        logger.debug("Failed to create job object, child processes may become orphaned")

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
_RESPONSE_TIMEOUT = 300.0


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
        on_tool_use: Callable[[str, str], None] | None = None,
        on_task_summary: Callable[[str], None] | None = None,
        on_system_notify: Callable[[str, str], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        on_tool_result: Callable[[str, bool], None] | None = None,
        on_result_content: Callable[[list], None] | None = None,
    ):
        self._bot_name = bot_name
        self._claude_path = claude_path
        self._workspace = workspace
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_permission_request = on_permission_request
        self._on_tool_use = on_tool_use
        self._on_task_summary = on_task_summary
        self._on_system_notify = on_system_notify
        self._on_error = on_error
        self._on_tool_result = on_tool_result
        self._on_result_content = on_result_content

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

        # Crash tracking: time-window-based (60s) to break infinite restart loops
        # when Claude keeps crashing immediately after restart.  Unlike a simple
        # counter, the window prevents premature reset across rapid crashes.
        self._crash_times: list[float] = []

        # Guard: prevent concurrent _handle_crash invocations from old drain threads
        self._crash_handling = False

        # Response generation counter: prevents stale events from
        # prematurely unblocking a subsequent send_message.
        self._response_gen = 0

        # Set by _handle_crash to signal send_message that Claude crashed
        # mid-response, so it can raise ConnectionError instead of returning
        # silently with empty _response_text.
        self._response_error: Exception | None = None

        # Timestamp of last activity from Claude (text, tool_use, etc.)
        # Used for idle-timeout instead of fixed wall-clock timeout.
        self._last_activity = time.monotonic()

        # Startup workspace warning: set when configured workspace is missing
        self._startup_ws_warning: str | None = None

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

        # Validate workspace — fallback to cwd if configured dir doesn't exist
        self._startup_ws_warning = None
        if self._workspace and not os.path.isdir(self._workspace):
            bad_ws = self._workspace
            self._workspace = os.getcwd()
            self._startup_ws_warning = bad_ws
            logger.warning("[{}] Workspace '{}' not found, falling back to '{}'",
                           self._bot_name, bad_ws, self._workspace)

        args = self._build_args(resume_id)

        env = {**os.environ, "CLAUDE_CODE_NON_INTERACTIVE": "true"}
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._workspace or os.getcwd(),
            env=env,
            creationflags=_CREATION_FLAGS,
        )
        self._alive = True
        logger.info("[{}] Claude subprocess started (pid={})", self._bot_name, self._proc.pid)

        # Assign to Windows job object so child auto-terminates on parent
        # exit (even crash / os._exit).  We OpenProcess with the correct
        # access rights because the default subprocess handle lacks
        # PROCESS_SET_QUOTA.
        if os.name == "nt" and _WIN32_JOB_HANDLE:
            try:
                import ctypes
                _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                PROCESS_SET_QUOTA = 0x0100
                PROCESS_TERMINATE = 0x0001
                ph = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, self._proc.pid)
                if ph:
                    _k32.AssignProcessToJobObject(_WIN32_JOB_HANDLE, ph)
                    _k32.CloseHandle(ph)
                else:
                    logger.warning("[{}] Could not open handle for pid {} to assign to job",
                                   self._bot_name, self._proc.pid)
            except Exception as e:
                logger.warning("[{}] Failed to assign pid {} to job object: {}",
                               self._bot_name, self._proc.pid, e)

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
            try:
                for raw in iter(self._proc.stdout.readline, b""):
                    text = raw.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    asyncio.run_coroutine_threadsafe(
                        self._handle_line(text), self._loop
                    )
            except Exception:
                logger.exception("[{}] stdout read error", self._bot_name)

            # Pipe closed → process exited
            self._alive = False
            rc = self._proc.poll() if self._proc else None
            logger.warning(
                "[{}] Claude stdout pipe closed (pid={}, returncode={})",
                self._bot_name, self._proc.pid if self._proc else "?", rc
            )

            # Give the stderr drain thread a moment to finish reading
            # before we snapshot the buffer.  Without this, _tail_stderr()
            # often returns "(no stderr)" because the stderr thread hasn't
            # flushed its last reads yet.
            time.sleep(0.05)

            # Suppress duplicate ERROR when a crash handler is already
            # running.  This happens when _handle_crash restarts Claude
            # and the new process immediately dies before establishing
            # a session — the new process's own drain thread would
            # otherwise log a misleading ERROR that looks like two
            # independent crashes.
            if self._crash_handling:
                logger.debug("[{}] Skipping redundant crash ERROR (handler in progress)", self._bot_name)
                # Still unblock parent crash handler if new process died
                # before session established, so it can retry promptly
                # instead of waiting for _INIT_TIMEOUT (45s).
                if not self._ready.is_set():
                    asyncio.run_coroutine_threadsafe(
                        self._handle_init_failure(), self._loop
                    )
                return

            stderr = self._tail_stderr()
            logger.error(
                "[{}] Claude process {} (pid={}, returncode={}). Stderr:\n{}",
                self._bot_name,
                "died during initialization" if not self._ready.is_set() else "crashed during operation",
                self._proc.pid if self._proc else "?", rc, stderr,
            )
            if not self._ready.is_set():
                asyncio.run_coroutine_threadsafe(
                    self._handle_init_failure(), self._loop
                )
            else:
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
        """Process died during normal operation — retry with backoff."""
        if self._crash_handling:
            logger.debug("[{}] Crash handler already running, skipping", self._bot_name)
            return
        self._crash_handling = True
        try:
            # Signal send_message that Claude crashed mid-response.  Without
            # this, send_message's while-loop sees _response_done set but
            # _response_gen unchanged, logs "stale result event", clears
            # and re-waits — until _RESPONSE_TIMEOUT (5 min).
            self._response_error = ConnectionError("Claude process crashed during response")
            self._response_gen += 1
            self._response_done.set()

            # Time-window crash counting: only crashes within the last 60s
            # count toward the limit, preventing indefinite restart loops when
            # the process keeps dying immediately after restart.
            now = time.monotonic()
            self._crash_times = [t for t in self._crash_times if now - t < 60]
            self._crash_times.append(now)

            n = len(self._crash_times)
            if n > 3:
                rc = self._proc.poll() if self._proc else None
                logger.error("[{}] Claude crashed {} times in 60s, giving up (pid={}, returncode={})",
                             self._bot_name, n, self._proc.pid if self._proc else "?", rc)
                self._alive = False
                return

            backoff = min(2 ** n, 30)
            logger.info("[{}] Auto-restarting Claude (attempt {}) in {}s...",
                         self._bot_name, n, backoff)

            # Ensure old process is fully dead before spawning a new one
            await self.stop()

            if self._session_file.exists():
                self._session_file.unlink()
            self._session_id = None

            await asyncio.sleep(backoff)

            # Retry restart up to 3 times within this crash-handler call.
            # Transient failures (stale session, rapid crash after spawn)
            # are recovered immediately instead of forcing send_message
            # recovery path (which adds 45s+ delay).
            for retry in range(3):
                self._init_failed = False
                self._ready.clear()
                await self.start()

                try:
                    await asyncio.wait_for(self._ready.wait(), timeout=_INIT_TIMEOUT)
                except (asyncio.TimeoutError, ConnectionError):
                    await self.stop()
                    logger.warning("[{}] Restart attempt {}.{} failed (timeout), retrying in {}s",
                                   self._bot_name, n, retry + 1, backoff)
                    await asyncio.sleep(backoff)
                    continue

                if self._init_failed:
                    logger.warning("[{}] Restart attempt {}.{} init failed (process died), retrying in {}s",
                                   self._bot_name, n, retry + 1, backoff)
                    self._init_failed = False
                    await self.stop()
                    await asyncio.sleep(backoff)
                    continue

                logger.info("[{}] Claude process auto-restarted", self._bot_name)
                return

            logger.error("[{}] Claude restart attempt {} failed after 3 retries",
                         self._bot_name, n)
        finally:
            self._crash_handling = False

    async def stop(self) -> None:
        """Terminate the Claude subprocess."""
        self._alive = False
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return

        pid = proc.pid
        logger.info("[{}] Stopping Claude subprocess (pid={})", self._bot_name, pid)

        try:
            proc.stdin.close()
        except OSError as e:
            logger.debug("[{}] stdin close error (pid={}): {}", self._bot_name, pid, e)

        try:
            proc.terminate()
            proc.wait(timeout=5)
            logger.info("[{}] Claude subprocess terminated (pid={}, returncode={})",
                        self._bot_name, pid, proc.returncode)
        except subprocess.TimeoutExpired:
            logger.warning("[{}] Claude subprocess did not terminate in time, killing (pid={})",
                          self._bot_name, pid)
            proc.kill()
            proc.wait(timeout=5)
            logger.info("[{}] Claude subprocess killed (pid={}, returncode={})",
                        self._bot_name, pid, proc.returncode)
        except OSError as e:
            logger.debug("[{}] terminate/kill error (pid={}): {}", self._bot_name, pid, e)

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
                logger.info("[{}] Claude down _init_failed={} _alive={} _crash_handling={} crash_count={}",
                            self._bot_name, self._init_failed, self._alive,
                            self._crash_handling, len(self._crash_times))

                # If a crash handler is already running, wait for it to finish
                # instead of racing to start a new process.  Without this wait,
                # send_message recovery starts a process that the in-flight
                # _handle_crash will kill or that inherits a stale session file,
                # causing it to crash immediately — flooding the log with paired
                # ERROR entries.
                for _ in range(50):
                    if not self._crash_handling:
                        break
                    await asyncio.sleep(0.1)
                else:
                    logger.warning("[{}] Crash handler still busy after 5s, recovering anyway",
                                   self._bot_name)

                # Crash handler may have already restarted Claude by now — re-check
                # before starting a second process.
                if self._alive and self._ready.is_set():
                    logger.info("[{}] Crash handler already recovered Claude (crash_count={})",
                                self._bot_name, len(self._crash_times))
                else:
                    logger.info("[{}] Recovering Claude process...", self._bot_name)
                    if self._session_file.exists():
                        self._session_file.unlink()
                    self._session_id = None
                    self._init_failed = False
                    await self.start()
                    await asyncio.wait_for(self._ready.wait(), timeout=_INIT_TIMEOUT)

            self._response_error = None
            gen = self._response_gen = self._response_gen + 1
            self._response_text = ""
            self._response_done.clear()
            self._task_summaries.clear()

            msg = {"type": "user", "message": {"role": "user", "content": text}}
            await self._write_json(msg)

            # Guard against stale result events (from a previous message
            # delivered late) by comparing the generation counter.
            # Idle timeout: only timeout when Claude has been completely
            # silent for `timeout` seconds — any event (text, tool_use,
            # thinking, tool_result) resets the timer.
            self._last_activity = time.monotonic()
            _CHECK_INTERVAL = 30.0
            while True:
                while not self._response_done.is_set():
                    idle = time.monotonic() - self._last_activity
                    remaining = timeout - idle
                    if remaining <= 0:
                        raise asyncio.TimeoutError(
                            f"Claude idle for {idle:.0f}s (timeout={timeout:.0f}s)"
                        )
                    try:
                        await asyncio.wait_for(
                            self._response_done.wait(),
                            timeout=min(remaining, _CHECK_INTERVAL),
                        )
                    except asyncio.TimeoutError:
                        continue  # re-check idle time

                if self._response_gen > gen:
                    break
                # Stale event — ignore and re-wait
                logger.debug("[{}] Ignoring stale result event (gen={})", self._bot_name, self._response_gen)
                self._response_done.clear()

            if self._response_error:
                err = self._response_error
                self._response_error = None
                raise err
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
        """Respond to a permission request from Claude.

        Uses _send_lock to avoid racing with other stdin writes.
        NOTE: send_message releases _send_lock BEFORE waiting for
        _response_done, so holding it here cannot deadlock.
        """
        async with self._send_lock:
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
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("[{}] Invalid JSON from Claude: {}", self._bot_name, text[:200])
            return
        try:
            await self._handle_event(event)
        except Exception:
            logger.exception("[{}] Unhandled error in event handler", self._bot_name)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a single JSON event from Claude."""
        self._last_activity = time.monotonic()
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
                    if self._on_task_summary:
                        try:
                            self._on_task_summary(summary)
                        except Exception:
                            logger.exception("[{}] on_task_summary callback failed", self._bot_name)
                    if self._on_system_notify:
                        try:
                            self._on_system_notify(summary, event.get("status", ""))
                        except Exception:
                            logger.exception("[{}] on_system_notify callback failed", self._bot_name)
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
                        try:
                            self._on_text(text)
                        except Exception:
                            logger.exception("[{}] on_text callback failed", self._bot_name)
                elif block_type == "thinking":
                    thinking = block.get("thinking", "")
                    if self._on_thinking:
                        try:
                            self._on_thinking(thinking)
                        except Exception:
                            logger.exception("[{}] on_thinking callback failed", self._bot_name)
                elif block_type == "tool_use":
                    # JSON stream-json 协议中 tool_use block 示例：
                    #
                    # Read 工具:
                    #   {"type":"tool_use","name":"Read","input":{"file_path":"src/foo.py"}}
                    #   → 飞书显示: "> Read src/foo.py"
                    #
                    # Agent 工具:
                    #   {"type":"tool_use","name":"Agent","input":{"description":"探索代码","prompt":"查找路由定义"}}
                    #   → 飞书显示: "> Agent 探索代码" (brief_input 取 description)
                    #
                    # TodoWrite 工具:
                    #   {"type":"tool_use","name":"TodoWrite","input":{"todos":[{"content":"修复bug","status":"in_progress"}]}}
                    #   → 飞书显示: "> TodoWrite" (brief_input 取不到, 只显示 name)
                    #
                    # Bash 工具:
                    #   {"type":"tool_use","name":"Bash","input":{"command":"pytest tests/"}}
                    #   → 飞书显示: "> Bash pytest tests/"
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    if isinstance(inp, dict):
                        # 优先按工具类型提取关键信息
                        if name == "TodoWrite" and "todos" in inp:
                            todos = inp["todos"]
                            if isinstance(todos, list) and todos:
                                first = todos[0].get("content", "") if isinstance(todos[0], dict) else str(todos[0])
                                count = len(todos)
                                brief_inp = f"{first}{f' … +{count - 1} 项' if count > 1 else ''}"
                            else:
                                brief_inp = ""
                        else:
                            brief_inp = (
                                inp.get("command")
                                or inp.get("pattern")
                                or inp.get("query")
                                or inp.get("path")
                                or inp.get("file_path")
                                or inp.get("description")
                                or inp.get("expression")
                                or ""
                            )
                    else:
                        brief_inp = ""
                    logger.debug("[{}] Tool use: {}{}", self._bot_name, name, f" {brief_inp}" if brief_inp else "")
                    if self._on_tool_use and name:
                        brief = brief_inp[:120] if brief_inp else ""
                        try:
                            self._on_tool_use(name, brief)
                        except Exception:
                            logger.exception("[{}] on_tool_use callback failed", self._bot_name)

                elif block_type == "tool_result":
                    tool_content = block.get("content", "")
                    is_error = block.get("is_error", False)
                    if self._on_tool_result:
                        try:
                            self._on_tool_result(str(tool_content)[:200], is_error)
                        except Exception:
                            logger.exception("[{}] on_tool_result callback failed", self._bot_name)

        elif event_type == "user":
            content = event.get("message", {}).get("content", "")
            # Truncate to avoid logging entire file contents from tool_results
            content_str = str(content) if not isinstance(content, str) else content
            logger.debug("[{}] User event: {}... ({} chars)",
                         self._bot_name, content_str[:200], len(content_str))

        elif event_type == "error":
            err = event.get("error", {})
            err_type = err.get("type", "")
            err_msg = err.get("message", "")
            logger.error("[{}] Error event: {} - {}", self._bot_name, err_type, err_msg)
            if self._on_error:
                try:
                    self._on_error(err_type, err_msg)
                except Exception:
                    logger.exception("[{}] on_error callback failed", self._bot_name)

        elif event_type == "result":
            if self._on_result_content:
                try:
                    result_data = event.get("result", {})
                    content = result_data if isinstance(result_data, list) else (
                        result_data.get("content", []) if isinstance(result_data, dict) else []
                    )
                    self._on_result_content(content)
                except Exception:
                    logger.exception("[{}] on_result_content callback failed", self._bot_name)
            if self._task_summaries:
                summary_str = ", ".join(dict.fromkeys(self._task_summaries))
                self._response_text = f"> {summary_str}\n\n" + self._response_text
                self._task_summaries.clear()
            logger.debug("[{}] Result event: {}", self._bot_name, event)
            self._response_gen += 1
            self._response_done.set()

        elif event_type == "control_request":
            request_id = event.get("request_id", "")
            prompt = event.get("prompt", "Permission requested")
            value = event.get("value", {})
            if self._on_permission_request:
                try:
                    self._on_permission_request(request_id, prompt, value)
                except Exception:
                    logger.exception("[{}] on_permission_request callback failed", self._bot_name)

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
