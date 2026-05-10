"""Configuration loading for feishu-cc."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union


CONFIG_DIR = Path.home() / ".feishu-cc"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class BotConfig:
    name: str
    app_id: str
    app_secret: str
    workspace: Optional[str] = None
    system_prompt: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BotConfig:
        return cls(
            name=d["name"],
            app_id=d.get("appId", d.get("app_id", "")),
            app_secret=d.get("appSecret", d.get("app_secret", "")),
            workspace=d.get("workspace"),
            system_prompt=d.get("system_prompt"),
        )


@dataclass
class Config:
    bots: list[BotConfig] = field(default_factory=list)
    domain: str = "feishu"
    claude_path: str = "claude"
    render_mode: str = "card"
    react_emoji: str = "THUMBSUP"
    done_emoji: Optional[str] = None

    @classmethod
    def load(cls, path: Optional[Union[str, Path]] = None) -> Config:
        path = Path(path) if path else CONFIG_FILE
        if not path.exists():
            cls._create_template(path)
            raise FileNotFoundError(
                f"Template config created at {path}. "
                "Please edit it with your bot credentials and re-run feishu-cc."
            )

        raw = json.loads(path.read_text(encoding="utf-8"))

        bots = [BotConfig.from_dict(b) for b in raw.get("bots", [])]
        if not bots:
            raise ValueError("At least one bot must be configured in 'bots'")

        return cls(
            bots=bots,
            domain=raw.get("domain", "feishu"),
            claude_path=raw.get("claude_path", raw.get("claudePath", "claude")),
            render_mode=raw.get("render_mode", raw.get("renderMode", "card")),
            react_emoji=raw.get("react_emoji", raw.get("reactEmoji", "THUMBSUP")),
            done_emoji=raw.get("done_emoji", raw.get("doneEmoji")),
        )

    @classmethod
    def _create_template(cls, path: Path) -> None:
        """Create a template config file at the given path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        default_prompt = (
            "你通过飞书与用户对话。回复可以使用 `---quick-replies` 提供一键按钮。\n"
            "不要截断你的回复，用户需要看到完整内容。\n"
            "表格使用 markdown 格式即可。"
        )
        template: dict[str, Any] = {
            "bots": [
                {
                    "name": "my-bot",
                    "appId": "cli_xxxxxxxxxxxxxxxxxxxx",
                    "appSecret": "your_app_secret",
                    "workspace": None,
                    "system_prompt": default_prompt,
                }
            ],
            "domain": "feishu",
            "claude_path": "claude",
            "render_mode": "card",
            "react_emoji": "THUMBSUP",
        }
        path.write_text(
            json.dumps(template, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
