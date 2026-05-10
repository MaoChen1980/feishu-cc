"""Configuration loading for feishu-cc."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONFIG_DIR = Path.home() / ".feishu-cc"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class BotConfig:
    name: str
    app_id: str
    app_secret: str
    workspace: str | None = None
    system_prompt: str | None = None

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
    done_emoji: str | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        path = Path(path) if path else CONFIG_FILE
        if not path.exists():
            raise FileNotFoundError(
                f"Config not found at {path}. "
                "Create the file with your Feishu bot credentials."
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
