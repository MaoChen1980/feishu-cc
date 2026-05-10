"""Entry point for feishu-cc.

Usage::

    python -m feishu_cc [--config <path>]
    feishu-cc [--config <path>]
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger


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

    from feishu_cc.app import FeishuCCApp

    app = FeishuCCApp(config_path=args.config)
    app.run()


if __name__ == "__main__":
    main()
