"""Tests for config loading and template creation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_cc.config import BotConfig, Config


class TestBotConfig:
    def test_from_dict_full(self) -> None:
        cfg = BotConfig.from_dict({
            "name": "test-bot",
            "appId": "cli_abc123",
            "appSecret": "secret_xyz",
            "workspace": "/tmp/ws",
            "system_prompt": "Be helpful",
        })
        assert cfg.name == "test-bot"
        assert cfg.app_id == "cli_abc123"
        assert cfg.app_secret == "secret_xyz"
        assert cfg.workspace == "/tmp/ws"
        assert cfg.system_prompt == "Be helpful"

    def test_from_dict_minimal(self) -> None:
        cfg = BotConfig.from_dict({
            "name": "minimal",
            "app_id": "cli_min",
            "app_secret": "secret_min",
        })
        assert cfg.name == "minimal"
        assert cfg.workspace is None
        assert cfg.system_prompt is None

    def test_from_dict_underscore_keys(self) -> None:
        cfg = BotConfig.from_dict({
            "name": "uscore",
            "app_id": "cli_underscore",
            "app_secret": "secret_underscore",
        })
        assert cfg.app_id == "cli_underscore"
        assert cfg.app_secret == "secret_underscore"


class TestConfig:
    def test_create_template(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".feishu-cc" / "config.json"
        Config._create_template(config_path)

        assert config_path.exists()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "bots" in data
        assert len(data["bots"]) == 1
        assert data["bots"][0]["name"] == "my-bot"
        assert "---quick-replies" in data["bots"][0]["system_prompt"]
        assert data["bots"][0]["appId"] == "cli_xxxxxxxxxxxxxxxxxxxx"
        assert data["domain"] == "feishu"

    def test_load_valid_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "bots": [{
                "name": "unit-test",
                "appId": "cli_test",
                "appSecret": "secret_test",
            }],
            "domain": "feishu",
        }), encoding="utf-8")

        cfg = Config.load(config_path)
        assert len(cfg.bots) == 1
        assert cfg.bots[0].name == "unit-test"
        assert cfg.domain == "feishu"

    def test_load_missing_raises_with_template(self, tmp_path: Path) -> None:
        config_path = tmp_path / "nonexistent" / "config.json"
        with pytest.raises(FileNotFoundError, match="Template config created"):
            Config.load(config_path)
        assert config_path.exists()

    def test_load_with_camelcase_keys(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "bots": [{
                "name": "camel",
                "appId": "cli_camel",
                "appSecret": "secret_camel",
            }],
            "claudePath": "claude-dev",
            "renderMode": "post",
            "reactEmoji": "ROCKET",
        }), encoding="utf-8")

        cfg = Config.load(config_path)
        assert cfg.claude_path == "claude-dev"
        assert cfg.render_mode == "post"
        assert cfg.react_emoji == "ROCKET"

    def test_load_custom_config_path(self, tmp_path: Path) -> None:
        config_path = tmp_path / "custom_config.json"
        config_path.write_text(json.dumps({
            "bots": [{
                "name": "custom-path",
                "appId": "cli_custom",
                "appSecret": "secret_custom",
            }],
        }), encoding="utf-8")

        cfg = Config.load(config_path)
        assert cfg.bots[0].name == "custom-path"

    def test_empty_bots_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "bots": [],
        }), encoding="utf-8")

        with pytest.raises(ValueError, match="At least one bot"):
            Config.load(config_path)
