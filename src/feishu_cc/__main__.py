"""Entry point for feishu-cc.

Usage::

    python -m feishu_cc [--config <path>]
    feishu-cc [--config <path>]
"""

from __future__ import annotations

import argparse
import atexit
import os
import sys

from loguru import logger

from feishu_cc.config import CONFIG_DIR

PID_FILE = CONFIG_DIR / "feishu-cc.pid"


def _check_pid() -> None:
    """Ensure only one feishu-cc instance runs via PID file."""
    current_pid = os.getpid()

    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None

        if old_pid and old_pid != current_pid:
            try:
                os.kill(old_pid, 0)  # signal 0 = probe alive
            except PermissionError:
                logger.warning("PID {} exists but access denied — another instance is running", old_pid)
                sys.exit(1)
            except OSError:
                logger.debug("Stale PID {} found, overwriting", old_pid)
            else:
                logger.error("Another instance (PID {}) is already running, exiting.", old_pid)
                sys.exit(1)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(current_pid))
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
    logger.debug("PID {} written to {}", current_pid, PID_FILE)


def main() -> None:
    parser = argparse.ArgumentParser(description="feishu-cc — Feishu IM bridge for Claude Code CLI")
    parser.add_argument(
        "--config", default=None,
        help="Path to config file (default: ~/.feishu-cc/config.json)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level=args.log_level)

    log_dir = CONFIG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "feishu-cc_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="10 MB",
        retention=30,
        encoding="utf-8",
    )

    _check_pid()

    from feishu_cc.app import FeishuCCApp

    app = FeishuCCApp(config_path=args.config)
    app.run()


if __name__ == "__main__":
    main()
